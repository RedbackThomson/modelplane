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

"""Tests for the DRA CEL selector module.

Pins Program.matches - the device activation shape, qualified-name domains,
unknown-domain handling, and quantity()/semver() dispatch - against upstream
DRA behavior (k8s.io/dynamic-resource-allocation/cel). Table-driven: each case
is one (selector expression, device, want).
"""

import dataclasses

import pytest
from function import cel


@dataclasses.dataclass
class Case:
    """A test case for matching a DRA CEL selector against a device."""

    name: str
    reason: str
    expr: str
    device: dict
    want: bool


def _hopper_gpu() -> dict:
    """A gpu.nvidia.com Hopper GPU with CUDA compute capability 9.5.3 and 141Gi of memory, on PCIe root pci0."""
    return {
        "driver": "gpu.nvidia.com",
        "attributes": {
            "architecture": {"string": "Hopper"},
            "cudaComputeCapability": {"version": "9.5.3"},
            "resource.kubernetes.io/pcieRoot": {"string": "pci0"},
        },
        "capacity": {"memory": {"value": "141Gi"}},
    }


def _scalar_device(*, x: dict) -> dict:
    """A gpu.nvidia.com device whose one attribute, x, is the given typed scalar."""
    return {"driver": "gpu.nvidia.com", "attributes": {"x": x}, "capacity": {}}


def _example_device(*, color: str, size: str) -> dict:
    """A resource-driver.example.com device, as in the DRA docs' examples."""
    return {
        "driver": "resource-driver.example.com",
        "attributes": {"color": {"string": color}, "size": {"string": size}},
        "capacity": {},
    }


