"""Opportunity cost: what the deal uses up is worth in its next-best use.

One function per kind of scarce thing. Each returns (amount, displaced lines) so
every figure in a staff rationale or escalation packet traces back to an input.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..money import ONE, ZERO, D, prob
from ..profile.schema import BusinessProfile, Product, Resource
from .calendar_math import Occurrence, busy_units, overlapping_occurrences, utilisation
from .types import CalendarView, DealContext, DisplacedItem, Placement


# --------------------------------------------------------------------------- P(resell)

def p_resell(profile: BusinessProfile, lead_time_hours: Decimal, util: Decimal) -> Decimal:
    """Deal Policy matrix lookup (lead time x forecast utilisation)."""
    rows = profile.resell_matrix
    if not rows:
        return D(profile.policy.p_resell_default)
    for r in rows:
        if r["lead_min_h"] <= lead_time_hours < r["lead_max_h"] and r["util_min"] <= util < r["util_max"]:
            return D(r["p_resell"])
    # Out of the grid (e.g. utilisation exactly 1.0): use the closest cell by bounds.
    best = min(rows, key=lambda r: (abs(min(lead_time_hours, Decimal(10**6)) - r["lead_min_h"]) + abs(util - r["util_min"]) * 1000))
    return D(best["p_resell"])


def _lookup(table: dict[str, Decimal], occ: Occurrence) -> Optional[Decimal]:
    for key in (f"{occ.start.date().isoformat()}:{occ.session.id}", f"{occ.day}:{occ.session.id}",
                f"{occ.day}:{occ.session.type}", occ.session.id, occ.session.type):
        if key in table:
            return D(table[key])
    return None


# --------------------------------------------------------------------------- seats, rooms, slots

def slot_opportunity(profile: BusinessProfile, ctx: DealContext, placement: Placement,
                     chosen_units: dict[str, list[str]]) -> tuple[Decimal, list[DisplacedItem]]:
    """O = sum over blocked regular sessions of P(book) x C(regular).

    'chance' mode follows the spec literally: a session counts only when no
    equivalent unit is free to absorb that customer. 'matrix' mode uses
    P(resell) for the marginal unit, which already reflects how full the session is.
    """
    total = ZERO
    lines: list[DisplacedItem] = []
    cal = ctx.calendar
    for part in placement.parts:
        res = profile.resource(part.resource_id)
        if res.kind != "unit" or res.regular is None:
            continue
        units = chosen_units.get(res.id) or []
        for occ in overlapping_occurrences(profile, res, placement.start, placement.end):
            contrib = _lookup(res.regular.contribution, occ)
            if contrib is None or contrib == 0:
                continue
            if res.regular.mode == "chance":
                p = _lookup(cal.chance, occ) if _lookup(cal.chance, occ) is not None else _lookup(res.regular.chance, occ)
                p = D(p) if p is not None else D(profile.policy.p_resell_default)
                taken = busy_units(cal, res, occ.start, occ.end) | set(units)
                free_other = len([u for u in res.unit_ids if u not in taken])
                displaced_units = max(0, len(units) - free_other)
                amount = D(displaced_units) * p * contrib
                note = "absorbed by a free equivalent unit" if displaced_units == 0 else ""
                lines.append(DisplacedItem(f"{res.label} {occ.session.id} {occ.start:%a %H:%M}", p, contrib * displaced_units, amount, note))
            else:
                forced = _lookup(res.regular.chance, occ)
                if _lookup(cal.chance, occ) is not None:
                    forced = _lookup(cal.chance, occ)
                if forced is not None:
                    p = D(forced)
                    note = "forecast"
                else:
                    util = utilisation(profile, cal, res, occ)
                    p = p_resell(profile, ctx.lead_time_hours(occ.start), util)
                    note = f"P(resell) matrix: lead {int(ctx.lead_time_hours(occ.start))}h, utilisation {int(util * 100)}%"
                amount = D(len(units)) * p * contrib
                lines.append(DisplacedItem(f"{len(units)} x {res.label} {occ.session.id} {occ.start:%a %H:%M}", p, contrib * len(units), amount, note))
            total += amount
    return total, lines


# --------------------------------------------------------------------------- people / machine hours

def hours_opportunity(profile: BusinessProfile, ctx: DealContext, resource: Resource, day_iso: str,
                      weekday: str, hours: Decimal) -> tuple[Decimal, list[DisplacedItem]]:
    """Hours displaced = how far booked + forecast + this job exceeds capacity.

    Each displaced hour costs the cheaper of losing the work (contribution per
    hour) or buying the capacity back (overtime / subcontract premium).
    """
    cap = D(resource.hours_capacity_per_day)
    booked = D(ctx.calendar.hours_booked.get(f"{resource.id}:{day_iso}", 0))
    forecast = D(resource.forecast_hours_per_day.get(weekday, resource.forecast_hours_per_day.get("default", 0)))
    displaced = max(ZERO, min(D(hours), booked + forecast + D(hours) - cap))
    per_hour = D(resource.contribution_per_hour)
    if resource.overtime_premium_per_hour is not None:
        per_hour = min(per_hour, D(resource.overtime_premium_per_hour))
    amount = displaced * per_hour
    note = f"capacity {cap}h, booked {booked}h, forecast {forecast}h"
    return amount, [DisplacedItem(f"{displaced}h of {resource.label}", ONE, per_hour, amount, note)]


# --------------------------------------------------------------------------- stock

def _poisson_tail(lam: float, k: int) -> float:
    """P(D >= k) for D ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def stock_economics(profile: BusinessProfile, ctx: DealContext, product: Product, qty: int,
                    unit_price: Optional[Decimal] = None) -> tuple[Decimal, Decimal, list[DisplacedItem]]:
    """Variable cost and opportunity cost of using `qty` units of a stocked product.

    Replenishable stock: cost = replacement cost; opportunity = chance each unit
    would otherwise have sold at full price before the restock x the margin lost.
    Perishable / not replenishable: the purchase cost is sunk, so cost = 0 and
    opportunity = what each unit would otherwise have fetched (full price if it
    would have sold before expiry, salvage value if not).
    """
    if qty <= 0:
        return ZERO, ZERO, []
    price = profile.business.tax.net(D(product.price))
    on_hand = ctx.calendar.product_on_hand.get(product.id, product.on_hand)
    if on_hand is None:
        return D(product.unit_cost) * qty, ZERO, []
    perishable = (not product.replenishable) or (
        product.expiry_days is not None and D(product.expiry_days) < D(product.days_to_restock)
    )
    horizon = D(product.expiry_days) if (perishable and product.expiry_days is not None) else D(product.days_to_restock)
    lam = float(D(product.daily_velocity) * horizon)
    lines: list[DisplacedItem] = []
    opp = ZERO
    for k in range(1, qty + 1):
        position = on_hand - k + 1  # the k-th unit taken off the top of the stock
        p_sold = prob(_poisson_tail(lam, position)) if position > 0 else ONE
        if perishable:
            opp += p_sold * price + (ONE - p_sold) * D(product.salvage_value)
        else:
            opp += p_sold * (price - D(product.unit_cost))
    variable = ZERO if perishable else D(product.unit_cost) * qty
    avg_p = prob(sum(prob(_poisson_tail(lam, on_hand - k + 1)) if on_hand - k + 1 > 0 else ONE for k in range(1, qty + 1)) / qty)
    note = (f"on hand {on_hand}, ~{lam:.1f} expected full-price sales in the next {horizon} days"
            + ("; cost already paid (perishable)" if perishable else "; replenishable"))
    lines.append(DisplacedItem(f"{qty} x {product.label}", avg_p, price, opp, note))
    return variable, opp, lines


