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

"""Tests that the fleet's telemetry reaches its destination renamed, converted and attributed.

e2e/manifests/60-telemetry.yaml points a TelemetryDestination at the collector's
debug exporter, which prints what reached it to the collector's own log. So one
log covers the whole path: service discovery found the engine by the labels
compose-model-replica stamps on serving pods, the built-in MetricMappings
renamed its series, the unit conversion ran, and the identity came off the pod.
"""

import dataclasses
import logging

import pytest
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1

from e2e import kube, wait

log = logging.getLogger(__name__)

# The collector the serving stack composes on the workload cluster.
COLLECTOR_NAMESPACE = "modelplane-system"
COLLECTOR = "modelplane-collector"


@dataclasses.dataclass
class Case:
    """A line the collector's debug exporter does or doesn't print."""

    name: str
    reason: str
    line: str
    want: bool


@pytest.fixture(scope="module")
def exported(control_plane: kube.Cluster, workload: kube.Cluster) -> str:
    """Wait for the collector to export the engine's series, and return the collector's log.

    The wait is for the DCGM series, which the engine publishes alongside the
    rest. If it never arrives, this returns the collector's log anyway, so each
    test reports whether its own line is there.
    """

    # The collector scrapes the engine pod directly, so this waits for the
    # engine rather than for anything to route to it.
    def deployed() -> None:
        obj = control_plane.modelplane("modeldeployments", "mock-demo", "ml-team")
        assert obj is not None, "ModelDeployment ml-team/mock-demo doesn't exist"
        md = mdv1alpha1.ModelDeployment.model_validate(obj)
        conditions = (md.status.conditions if md.status else None) or []
        assert any(c.type == "Ready" and c.status == "True" for c in conditions), (
            f"ModelDeployment ml-team/mock-demo isn't Ready: {[(c.type, c.status, c.reason) for c in conditions]}"
        )

    wait.until(deployed, timeout=20 * 60, what="ModelDeployment ml-team/mock-demo to be Ready", retry=kube.RETRY)

    def collector_rolled_out() -> None:
        d = workload.apps.read_namespaced_deployment(
            COLLECTOR, COLLECTOR_NAMESPACE, _request_timeout=kube.TIMEOUT_SECONDS
        )
        kube.rolled_out(d)

    wait.until(collector_rolled_out, timeout=3 * 60, what=f"Deployment {COLLECTOR} to roll out", retry=kube.RETRY)

    def collector_log() -> str:
        d = workload.apps.read_namespaced_deployment(
            COLLECTOR, COLLECTOR_NAMESPACE, _request_timeout=kube.TIMEOUT_SECONDS
        )
        selector = ",".join(f"{k}={v}" for k, v in d.spec.selector.match_labels.items())
        pods = workload.core.list_namespaced_pod(
            COLLECTOR_NAMESPACE, label_selector=selector, _request_timeout=kube.TIMEOUT_SECONDS
        )
        return "\n".join(
            workload.logs(p.metadata.name, COLLECTOR_NAMESPACE, "collector", tail_lines=4000) for p in pods.items
        )

    # The collector's config arrives by reconcile. On a fresh install it can
    # roll out once against the destination and again once the MetricMappings
    # land, and the restart the config change triggers starts its log over. In
    # steady state the first read already has everything.
    def scraped() -> str:
        logs = collector_log()
        assert "modelplane_gpu_memory_used_bytes" in logs, "the collector hasn't exported the engine's series yet"
        return logs

    try:
        logs = wait.until(
            scraped, timeout=5 * 60, interval=10, what="the collector to export the engine's series", retry=kube.RETRY
        )
    except AssertionError:
        logs = collector_log()
    # What a failing test needs to see, without the rest of the log.
    summary = [line for line in logs.splitlines() if any(k in line for k in ("Name: ", "-> ", "Value: "))]
    log.info("The collector exported:\n%s", "\n".join(summary[-60:]))
    return logs


EXPORTED_CASES = [
    Case(
        name="RequestsWaiting",
        reason="The engine's vllm:num_requests_waiting arrives renamed to modelplane_requests_waiting.",
        line="Name: modelplane_requests_waiting",
        want=True,
    ),
    Case(
        name="GPUMemoryUsed",
        reason="DCGM_FI_DEV_FB_USED arrives renamed to modelplane_gpu_memory_used_bytes.",
        line="Name: modelplane_gpu_memory_used_bytes",
        want=True,
    ),
    # DCGM reports the framebuffer in MiB and the name says bytes, so a mapping
    # that forgot the unit reads 1024 here instead.
    Case(
        name="MiBToBytes",
        reason="The 1024 MiB of framebuffer DCGM reports arrives as 1073741824 bytes.",
        line="Value: 1073741824",
        want=True,
    ),
    Case(
        name="Deployment",
        reason="A series carries the ModelDeployment it belongs to.",
        line="deployment: Str(mock-demo)",
        want=True,
    ),
    Case(
        name="Engine",
        reason="A series carries the engine it belongs to.",
        line="engine: Str(mock)",
        want=True,
    ),
    Case(
        name="Role",
        reason="A series carries the role of the engine member it belongs to.",
        line="role: Str(Standalone)",
        want=True,
    ),
    Case(
        name="Cluster",
        reason="A series carries the InferenceCluster it came from.",
        line="cluster: Str(local)",
        want=True,
    ),
    # Nothing the mappings didn't rename leaves a cluster.
    Case(
        name="EngineNames",
        reason="The engine's own vllm: names don't leave the cluster.",
        line="Name: vllm:num_requests_waiting",
        want=False,
    ),
]


@pytest.mark.parametrize("case", EXPORTED_CASES, ids=lambda case: case.name)
def test_exported(case: Case, exported: str) -> None:
    """The collector's debug exporter prints a line, or doesn't."""
    found = case.line in exported
    assert found == case.want, case.reason
