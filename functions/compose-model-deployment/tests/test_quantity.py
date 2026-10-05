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

"""Tests for the quantity module.

The cases mirror upstream's quantity CEL surface so we catch regressions against
it: every example from the quantity.go doc comment and every applicable case
from k8s.io/apiserver/pkg/cel/library/quantity_test.go is represented as a row.

Expressions are evaluated end to end through a compiled CEL selector (the device
activation is built but unused), so each case reads like the CEL a user writes.
Value-returning expressions are wrapped in a `== <want>` comparison so the case
asserts a single bool, matching how the upstream table asserts equality.

A few upstream cases don't apply to our reimplementation and are noted inline:
compile-time overload errors (isQuantity([1,2,3])) - celpy doesn't type-check
overloads; and runtime-error cases (an invalid suffix, integer overflow) which
upstream raises but we treat as a non-match (a CEL eval error -> matches() is
False), exercised here through the parse layer and the selector layer.

These expected values come from running the inputs through the real Kubernetes
code, not from assertion by hand. When you change the quantity module or want to
add a case, derive its expected value with the parity oracle in ./oracle (see
the package comment in oracle/main.go) rather than reasoning about it -
upstream has surprises (e.g. binary-suffix overflow saturates to int64-max, so
8Ei == 10Ei).
"""

import dataclasses

import pytest
from function import cel, quantity


@dataclasses.dataclass
class QuantityCase:
    """A test case for a quantity CEL expression."""

    name: str
    reason: str
    expr: str
    want: bool


@dataclasses.dataclass
class ParseRejectsCase:
    """A test case for a string quantity.parse rejects."""

    name: str
    reason: str
    s: str
    want: str


