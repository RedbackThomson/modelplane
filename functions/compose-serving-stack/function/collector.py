# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The OpenTelemetry collector each inference cluster runs.

It scrapes everything Modelplane installs, renames what it scraped onto the
modelplane_* surface, merges each deployment's replicas into one series, and
exports to the destination.

Rendered here rather than through the OpenTelemetry Operator: its
OpenTelemetryCollector CRD would be one more operator on every GPU cluster to
install, own and upgrade, for a Deployment and a ConfigMap that this composes
directly. The receiver does its own Kubernetes service discovery, so nothing
here needs the operator's target allocator either.
"""

import hashlib
from typing import Any

import yaml

NAMESPACE = "modelplane-system"
NAME = "modelplane-collector"

# Pinned rather than floating: a collector that silently changed what it
# renames on a chart bump would move the metric surface under an operator's
# dashboards.
IMAGE = "otel/opentelemetry-collector-contrib:0.139.0"

# The label Modelplane stamps on every serving pod, and the port name it gives
# the engine's metrics. Both matter: an engine container's port is unnamed by
# default, and matching by number would find the pd-sidecar on a disaggregated
# pod rather than the engine behind it.
_SERVING_LABEL = "modelplane_ai_serving"
_METRICS_PORT = "http"

_SCRAPE_INTERVAL = "15s"
_SUBSTRATE_INTERVAL = "30s"


def _relabel_pod_identity() -> list[dict[str, Any]]:
    """Carry Modelplane's identity from the pod's labels onto every series.

    A recorded series inherits these, so a MetricMapping's statements need no
    labels of their own.
    """
    return [
        {"source_labels": [f"__meta_kubernetes_pod_label_{src}"], "target_label": dst}
        for src, dst in (
            ("modelplane_ai_deployment", "deployment"),
            ("modelplane_ai_model", "model"),
            ("modelplane_ai_engine", "engine"),
            ("modelplane_ai_role", "role"),
        )
    ] + [{"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"}]


def _scrape_configs() -> list[dict[str, Any]]:
    """What to scrape on an inference cluster.

    The gateway's GenAI metrics sit on the ext-proc sidecar's admin port rather
    than the proxy's, so the front door needs a target of its own.
    """
    return [
        {
            "job_name": "modelplane-engines",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {"source_labels": [f"__meta_kubernetes_pod_label_{_SERVING_LABEL}"], "action": "keep", "regex": "true"},
                {
                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                    "action": "keep",
                    "regex": _METRICS_PORT,
                },
                *_relabel_pod_identity(),
            ],
        },
        {
            "job_name": "modelplane-gateway",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"],
                    "action": "keep",
                    "regex": ".+",
                },
                {"source_labels": ["__meta_kubernetes_pod_container_port_name"], "action": "keep", "regex": "metrics"},
            ],
        },
        {
            "job_name": "modelplane-substrate",
            "scrape_interval": _SUBSTRATE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                    "action": "keep",
                    "regex": "true",
                },
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
            ],
        },
    ]


def _transform(statements: list[str]) -> dict[str, Any]:
    """Modelplane's renames, then whatever the MetricMappings add."""
    return {"metric_statements": [{"context": "metric", "statements": statements}]}


