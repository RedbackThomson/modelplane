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

"""Tests for the serving stack component lists.

The join itself is the assertion: components() fails closed on
duplicate keys and on depends_on edges the join didn't produce, so
iterating every cloud and stack pair gates every list - including the
generated ones, once mapped - without involving fn.py.
"""

import pytest
from function import stacks
from function.stacks.clouds import civo


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_every_cloud_and_stack_joins(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every cloud and stack pair joins into a non-empty stack."""
    got = stacks.join(cloud, stack)
    assert got, "a joined stack can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_charts_have_reserved_release_names(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Chart's Helm release is named mp-<chart>."""
    for c in stacks.join(cloud, stack):
        if isinstance(c, stacks.Chart):
            assert c.release == f"mp-{c.chart}", (
                f"{c.key}: release names are mp-<chart>: stable across upgrades, reserved to Modelplane"
            )


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_manifests_are_populated(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Manifests entry carries at least one manifest."""
    for c in stacks.join(cloud, stack):
        if isinstance(c, stacks.Manifests):
            assert c.manifests, f"{c.key}: a Manifests entry can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_multi_doc_manifests_derive_per_doc_keys(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """A multi-doc Manifests entry renders a key per doc, and anything else renders its own key."""
    for c in stacks.join(cloud, stack):
        keys = stacks.components.doc_keys(c)
        if isinstance(c, stacks.Chart) or len(c.manifests) == 1:
            assert keys == [c.key]
            continue
        assert keys == [f"{c.key}-{doc['metadata']['name']}" for doc in c.manifests], (
            f"{c.key}: a multi-doc bundle renders one Object per doc, keyed <key>-<name>"
        )


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_ready_entries_are_single_doc(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """A Manifests entry with a readiness query carries a single manifest."""
    # A readiness CEL query applies to every doc in an entry, so an
    # entry carrying one keeps to a single manifest - a Service or
    # ServiceAccount has no status conditions to satisfy it.
    for c in stacks.join(cloud, stack):
        if isinstance(c, stacks.Manifests) and c.ready is not None:
            assert len(c.manifests) == 1, c.key


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_depended_on_charts_wait(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Chart another component depends on sets wait."""
    # A chart another component depends on renders with helm --wait,
    # so its Ready means healthy and the install gate orders
    # dependents on health rather than deploy. Without this, the
    # gate would open the moment Helm accepted the manifests.
    joined = stacks.join(cloud, stack)
    depended_on = {dep for c in joined for dep in c.depends_on}
    for c in joined:
        if isinstance(c, stacks.Chart) and c.key in depended_on:
            assert c.wait, f"{c.key}: a depended-on chart must set wait"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_no_wildcard_tolerations(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """No component of a joined stack carries a keyless toleration."""

    # A keyless toleration tolerates every taint, so the pod lands
    # on tainted GPU nodes: control-plane charts squat on
    # accelerated capacity and their eviction stalls autoscaler
    # scale-down. aicr's bundler stamps exactly that wildcard on
    # every pod it renders; the generator scopes each one
    # (TOLERATIONS in generate.py). This pins that no keyless
    # toleration survives in any joined stack, chart values and
    # manifests alike.
    def check(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "tolerations" and isinstance(val, list):
                    for toleration in val:
                        assert isinstance(toleration, dict), f"keyless (wildcard) toleration in {where}"
                        assert "key" in toleration, f"keyless (wildcard) toleration in {where}"
                else:
                    check(val, where)
        elif isinstance(node, list):
            for item in node:
                check(item, where)

    for c in stacks.join(cloud, stack):
        check(c.values if isinstance(c, stacks.Chart) else c.manifests, c.key)


def test_unknown_cloud_and_stack_fail_closed() -> None:
    """join rejects an unknown cloud or stack."""
    # The Literal types reject these at type-checking time; this
    # exercises the runtime guard behind them, which catches the API
    # and the stacks package disagreeing on a value.
    with pytest.raises(ValueError, match="unknown cloud 'Mars'"):
        stacks.join("Mars", "Standard")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="unknown stack 'Turbo'"):
        stacks.join("Nebius", "Turbo")  # ty: ignore[invalid-argument-type]


def test_gpu_operator_switches_to_nvidia_driver_crd() -> None:
    """with_nvlink_disabled switches the gpu-operator chart to NVIDIADriver-CRD mode."""
    # The chart's default NVIDIADriver (deployDefaultCR) keeps driving
    # pools the transform doesn't name, so flipping modes changes
    # nothing for them.
    got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
    op = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
    assert op.values is not None
    assert op.values["driver"]["nvidiaDriverCRD"] == {"enabled": True, "deployDefaultCR": True}


def test_each_pool_gets_its_own_driver() -> None:
    """with_nvlink_disabled adds one NVIDIADriver per named pool, selecting that pool's nodes."""
    got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["pool-a", "pool-b"])
    drivers = [c for c in got if isinstance(c, stacks.Manifests) and c.key.startswith("nvlink-disabled-driver-")]
    assert [c.key for c in drivers] == ["nvlink-disabled-driver-pool-a", "nvlink-disabled-driver-pool-b"]
    for c, pool in zip(drivers, ["pool-a", "pool-b"], strict=True):
        # Ready entries keep to a single doc, and gate on the
        # operator-populated state so the DRA driver's install
        # gate orders on driver health.
        assert len(c.manifests) == 1, pool
        assert c.ready is not None, pool
        assert c.depends_on == ["gpu-operator", "nvlink-disable-config"], pool
        spec = c.manifests[0]["spec"]
        assert spec["nodeSelector"] == {"modelplane.ai/pool": pool}, pool
        assert spec["kernelModuleConfig"] == {"name": "nvidia-kernel-config"}, pool


def test_driver_pin_mirrors_the_chart() -> None:
    """A per-pool NVIDIADriver installs the same driver as the chart's default CR."""
    # One review moves both: the per-pool NVIDIADriver must install
    # the same driver the chart's default CR does.
    got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
    op = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
    driver = next(c for c in got if isinstance(c, stacks.Manifests) and c.key == "nvlink-disabled-driver-h100-pool")
    assert op.values is not None
    spec = driver.manifests[0]["spec"]
    assert spec["version"] == op.values["driver"]["version"]
    assert spec["useOpenKernelModules"] == op.values["driver"]["useOpenKernelModules"]


def test_configmap_carries_the_module_option() -> None:
    """with_nvlink_disabled adds the kernel module ConfigMap, in its own Namespace."""
    got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
    config = next(c for c in got if isinstance(c, stacks.Manifests) and c.key == "nvlink-disable-config")
    kinds = [doc["kind"] for doc in config.manifests]
    assert kinds == ["Namespace", "ConfigMap"]
    assert config.manifests[1]["data"] == {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"}


def test_dra_driver_gates_on_pool_drivers() -> None:
    """The DRA driver depends on every per-pool NVIDIADriver."""
    got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["pool-a", "pool-b"])
    dra = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu")
    assert dra.depends_on == ["gpu-operator", "nvlink-disabled-driver-pool-a", "nvlink-disabled-driver-pool-b"]


def test_join_is_not_mutated() -> None:
    """with_nvlink_disabled leaves the joined stack it was given unchanged."""
    # The transform must copy: the joined lists share the module-level
    # component objects, and mutating them would leak NVLink disable
    # into every later request.
    civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
    joined = stacks.join("Civo", "Standard")
    op = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
    dra = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu")
    assert op.values is not None
    assert "nvidiaDriverCRD" not in op.values["driver"]
    assert dra.depends_on == ["gpu-operator"]