QUANTITY_CASES = [
    # parse + isQuantity.
    QuantityCase(
        name="Parse",
        reason="quantity() parses 12Mi into a value that compares equal to 12Mi.",
        expr='quantity("12Mi").compareTo(quantity("12Mi")) == 0',
        want=True,
    ),
    QuantityCase(
        name="IsQuantity",
        reason="isQuantity accepts the plain integer 20.",
        expr='isQuantity("20")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityMegabytes",
        reason="isQuantity accepts 20M.",
        expr='isQuantity("20M")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityMebibytes",
        reason="isQuantity accepts 20Mi.",
        expr='isQuantity("20Mi")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityInvalidSuffix",
        reason="isQuantity rejects 20Mo, whose suffix is invalid.",
        expr='isQuantity("20Mo")',
        want=False,
    ),
    QuantityCase(
        name="IsQuantityPassingRegex",
        reason="isQuantity rejects 10Mm, which passes resource.Quantity's split regex but has no valid suffix.",
        expr='isQuantity("10Mm")',
        want=False,
    ),
    # resource.Quantity accepts decimal exponents and nano/micro suffixes.
    QuantityCase(
        name="IsQuantityExponent",
        reason="isQuantity accepts 256e3, with a lower-case decimal exponent.",
        expr='isQuantity("256e3")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityUpperExponent",
        reason="isQuantity accepts 1E3, with an upper-case decimal exponent.",
        expr='isQuantity("1E3")',
        want=True,
    ),
    QuantityCase(
        name="ExponentValue",
        reason="256e3 compares equal to 256000.",
        expr='quantity("256e3").compareTo(quantity("256000")) == 0',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityNano",
        reason="isQuantity accepts 100n, with the nano suffix.",
        expr='isQuantity("100n")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityMicro",
        reason="isQuantity accepts 100u, with the micro suffix.",
        expr='isQuantity("100u")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityTrailingDot",
        reason="isQuantity accepts 5., with a trailing dot.",
        expr='isQuantity("5.")',
        want=True,
    ),
    # The quantity() constructor does NOT trim whitespace.
    QuantityCase(
        name="IsQuantityLeadingWhitespace",
        reason="isQuantity rejects a quantity with leading whitespace.",
        expr='isQuantity(" 5Gi")',
        want=False,
    ),
    QuantityCase(
        name="IsQuantityTrailingWhitespace",
        reason="isQuantity rejects a quantity with trailing whitespace.",
        expr='isQuantity("5Gi ")',
        want=False,
    ),
    # resource.ParseQuantity rounds up to nano.
    QuantityCase(
        name="NanoRounding",
        reason="0.0000000004 and 0.000000001, equal at nano resolution, compare equal.",
        expr='quantity("0.0000000004").compareTo(quantity("0.000000001")) == 0',
        want=True,
    ),
    # doc-comment isQuantity examples.
    QuantityCase(
        name="IsQuantityDecimalG",
        reason="isQuantity accepts 1.3G.",
        expr='isQuantity("1.3G")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityDecimalGi",
        reason="isQuantity accepts 1.3Gi.",
        expr='isQuantity("1.3Gi")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityComma",
        reason="isQuantity rejects 1,3G, which has a comma for a decimal point.",
        expr='isQuantity("1,3G")',
        want=False,
    ),
    QuantityCase(
        name="IsQuantityKilo",
        reason="isQuantity accepts 10000k.",
        expr='isQuantity("10000k")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityCapitalK",
        reason="isQuantity rejects 200K, because Kubernetes spells the kilo suffix as a lower-case k.",
        expr='isQuantity("200K")',
        want=False,
    ),
    QuantityCase(
        name="IsQuantityWord",
        reason="isQuantity rejects the word Three.",
        expr='isQuantity("Three")',
        want=False,
    ),
    QuantityCase(
        name="IsQuantityBareSuffix",
        reason="isQuantity rejects Mi, a bare suffix.",
        expr='isQuantity("Mi")',
        want=False,
    ),
    # equality.
    QuantityCase(
        name="EqualityReflexivity",
        reason="200M equals itself.",
        expr='quantity("200M") == quantity("200M")',
        want=True,
    ),
    QuantityCase(
        name="EqualitySymmetry",
        reason="200M equals 0.2G, and 0.2G equals 200M.",
        expr='quantity("200M") == quantity("0.2G") && quantity("0.2G") == quantity("200M")',
        want=True,
    ),
    QuantityCase(
        name="EqualityTransitivity",
        reason="2M, 0.002G and 2000k all equal one another.",
        expr=(
            'quantity("2M") == quantity("0.002G") && quantity("2000k") == quantity("2M") && '
            'quantity("0.002G") == quantity("2000k")'
        ),
        want=True,
    ),
    QuantityCase(
        name="Inequality",
        reason="200M doesn't equal 0.3G.",
        expr='quantity("200M") == quantity("0.3G")',
        want=False,
    ),
    # isLessThan / isGreaterThan.
    QuantityCase(
        name="IsLessThan",
        reason="50M is less than 50Mi.",
        expr='quantity("50M").isLessThan(quantity("50Mi"))',
        want=True,
    ),
    QuantityCase(
        name="IsLessThanObvious",
        reason="50M is less than 100M.",
        expr='quantity("50M").isLessThan(quantity("100M"))',
        want=True,
    ),
    QuantityCase(
        name="IsLessThanFalse",
        reason="100M isn't less than 50M.",
        expr='quantity("100M").isLessThan(quantity("50M"))',
        want=False,
    ),
    QuantityCase(
        name="IsGreaterThan",
        reason="50Mi is greater than 50M.",
        expr='quantity("50Mi").isGreaterThan(quantity("50M"))',
        want=True,
    ),
    QuantityCase(
        name="IsGreaterThanObvious",
        reason="150Mi is greater than 100Mi.",
        expr='quantity("150Mi").isGreaterThan(quantity("100Mi"))',
        want=True,
    ),
    QuantityCase(
        name="IsGreaterThanFalse",
        reason="50M isn't greater than 100M.",
        expr='quantity("50M").isGreaterThan(quantity("100M"))',
        want=False,
    ),
    # compareTo.
    QuantityCase(
        name="CompareEqual",
        reason="200M compares equal to 0.2G.",
        expr='quantity("200M").compareTo(quantity("0.2G")) == 0',
        want=True,
    ),
    QuantityCase(
        name="CompareLess",
        reason="50M compares less than 50Mi.",
        expr='quantity("50M").compareTo(quantity("50Mi")) == -1',
        want=True,
    ),
    QuantityCase(
        name="CompareGreater",
        reason="50Mi compares greater than 50M.",
        expr='quantity("50Mi").compareTo(quantity("50M")) == 1',
        want=True,
    ),
    # add / sub (quantity and int overloads).
    QuantityCase(
        name="AddQuantity",
        reason="50k plus the quantity 20 equals 50.02k.",
        expr='quantity("50k").add(quantity("20")) == quantity("50.02k")',
        want=True,
    ),
    QuantityCase(
        name="AddInt",
        reason="50k plus the int 20 isn't less than 50020.",
        expr='quantity("50k").add(20).isLessThan(quantity("50020"))',
        want=False,
    ),
    QuantityCase(
        name="SubQuantity",
        reason="50k minus the quantity 20 equals 49.98k.",
        expr='quantity("50k").sub(quantity("20")) == quantity("49.98k")',
        want=True,
    ),
    QuantityCase(
        name="SubInt",
        reason="50k minus the int 20 equals 49980.",
        expr='quantity("50k").sub(20) == quantity("49980")',
        want=True,
    ),
    QuantityCase(
        name="ArithChain",
        reason="50k plus 20 minus 100k is the integer -49980.",
        expr='quantity("50k").add(20).sub(quantity("100k")).asInteger() == -49980',
        want=True,
    ),
    QuantityCase(
        name="ArithChainLonger",
        reason="50k plus 20 minus 100k minus -50000 is the integer 20.",
        expr='quantity("50k").add(20).sub(quantity("100k")).sub(-50000).asInteger() == 20',
        want=True,
    ),
    # sign (doc comment). Upstream declares sign as a GLOBAL function
    # (sign(q)), not a member (q.sign()); the global form is the parity
    # surface. celpy can't tell the two call styles apart, so we accept
    # both, but the test asserts the upstream-correct global form (see
    # cel.py's documented divergences).
    QuantityCase(
        name="SignPositive",
        reason="The sign of 50k is 1.",
        expr='sign(quantity("50k")) == 1',
        want=True,
    ),
    QuantityCase(
        name="SignNegative",
        reason="The sign of -50k is -1.",
        expr='sign(quantity("-50k")) == -1',
        want=True,
    ),
    QuantityCase(
        name="SignZero",
        reason="The sign of 0 is 0.",
        expr='sign(quantity("0")) == 0',
        want=True,
    ),
    # Binary-suffix overflow saturates to int64-max, keeping sign, so
    # 8Ei/10Ei/100Ei all compare equal to int64-max (resource.Quantity
    # stores BinarySI in an int64). Confirmed against resource.Quantity.
    QuantityCase(
        name="EiSaturates",
        reason="8Ei saturates, comparing equal to the int64 maximum.",
        expr='quantity("8Ei").compareTo(quantity("9223372036854775807")) == 0',
        want=True,
    ),
    QuantityCase(
        name="SaturatedEiEqual",
        reason="8Ei and 10Ei both saturate, so they compare equal.",
        expr='quantity("8Ei").compareTo(quantity("10Ei")) == 0',
        want=True,
    ),
    QuantityCase(
        name="LargerEiEqual",
        reason="10Ei and 100Ei both saturate, so they compare equal.",
        expr='quantity("10Ei").compareTo(quantity("100Ei")) == 0',
        want=True,
    ),
    QuantityCase(
        name="NegativeEiSaturates",
        reason="-10Ei saturates, keeping its sign, to compare equal to minus the int64 maximum.",
        expr='quantity("-10Ei").compareTo(quantity("-9223372036854775807")) == 0',
        want=True,
    ),
    QuantityCase(
        name="EiBelowSaturation",
        reason="7Ei is below the int64 maximum, so it doesn't saturate and is less than 8Ei.",
        expr='quantity("7Ei").isLessThan(quantity("8Ei"))',
        want=True,
    ),
    # Large DECIMAL-path values do not saturate (only the binary path
    # does) and must not raise on nano-rounding. isQuantity must be true
    # and the value must round-trip.
    QuantityCase(
        name="IsQuantityLargeExa",
        reason="isQuantity accepts 256E, a decimal quantity too large for int64.",
        expr='isQuantity("256E")',
        want=True,
    ),
    QuantityCase(
        name="IsQuantityExa",
        reason="isQuantity accepts 10E.",
        expr='isQuantity("10E")',
        want=True,
    ),
    QuantityCase(
        name="LargeExaValue",
        reason="256E compares equal to its full value, unsaturated.",
        expr='quantity("256E").compareTo(quantity("256000000000000000000")) == 0',
        want=True,
    ),
    QuantityCase(
        name="LargeExaGreater",
        reason="256E is greater than 1Ei.",
        expr='quantity("256E").isGreaterThan(quantity("1Ei"))',
        want=True,
    ),
    # asInteger / isInteger.
    QuantityCase(
        name="AsInteger",
        reason="50k as an integer is 50000.",
        expr='quantity("50k").asInteger() == 50000',
        want=True,
    ),
    QuantityCase(
        name="IsInteger",
        reason="50 is an integer.",
        expr='quantity("50").isInteger()',
        want=True,
    ),
    QuantityCase(
        name="IsIntegerBig",
        reason="50000000G is an integer.",
        expr='quantity("50000000G").isInteger()',
        want=True,
    ),
    QuantityCase(
        name="IsIntegerOverflow",
        reason="A quantity too large for int64 isn't an integer.",
        expr='quantity("9999999999999999999999999999999999999G").isInteger()',
        want=False,
    ),
    QuantityCase(
        name="AsIntegerError",
        reason="Converting a quantity too large for int64 to an integer doesn't match; upstream raises instead.",
        expr='quantity("9999999999999999999999999999999999999G").asInteger() > 0',
        want=False,
    ),
    # asApproximateFloat.
    QuantityCase(
        name="AsFloat",
        reason="50.703k as an approximate float is 50703.0.",
        expr='quantity("50.703k").asApproximateFloat() == 50703.0',
        want=True,
    ),
    # isGreaterThan is a member method upstream accepts, so the non-match is
    # the parse failure, not a rejected call form.
    QuantityCase(
        name="InvalidSuffix",
        reason="Comparing 10Mo, whose suffix is invalid, doesn't match; upstream raises instead.",
        expr='quantity("10Mo").isGreaterThan(quantity("1"))',
        want=False,
    ),
]


