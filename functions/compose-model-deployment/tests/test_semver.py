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

"""Tests for the semver module.

The cases mirror upstream's semver CEL surface so we catch regressions against
it: every example from the semverlib.go doc comment and every applicable case
from k8s.io/apiserver/pkg/cel/library/semver_test.go is represented as a row.

Expressions are evaluated end to end through a compiled CEL selector (the device
activation is built but unused). Value-returning expressions are wrapped in a
`== <want>` comparison so each case asserts a single bool.

Upstream cases that don't apply: the compile-time overload error
(isSemver([1,2,3])) - celpy doesn't type-check overloads; and the runtime parse
error for semver("v1.0") - upstream raises, we treat a bad version as a
non-match (driven through the parse layer in test_parse_rejects).
"""

import dataclasses

import pytest
from function import cel, semver


@dataclasses.dataclass
class SemverCase:
    """A test case for a semver CEL expression."""

    name: str
    reason: str
    expr: str
    want: bool


@dataclasses.dataclass
class ParseRejectsCase:
    """A test case for a version string semver.parse rejects."""

    name: str
    reason: str
    s: str
    want: str


SEMVER_CASES = [
    # parse + doc-comment examples. Upstream's parse row returns the version.
    # Wrapped to return a bool, it matches CompareEqual below. Both are kept
    # to mirror upstream's table.
    SemverCase(
        name="Parse",
        reason="semver() parses 1.2.3 into a version that compares equal to 1.2.3.",
        expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0',
        want=True,
    ),
    SemverCase(
        name="ParsePrerelease",
        reason="semver() parses 0.1.0-alpha.1, prerelease and all, as major version 0.",
        expr='semver("0.1.0-alpha.1").major() == 0',
        want=True,
    ),
    # isSemver strict.
    SemverCase(
        name="IsSemver",
        reason="isSemver accepts a version with a prerelease and build metadata.",
        expr='isSemver("1.2.3-beta.1+build.1")',
        want=True,
    ),
    SemverCase(
        name="IsSemverSimple",
        reason="isSemver accepts 1.0.0.",
        expr='isSemver("1.0.0")',
        want=True,
    ),
    SemverCase(
        name="IsSemverWord",
        reason="isSemver rejects the word hello.",
        expr='isSemver("hello")',
        want=False,
    ),
    SemverCase(
        name="IsSemverEmpty",
        reason="isSemver rejects an empty string.",
        expr='isSemver("")',
        want=False,
    ),
    SemverCase(
        name="IsSemverVPrefix",
        reason="isSemver rejects v1.0.0, because of its v prefix.",
        expr='isSemver("v1.0.0")',
        want=False,
    ),
    SemverCase(
        name="IsSemverShortPrefixed",
        reason="isSemver rejects v1.0, which has a v prefix and no patch version.",
        expr='isSemver("v1.0")',
        want=False,
    ),
    SemverCase(
        name="IsSemverLeadingWhitespace",
        reason="isSemver rejects a version with leading whitespace.",
        expr='isSemver(" 1.0.0")',
        want=False,
    ),
    SemverCase(
        name="IsSemverContainsWhitespace",
        reason="isSemver rejects a version with whitespace inside it.",
        expr='isSemver("1. 0.0")',
        want=False,
    ),
    SemverCase(
        name="IsSemverTrailingWhitespace",
        reason="isSemver rejects a version with trailing whitespace.",
        expr='isSemver("1.0.0 ")',
        want=False,
    ),
    SemverCase(
        name="IsSemverLeadingZeros",
        reason="isSemver rejects 01.01.01, because of its leading zeros.",
        expr='isSemver("01.01.01")',
        want=False,
    ),
    SemverCase(
        name="IsSemverMajorOnly",
        reason="isSemver rejects 1, which has no minor or patch version.",
        expr='isSemver("1")',
        want=False,
    ),
    SemverCase(
        name="IsSemverNoPatch",
        reason="isSemver rejects 1.1, which has no patch version.",
        expr='isSemver("1.1")',
        want=False,
    ),
    SemverCase(
        name="IsSemverQuantity",
        reason="isSemver rejects the quantity 200K.",
        expr='isSemver("200K")',
        want=False,
    ),
    SemverCase(
        name="IsSemverBareSuffix",
        reason="isSemver rejects Mi, a bare quantity suffix.",
        expr='isSemver("Mi")',
        want=False,
    ),
    # isSemver normalize overload. Normalization does NOT trim whitespace.
    SemverCase(
        name="NormalizeEmpty",
        reason="isSemver with normalize rejects an empty string.",
        expr='isSemver("", true)',
        want=False,
    ),
    SemverCase(
        name="NormalizeLeadingWhitespace",
        reason="isSemver with normalize rejects a version with leading whitespace.",
        expr='isSemver(" 1.0.0", true)',
        want=False,
    ),
    SemverCase(
        name="NormalizeContainsWhitespace",
        reason="isSemver with normalize rejects a version with whitespace inside it.",
        expr='isSemver("1. 0.0", true)',
        want=False,
    ),
    SemverCase(
        name="NormalizeTrailingWhitespace",
        reason="isSemver with normalize rejects a version with trailing whitespace.",
        expr='isSemver("1.0.0 ", true)',
        want=False,
    ),
    SemverCase(
        name="NormalizeVPrefix",
        reason="isSemver with normalize accepts v1.0.0, v prefix and all.",
        expr='isSemver("v1.0.0", true)',
        want=True,
    ),
    SemverCase(
        name="NormalizeLeadingZeros",
        reason="isSemver with normalize accepts 01.01.01, leading zeros and all.",
        expr='isSemver("01.01.01", true)',
        want=True,
    ),
    SemverCase(
        name="NormalizeMajorOnly",
        reason="isSemver with normalize accepts 1, a major version alone.",
        expr='isSemver("1", true)',
        want=True,
    ),
    SemverCase(
        name="NormalizeNoPatch",
        reason="isSemver with normalize accepts 1.1, which has no patch version.",
        expr='isSemver("1.1", true)',
        want=True,
    ),
    # normalize equality and semver(...) examples.
    SemverCase(
        name="EqualityNormalize",
        reason="v01.01, normalized, equals 1.1.0.",
        expr='semver("v01.01", true) == semver("1.1.0")',
        want=True,
    ),
    SemverCase(
        name="SemverNormalizeVPrefix",
        reason="semver() with normalize parses v1.0.0 as major version 1.",
        expr='semver("v1.0.0", true).major() == 1',
        want=True,
    ),
    SemverCase(
        name="SemverNormalizeNoPatch",
        reason="semver() with normalize parses 1.0 as patch version 0.",
        expr='semver("1.0", true).patch() == 0',
        want=True,
    ),
    SemverCase(
        name="SemverNormalizeLeadingZeros",
        reason="semver() with normalize parses 01.01.01 as minor version 1.",
        expr='semver("01.01.01", true).minor() == 1',
        want=True,
    ),
    # equality / comparison.
    SemverCase(
        name="EqualityReflexivity",
        reason="1.2.3 equals itself.",
        expr='semver("1.2.3") == semver("1.2.3")',
        want=True,
    ),
    SemverCase(
        name="Inequality",
        reason="1.2.3 doesn't equal 1.0.0.",
        expr='semver("1.2.3") == semver("1.0.0")',
        want=False,
    ),
    SemverCase(
        name="IsLessThan",
        reason="1.0.0 is less than 1.2.3.",
        expr='semver("1.0.0").isLessThan(semver("1.2.3"))',
        want=True,
    ),
    SemverCase(
        name="IsLessThanFalse",
        reason="1.0.0 isn't less than itself.",
        expr='semver("1.0.0").isLessThan(semver("1.0.0"))',
        want=False,
    ),
    SemverCase(
        name="IsGreaterThan",
        reason="1.2.3 is greater than 1.0.0.",
        expr='semver("1.2.3").isGreaterThan(semver("1.0.0"))',
        want=True,
    ),
    SemverCase(
        name="IsGreaterThanFalse",
        reason="1.0.0 isn't greater than itself.",
        expr='semver("1.0.0").isGreaterThan(semver("1.0.0"))',
        want=False,
    ),
    SemverCase(
        name="CompareEqual",
        reason="1.2.3 compares equal to itself.",
        expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0',
        want=True,
    ),
    SemverCase(
        name="CompareLess",
        reason="1.2.3 compares less than 2.0.0.",
        expr='semver("1.2.3").compareTo(semver("2.0.0")) == -1',
        want=True,
    ),
    SemverCase(
        name="CompareGreater",
        reason="1.2.3 compares greater than 0.1.2.",
        expr='semver("1.2.3").compareTo(semver("0.1.2")) == 1',
        want=True,
    ),
    # major / minor / patch.
    SemverCase(
        name="Major",
        reason="1.2.3 has major version 1.",
        expr='semver("1.2.3").major() == 1',
        want=True,
    ),
    SemverCase(
        name="Minor",
        reason="1.2.3 has minor version 2.",
        expr='semver("1.2.3").minor() == 2',
        want=True,
    ),
    SemverCase(
        name="Patch",
        reason="1.2.3 has patch version 3.",
        expr='semver("1.2.3").patch() == 3',
        want=True,
    ),
    SemverCase(
        name="ParseInvalidVersion",
        reason="Reading the major version of v1.0, which isn't valid, doesn't match; upstream raises instead.",
        expr='semver("v1.0").major() == 1',
        want=False,
    ),
]