def config(
    cluster: str,
    statements: list[str],
    exporters: dict[str, Any],
    extensions: dict[str, Any],
    *,
    keep_raw: bool,
) -> str:
    """The collector's configuration, as YAML.

    Only modelplane_* leaves the cluster unless a MetricMapping asked to keep an
    engine's own names: a series the statements did not rename is one whose
    meaning Modelplane cannot vouch for across engines, and it costs the same to
    carry as one that was renamed.
    """
    processors: dict[str, Any] = {
        # cluster is stamped here rather than downstream: one receiver on the
        # control plane sees a merged stream and cannot tell senders apart.
        "resource/cluster": {"attributes": [{"key": "cluster", "value": cluster, "action": "upsert"}]},
        "transform/modelplane": _transform(statements),
        # A pod's identity is a resource attribute, where a metric processor
        # cannot reach it. Strip and merge the resources first, or the
        # aggregation below combines nothing.
        "groupbyattrs/replicas": {"keys": ["cluster", "namespace", "deployment", "model", "engine", "role"]},
        "batch": {"timeout": "10s"},
    }
    pipeline = ["resource/cluster", "transform/modelplane", "groupbyattrs/replicas"]
    if not keep_raw:
        processors["filter/modelplane"] = {
            "metrics": {"metric": ['not IsMatch(name, "^modelplane_.*")']},
        }
        pipeline.append("filter/modelplane")
    pipeline.append("batch")

    service: dict[str, Any] = {
        "pipelines": {"metrics": {"receivers": ["prometheus"], "processors": pipeline, "exporters": sorted(exporters)}},
    }
    cfg: dict[str, Any] = {
        "receivers": {"prometheus": {"config": {"scrape_configs": _scrape_configs()}}},
        "processors": processors,
        "exporters": exporters,
        "service": service,
    }
    if extensions:
        cfg["extensions"] = extensions
        # An authenticator the service does not list is one the collector will
        # not load, and an exporter referencing it then fails at startup.
        service["extensions"] = sorted(extensions)
    return yaml.safe_dump(cfg, sort_keys=False)


def _digest(rendered: str) -> str:
    """A stable hash of the rendered config, so the pod restarts when it changes."""
    return hashlib.sha256(rendered.encode()).hexdigest()[:16]


def objects(
    cluster: str,
    statements: list[str],
    exporters: dict[str, Any],
    extensions: dict[str, Any],
    secret_name: str | None,
    *,
    keep_raw: bool,
) -> list[tuple[str, dict[str, Any], str | None]]:
    """The collector as (key, manifest, readiness CEL) triples."""
    labels = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/managed-by": "modelplane"}
    rendered = config(cluster, statements, exporters, extensions, keep_raw=keep_raw)
    volumes: list[dict[str, Any]] = [{"name": "config", "configMap": {"name": NAME}}]
    mounts: list[dict[str, Any]] = [{"name": "config", "mountPath": "/conf"}]
    env_from: list[dict[str, Any]] = []
    if secret_name:
        # Mounted both ways. An environment variable is fixed for the life of a
        # process, so a rotated credential would need a restart to be read; a
        # mounted file is refreshed in place and an authenticator reading one
        # picks the new credential up without one.
        volumes.append({"name": "credentials", "secret": {"secretName": secret_name}})
        mounts.append({"name": "credentials", "mountPath": "/etc/modelplane/telemetry", "readOnly": True})
        env_from.append({"secretRef": {"name": secret_name}})

    return [
        (
            "collector-serviceaccount",
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
            },
            None,
        ),
        (
            "collector-clusterrole",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": NAME, "labels": labels},
                # Read-only, and only what Kubernetes service discovery needs.
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["pods", "services", "endpoints", "nodes", "nodes/metrics"],
                        "verbs": ["get", "list", "watch"],
                    },
                    {"nonResourceURLs": ["/metrics"], "verbs": ["get"]},
                ],
            },
            None,
        ),
        (
            "collector-clusterrolebinding",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": NAME, "labels": labels},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NAME},
                "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}],
            },
            None,
        ),
        (
            "collector-config",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
                "data": {"collector.yaml": rendered},
            },
            None,
        ),
        (
            "collector",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                    "template": {
                        "metadata": {
                            "labels": {"app.kubernetes.io/name": NAME},
                            # The config is a file, so a changed ConfigMap does
                            # not restart the pod on its own. Hashed with
                            # sha256 rather than hash(), whose string seed is
                            # randomised per process: the annotation would
                            # differ on every reconcile and redeploy the
                            # collector forever.
                            "annotations": {"modelplane.ai/config-hash": _digest(rendered)},
                        },
                        "spec": {
                            "serviceAccountName": NAME,
                            "containers": [
                                {
                                    "name": "collector",
                                    "image": IMAGE,
                                    "args": ["--config=/conf/collector.yaml"],
                                    "volumeMounts": mounts,
                                    **({"envFrom": env_from} if env_from else {}),
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                }
                            ],
                            "volumes": volumes,
                        },
                    },
                },
            },
            "object.status.readyReplicas > 0",
        ),
    ]
