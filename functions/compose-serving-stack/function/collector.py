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
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1
from models.ai.modelplane.telemetrydestination import v1alpha1 as tdv1alpha1

NAMESPACE = "modelplane-system"
NAME = "modelplane-collector"
_CREDENTIALS_DIR = "/etc/modelplane/telemetry"

IMAGE = "otel/opentelemetry-collector-contrib:0.161.0"

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


# What each source unit is worth in the base unit the target name claims.
# Written as the expression rather than a factor so nothing has to render a
# float: 1e-09 is not an OTTL literal.
_UNIT_CONVERSION = {
    "Millijoules": "value_double / 1000",
    "Milliseconds": "value_double / 1000",
    "Nanoseconds": "value_double / 1000000000",
    "Mebibytes": "value_double * 1048576",
}


def statements(mappings: list[mmv1alpha1.MetricMapping]) -> tuple[list[str], list[str]]:
    """Compile the mappings to OTTL, as (datapoint, metric) statements.

    OTTL is rendered here rather than written in a MetricMapping so the kind
    stays a description of what a component emits, and the collector's own
    configuration language stays Modelplane's problem. It is also the only
    place that knows a unit conversion has to run somewhere a rename cannot.
    """
    datapoint: list[str] = []
    metric: list[str] = []
    for mapping in mappings:
        for m in mapping.spec.metrics:
            if m.fromUnit:
                conversion = _UNIT_CONVERSION[m.fromUnit]
                datapoint.append(f'set(value_double, {conversion}) where metric.name == "{m.from_}"')
            metric.append(f'set(name, "{m.to}") where name == "{m.from_}"')
    return datapoint, metric


def _transform(mappings: list[mmv1alpha1.MetricMapping]) -> dict[str, Any]:
    """Unit conversions first, then every rename.

    Two blocks rather than one list: a statement reaching a datapoint's value
    can't run in the metric context, and the processor finishes a block over
    every datapoint before it starts the next, which is what keeps a rename
    from stranding the datapoints a conversion hasn't reached yet.
    """
    datapoint, metric = statements(mappings)
    blocks = [{"context": "metric", "statements": metric}]
    if datapoint:
        blocks.insert(0, {"context": "datapoint", "statements": datapoint})
    return {"metric_statements": blocks}


def _credential_path(sink: tdv1alpha1.Sink, key: str) -> str:
    return f"{_CREDENTIALS_DIR}/{sink.name}/{key}"


def authenticators(sinks: list[tdv1alpha1.Sink]) -> dict[str, Any]:
    """The extensions Modelplane composes for the sinks that asked for one.

    The collector carries no credential on an exporter: it authenticates
    through an extension the exporter names. A typed auth block is therefore
    an extension plus a reference, not a field, and composing both is what
    keeps `auth` from being something an operator has to assemble by hand.
    """
    composed: dict[str, Any] = {}
    for sink in sinks:
        if sink.auth and sink.auth.bearerTokenKey:
            composed[f"bearertokenauth/{sink.name}"] = {
                # A file rather than the environment: an environment variable
                # is fixed for the life of the process, so a rotated token
                # would need a restart to be read.
                "filename": _credential_path(sink, sink.auth.bearerTokenKey),
            }
    return composed


def exporters(sinks: list[tdv1alpha1.Sink]) -> dict[str, Any]:
    """The sinks as the collector's exporters block.

    A sink renders under `<type>/<name>`, which is how the collector names a
    second instance of one component, so two sinks of the same type don't
    collide.

    The endpoint and the authenticator reference are Modelplane's, and go on
    last: an operator's own config can carry anything the exporter takes, but
    not quietly redirect the sink somewhere else or unpick its credential.
    """
    rendered: dict[str, Any] = {}
    for sink in sinks:
        cfg: dict[str, Any] = dict(sink.config or {})
        if sink.endpoint:
            cfg["endpoint"] = sink.endpoint
        if sink.auth and sink.auth.bearerTokenKey:
            cfg["auth"] = {"authenticator": f"bearertokenauth/{sink.name}"}
        rendered[f"{sink.type}/{sink.name}"] = cfg
    return rendered