@pytest.mark.parametrize("case", SEMVER_CASES, ids=lambda case: case.name)
def test_semver(case: SemverCase) -> None:
    """A semver CEL expression evaluates as it does upstream."""
    got = cel.Program(case.expr).matches({})
    assert got == case.want, case.reason


PARSE_REJECTS_CASES = [
    ParseRejectsCase(
        name="VPrefixNoPatch",
        reason="parse rejects v1.0, which has only two parts, before it reads the v prefix.",
        s="v1.0",
        want=r"no Major\.Minor\.Patch elements found",
    ),
    ParseRejectsCase(
        name="MajorOnly",
        reason="parse rejects 1, which has no minor or patch version.",
        s="1",
        want=r"no Major\.Minor\.Patch elements found",
    ),
    ParseRejectsCase(
        name="NoPatch",
        reason="parse rejects 1.1, which has no patch version.",
        s="1.1",
        want=r"no Major\.Minor\.Patch elements found",
    ),
    ParseRejectsCase(
        name="LeadingZeros",
        reason="parse rejects 01.01.01, because its major version has a leading zero.",
        s="01.01.01",
        want="major number must not contain leading zeroes: '01'",
    ),
    ParseRejectsCase(
        name="LeadingWhitespace",
        reason="parse rejects a version with leading whitespace.",
        s=" 1.0.0",
        want=r"invalid character\(s\) in major number: ' 1'",
    ),
    ParseRejectsCase(
        name="TrailingWhitespace",
        reason="parse rejects a version with trailing whitespace.",
        s="1.0.0 ",
        want=r"invalid character\(s\) in patch number: '0 '",
    ),
    ParseRejectsCase(
        name="Empty",
        reason="parse rejects an empty string.",
        s="",
        want="version string empty",
    ),
    ParseRejectsCase(
        name="Word",
        reason="parse rejects the word hello.",
        s="hello",
        want=r"no Major\.Minor\.Patch elements found",
    ),
]


@pytest.mark.parametrize("case", PARSE_REJECTS_CASES, ids=lambda case: case.name)
def test_parse_rejects(case: ParseRejectsCase) -> None:
    """parse() (strict) rejects what blang/semver Parse rejects."""
    with pytest.raises(ValueError, match=case.want):
        semver.parse(case.s)