MATCHES_CASES = [
    # driver.
    Case(
        name="DriverMatches",
        reason="A selector naming the device's driver matches.",
        expr='device.driver == "gpu.nvidia.com"',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="DriverMismatch",
        reason="A selector naming another driver doesn't match.",
        expr='device.driver == "nic.nvidia.com"',
        device=_hopper_gpu(),
        want=False,
    ),
    # Quantity comparison + methods.
    Case(
        name="QuantityCompareTo",
        reason="141Gi of GPU memory compares at least equal to 141Gi.",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="QuantityTooBig",
        reason="141Gi of GPU memory doesn't compare at least equal to 200Gi.",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) >= 0',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="QuantityIsGreaterThan",
        reason="141Gi of GPU memory is greater than 80Gi.",
        expr='device.capacity["gpu.nvidia.com"].memory.isGreaterThan(quantity("80Gi"))',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="QuantityIsLessThan",
        reason="141Gi of GPU memory is less than 200Gi.",
        expr='device.capacity["gpu.nvidia.com"].memory.isLessThan(quantity("200Gi"))',
        device=_hopper_gpu(),
        want=True,
    ),
    # Upstream sign is global-only, so q.sign() is a compile error there. This
    # pins the member form we accept anyway, a documented divergence in cel.py
    # that only makes us more permissive.
    Case(
        name="QuantitySignMember",
        reason="The member form of sign() finds 141Gi of GPU memory positive.",
        expr='device.capacity["gpu.nvidia.com"].memory.sign() == 1',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="QuantityAsInteger",
        reason="141Gi of GPU memory is the integer 151397597184.",
        expr='device.capacity["gpu.nvidia.com"].memory.asInteger() == 151397597184',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="QuantityIsInteger",
        reason="141Gi of GPU memory is an integer.",
        expr='device.capacity["gpu.nvidia.com"].memory.isInteger()',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="QuantityAdd",
        reason="141Gi of GPU memory plus 1Gi compares equal to 142Gi.",
        expr='device.capacity["gpu.nvidia.com"].memory.add(quantity("1Gi")).compareTo(quantity("142Gi")) == 0',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="IsQuantity",
        reason="isQuantity accepts 1.3Gi.",
        expr='isQuantity("1.3Gi")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="IsQuantityFalse",
        reason="isQuantity rejects 200K, because Kubernetes spells the kilo suffix as a lower-case k.",
        expr='isQuantity("200K")',
        device=_hopper_gpu(),
        want=False,
    ),
    # Semver comparison + methods.
    Case(
        name="SemverIsGreaterThan",
        reason="A CUDA compute capability of 9.5.3 is greater than 9.0.0.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.0.0"))',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="SemverNotGreater",
        reason="A CUDA compute capability of 9.5.3 isn't greater than 9.9.0.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.9.0"))',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="SemverMajor",
        reason="A CUDA compute capability of 9.5.3 has major version 9.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.major() == 9',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="SemverMinor",
        reason="A CUDA compute capability of 9.5.3 has minor version 5.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.minor() == 5',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="SemverPatch",
        reason="A CUDA compute capability of 9.5.3 has patch version 3.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.patch() == 3',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="SemverEquality",
        reason="A CUDA compute capability of 9.5.3 equals semver 9.5.3.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability == semver("9.5.3")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="IsSemver",
        reason="isSemver accepts 1.0.0.",
        expr='isSemver("1.0.0")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="IsSemverShort",
        reason="isSemver rejects 1.0, which has no patch version.",
        expr='isSemver("1.0")',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="IsSemverNormalize",
        reason="isSemver with normalize accepts 1.0, which has no patch version.",
        expr='isSemver("1.0", true)',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="SemverNormalize",
        reason="semver() with normalize parses v1.0 as major version 1.",
        expr='semver("v1.0", true).major() == 1',
        device=_hopper_gpu(),
        want=True,
    ),
    # Typed scalar attributes (resolve straight to the value, no .string).
    Case(
        name="StringAttribute",
        reason="A string attribute reads as its value, Hopper.",
        expr='device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="NonGPUDriverDomain",
        reason="A NIC's string attribute reads under its own driver's domain.",
        expr='device.attributes["nic.nvidia.com"].linkType == "infiniband"',
        device={"driver": "nic.nvidia.com", "attributes": {"linkType": {"string": "infiniband"}}, "capacity": {}},
        want=True,
    ),
    Case(
        name="BoolAttributeTrue",
        reason="A true bool attribute matches on its own.",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"bool": True}),
        want=True,
    ),
    Case(
        name="BoolAttributeFalse",
        reason="A false bool attribute doesn't match.",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"bool": False}),
        want=False,
    ),
    Case(
        name="IntAttribute",
        reason="An int attribute of 8 satisfies >= 8.",
        expr='device.attributes["gpu.nvidia.com"].x >= 8',
        device=_scalar_device(x={"int": 8}),
        want=True,
    ),
    Case(
        name="IntAttributeBelow",
        reason="An int attribute of 4 doesn't satisfy >= 8.",
        expr='device.attributes["gpu.nvidia.com"].x >= 8',
        device=_scalar_device(x={"int": 4}),
        want=False,
    ),
    # Qualified names split into their own domain.
    Case(
        name="QualifiedAttributeName",
        reason="The qualified attribute resource.kubernetes.io/pcieRoot reads under its own domain.",
        expr='device.attributes["resource.kubernetes.io"].pcieRoot == "pci0"',
        device=_hopper_gpu(),
        want=True,
    ),
    # The same input as StringAttribute, kept to mirror upstream's separate
    # driver-name-qualifier row.
    Case(
        name="DriverNameQualifier",
        reason="A bare attribute name reads under the device's driver domain.",
        expr='device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    # Non-matches that must not raise.
    Case(
        name="TwoPartVersion",
        reason="A version attribute of 9.0 isn't valid semver, so the selector doesn't match rather than raising.",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("8.0.0"))',
        device={
            "driver": "gpu.nvidia.com",
            "attributes": {"cudaComputeCapability": {"version": "9.0"}},
            "capacity": {},
        },
        want=False,
    ),
    # 10Mo isn't a valid quantity, but 10M wouldn't match this selector either,
    # so a parser that read 10Mo as 10M would pass too.
    Case(
        name="MalformedQuantity",
        reason="A selector for at least 1Gi of memory doesn't match a capacity of 10Mo, and doesn't raise.",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("1Gi")) >= 0',
        device={"driver": "gpu.nvidia.com", "attributes": {}, "capacity": {"memory": {"value": "10Mo"}}},
        want=False,
    ),
    Case(
        name="UnknownAttribute",
        reason="Reading an attribute the device lacks doesn't match rather than raising.",
        expr='device.attributes["gpu.nvidia.com"].nope == "x"',
        device=_hopper_gpu(),
        want=False,
    ),
    # A non-bool selector must not spuriously match. Upstream rejects it
    # at compile time; we treat a non-bool result as a non-match.
    Case(
        name="NonBoolString",
        reason="A selector that evaluates to a string doesn't match.",
        expr='"5"',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="NonBoolInt",
        reason="A selector that evaluates to an int doesn't match.",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"int": 5}),
        want=False,
    ),
    # Domain presence. Upstream's domain-presence idiom is "<domain>" in
    # device.attributes, not has(device.attributes["<domain>"]): cel-go's
    # has() macro rejects an index argument, so the has() form is a
    # compile error on a real cluster (celpy accepts it - see cel.py's
    # documented divergences). An unknown domain is simply absent (False),
    # not present-but-empty.
    Case(
        name="DomainCheckNegative",
        reason="A domain the device has no attributes under isn't in device.attributes.",
        expr='"other.com" in device.attributes',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="DomainCheckPositive",
        reason="The device's driver domain is in device.attributes.",
        expr='"gpu.nvidia.com" in device.attributes',
        device=_hopper_gpu(),
        want=True,
    ),
    # Upstream errors on the id, not the domain. matches() turns either error
    # into a non-match.
    Case(
        name="UnknownDomainAttribute",
        reason="Reading an attribute under a domain the device lacks doesn't match rather than raising.",
        expr='device.attributes["other.com"].x == "y"',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="GuardedDomain",
        reason="A domain read guarded by the in idiom matches when the domain is present.",
        expr='"gpu.nvidia.com" in device.attributes && device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="DesignSelector",
        reason="The design's selector, compute capability above 9.0.0 and at least 141Gi of memory, matches a Hopper GPU.",
        expr=(
            'device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.0.0")) && '
            'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0'
        ),
        device=_hopper_gpu(),
        want=True,
    ),
    # Verbatim selector examples from the DRA docs (the k8s.io concept page
    # and the allocate-devices-dra task page), each against a device that
    # should match, and all but small-white against one that shouldn't.
    Case(
        name="DocsLargeBlack",
        reason="The docs' large-black selector matches a large black device.",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "black" && '
            'device.attributes["resource-driver.example.com"].size == "large"'
        ),
        device=_example_device(color="black", size="large"),
        want=True,
    ),
    Case(
        name="DocsLargeBlackRejects",
        reason="The docs' large-black selector rejects a small white device.",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "black" && '
            'device.attributes["resource-driver.example.com"].size == "large"'
        ),
        device=_example_device(color="white", size="small"),
        want=False,
    ),
    Case(
        name="DocsSmallWhite",
        reason="The docs' small-white selector matches a small white device.",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "white" && '
            'device.attributes["resource-driver.example.com"].size == "small"'
        ),
        device=_example_device(color="white", size="small"),
        want=True,
    ),
    Case(
        name="DocsDeviceClass",
        reason="The docs' extended-resource DeviceClass selector matches a gpu.example.com GPU.",
        expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
        device={"driver": "gpu.example.com", "attributes": {"type": {"string": "gpu"}}, "capacity": {}},
        want=True,
    ),
    Case(
        name="DocsDeviceClassRejects",
        reason="The docs' extended-resource DeviceClass selector rejects a device of another driver.",
        expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
        device={"driver": "nic.nvidia.com", "attributes": {"linkType": {"string": "infiniband"}}, "capacity": {}},
        want=False,
    ),
    Case(
        name="DocsResourceClaim",
        reason="The docs' ResourceClaim selector for a 64Gi GPU matches a GPU with 64Gi of memory.",
        expr=(
            'device.attributes["driver.example.com"].type == "gpu" && '
            'device.capacity["driver.example.com"].memory == quantity("64Gi")'
        ),
        device={
            "driver": "driver.example.com",
            "attributes": {"type": {"string": "gpu"}},
            "capacity": {"memory": {"value": "64Gi"}},
        },
        want=True,
    ),
    Case(
        name="DocsResourceClaimRejects",
        reason="The docs' ResourceClaim selector for a 64Gi GPU rejects a GPU with 32Gi of memory.",
        expr=(
            'device.attributes["driver.example.com"].type == "gpu" && '
            'device.capacity["driver.example.com"].memory == quantity("64Gi")'
        ),
        device={
            "driver": "driver.example.com",
            "attributes": {"type": {"string": "gpu"}},
            "capacity": {"memory": {"value": "32Gi"}},
        },
        want=False,
    ),
]


@pytest.mark.parametrize("case", MATCHES_CASES, ids=lambda case: case.name)
def test_matches(case: Case) -> None:
    """A DRA CEL selector matches a device as it does upstream."""
    got = cel.Program(case.expr).matches(case.device)
    assert got == case.want, case.reason


def test_compile_error() -> None:
    """A malformed expression fails to compile."""
    with pytest.raises(cel.CELCompileError, match=r"not \) valid \("):
        cel.Program("not ) valid (")