def config(
    cluster: str,
    mappings: list[mmv1alpha1.MetricMapping],
    sinks: list[tdv1alpha1.Sink],
    extensions: dict[str, Any],
) -> str:
    """The collector's configuration, as YAML.

    Only modelplane_* leaves the cluster: a series no mapping renamed is one
    whose meaning Modelplane cannot vouch for across engines, and it costs the
    same to carry as one that was renamed.
    """
    sink_exporters = exporters(sinks)
    # Modelplane's authenticators last: an operator's extensions can define
    # anything, but not replace the one composed for a sink's own auth block.
    extensions = {**extensions, **authenticators(sinks)}
    processors: dict[str, Any] = {
        # cluster is stamped here rather than downstream: one receiver on the
        # control plane sees a merged stream and cannot tell senders apart.
        "resource/cluster": {"attributes": [{"key": "cluster", "value": cluster, "action": "upsert"}]},
        "transform/modelplane": _transform(mappings),
        # A pod's identity is a resource attribute, where a metric processor
        # cannot reach it. Strip and merge the resources first, or the
        # aggregation below combines nothing.
        "groupbyattrs/replicas": {"keys": ["cluster", "namespace", "deployment", "model", "engine", "role"]},
        "filter/modelplane": {"metrics": {"metric": ['not IsMatch(name, "^modelplane_.*")']}},
        "batch": {"timeout": "10s"},
    }
    pipeline = [
        "resource/cluster",
        "transform/modelplane",
        "groupbyattrs/replicas",
        "filter/modelplane",
        "batch",
    ]

    service: dict[str, Any] = {
        "pipelines": {
            "metrics": {
                "receivers": ["prometheus"],
                "processors": pipeline,
                "exporters": sorted(sink_exporters),
            }
        },
    }
    cfg: dict[str, Any] = {
        "receivers": {"prometheus": {"config": {"scrape_configs": _scrape_configs()}}},
        "processors": processors,
        "exporters": sink_exporters,
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
    mappings: list[mmv1alpha1.MetricMapping],
    sinks: list[tdv1alpha1.Sink],
    extensions: dict[str, Any],
) -> list[tuple[str, dict[str, Any], str | None]]:
    """The collector as (key, manifest, readiness CEL) triples.

    Composed here rather than as a Manifests entry in the stack because the
    stack is fixed at build time and every object here depends on request-time
    data: the ConfigMap holds config rendered from the TelemetryDestination and
    the MetricMappings, the Deployment carries that config's digest and the
    destination's optional Secret mounts, and none of it exists at all until a
    TelemetryDestination does. The ServiceAccount and RBAC would fit the stack,
    but splitting one component across two mechanisms would put a collector's
    permissions on clusters running no collector.
    """
    labels = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/managed-by": "modelplane"}
    rendered = config(cluster, mappings, sinks, extensions)
    volumes: list[dict[str, Any]] = [{"name": "config", "configMap": {"name": NAME}}]
    mounts: list[dict[str, Any]] = [{"name": "config", "mountPath": "/conf"}]
    env_from: list[dict[str, Any]] = []
    for sink in sinks:
        if not sink.secretRef:
            continue
        # Mounted both ways. An environment variable is fixed for the life of a
        # process, so a rotated credential would need a restart to be read; a
        # mounted file is refreshed in place and an authenticator reading one
        # picks the new credential up without one. The file is under the sink's
        # own directory, so two sinks can both hold a key called `token`.
        volume = f"credentials-{sink.name}"
        volumes.append({"name": volume, "secret": {"secretName": sink.secretRef.name}})
        mounts.append({"name": volume, "mountPath": f"{_CREDENTIALS_DIR}/{sink.name}", "readOnly": True})
        env_from.append({"secretRef": {"name": sink.secretRef.name}})

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
                            # A changed ConfigMap doesn't restart the pod on
                            # its own. sha256 rather than hash(), whose string
                            # seed is randomised per process and would redeploy
                            # the collector on every reconcile.
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