@pytest.mark.parametrize("case", QUANTITY_CASES, ids=lambda case: case.name)
def test_quantity(case: QuantityCase) -> None:
    """A quantity CEL expression evaluates as it does upstream."""
    got = cel.Program(case.expr).matches({})
    assert got == case.want, case.reason


PARSE_REJECTS_CASES = [
    ParseRejectsCase(
        name="InvalidSuffix",
        reason="parse rejects 10Mo, whose suffix is invalid.",
        s="10Mo",
        want="invalid quantity: '10Mo'",
    ),
    ParseRejectsCase(
        name="PassingRegex",
        reason="parse rejects 10Mm, which passes resource.Quantity's split regex but has no valid suffix.",
        s="10Mm",
        want="invalid quantity: '10Mm'",
    ),
    ParseRejectsCase(
        name="CapitalK",
        reason="parse rejects 200K, because Kubernetes spells the kilo suffix as a lower-case k.",
        s="200K",
        want="invalid quantity: '200K'",
    ),
    ParseRejectsCase(
        name="Comma",
        reason="parse rejects 1,3G, which has a comma for a decimal point.",
        s="1,3G",
        want="invalid quantity: '1,3G'",
    ),
    ParseRejectsCase(
        name="Word",
        reason="parse rejects the word Three.",
        s="Three",
        want="invalid quantity: 'Three'",
    ),
    # A DELIBERATE divergence, not parity: upstream parses most bare suffixes
    # as 0 but inconsistently errors on a few (see parse()'s docstring). We
    # reject every bare suffix; no device capacity is ever a bare suffix.
    ParseRejectsCase(
        name="BareSuffix",
        reason="parse rejects Mi, a bare suffix, where upstream parses most bare suffixes as 0.",
        s="Mi",
        want="invalid quantity: 'Mi'",
    ),
    ParseRejectsCase(
        name="Empty",
        reason="parse rejects an empty string.",
        s="",
        want="invalid quantity: ''",
    ),
]


@pytest.mark.parametrize("case", PARSE_REJECTS_CASES, ids=lambda case: case.name)
def test_parse_rejects(case: ParseRejectsCase) -> None:
    """parse() rejects what resource.Quantity rejects, plus every bare suffix, a deliberate divergence."""
    with pytest.raises(ValueError, match=case.want):
        quantity.parse(case.s)
