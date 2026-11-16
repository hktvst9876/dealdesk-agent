"""Exact money and probability arithmetic.

All amounts are Decimal. Floats never touch a price: they only appear inside
probability maths (Poisson tails) and are converted back to Decimal at 4 dp.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, getcontext
from typing import Iterable, Union

getcontext().prec = 28

Number = Union[Decimal, int, float, str]

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: Number | None) -> Decimal:
    """Convert to Decimal without float noise (floats go through str)."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def cents(value: Number) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def one_dp(value: Number) -> Decimal:
    return D(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def whole(value: Number) -> Decimal:
    """Display rounding used in the spec tables: half-up to whole currency units."""
    return D(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def prob(value: Number) -> Decimal:
    p = D(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return min(ONE, max(ZERO, p))


def pct(value: Number) -> str:
    """0.4526 -> '45.3%'"""
    return f"{one_dp(D(value) * 100)}%"


def total(values: Iterable[Number]) -> Decimal:
    s = ZERO
    for v in values:
        s += D(v)
    return s


def fmt_money(value: Number, symbol: str = "$", whole_units: bool = True) -> str:
    v = D(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if whole_units or v == v.to_integral_value():
        return f"{sign}{symbol}{whole(v):,}"
    return f"{sign}{symbol}{cents(v):,}"