# --------------------------------------------------------------------------- cash

def cash_opportunity(profile: BusinessProfile, ctx: DealContext, amount: Decimal, credit_days: int) -> tuple[Decimal, list[DisplacedItem]]:
    """Cost of waiting for money: amount x cost of capital x days/365, plus expected bad debt."""
    if credit_days <= 0 or amount <= 0:
        return ZERO, []
    coc = D(profile.economics.cost_of_capital_annual)
    if ctx.posture is not None and getattr(ctx.posture, "cost_of_capital", None) is not None:
        coc = D(ctx.posture.cost_of_capital)
    carry = D(amount) * coc * D(credit_days) / D(365)
    bad = D(amount) * D(profile.economics.bad_debt_rate)
    return carry + bad, [DisplacedItem(f"{credit_days}-day credit on {amount}", ONE, amount, carry + bad,
                                       f"cost of capital {coc}, bad-debt rate {profile.economics.bad_debt_rate}")]


# --------------------------------------------------------------------------- extensions

def extension_opportunity(profile: BusinessProfile, ctx: DealContext, placement: Placement,
                          chosen_units: dict[str, list[str]], extra_hours: Decimal) -> tuple[Decimal, list[DisplacedItem], bool]:
    """Extending a booking blocks the next session's units. Returns (O, lines, available)."""
    if extra_hours <= 0:
        return ZERO, [], True
    ext = Placement(parts=placement.parts, start=placement.end,
                    end=placement.end + timedelta(hours=float(extra_hours)), label="extension")
    # Units must be unsold in the extension window.
    for part in placement.parts:
        res = profile.resource(part.resource_id)
        taken = busy_units(ctx.calendar, res, ext.start, ext.end)
        if set(chosen_units.get(res.id, [])) & taken:
            return ZERO, [], False
    o, lines = slot_opportunity(profile, ctx, ext, chosen_units)
    return o, lines, True
