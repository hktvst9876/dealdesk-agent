"""The financial decision engine.

Deterministic code. It takes the business profile, the facts of this turn and
candidate offers, and returns the figures plus a sendable / not-sendable verdict
with the rule that produced it. The language model never computes a number.

    R    = max(F(1-d), n*s) + fees + extras            (minimum-spend venues)
    C    = R - COGS - service - BYO displaced - perk cost - perk displaced margin
    O    = sum_i P(book_i) x C(regular_i)              (+ stock, hours, cash where used)
    Pmin = V + max(O, M)                               (solved for the floor where costs scale)
    E    = p x C + (1 - p) x O
    p*   = (E_standard - O) / (C - O)
"""
from __future__ import annotations

import copy
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from ..money import ONE, ZERO, D, one_dp, prob
from ..profile.schema import TIER_ORDER, BusinessProfile, Perk
from .calendar_math import is_high_demand, overlapping_occurrences, pick_units, session_type_at
from .opportunity import cash_opportunity, extension_opportunity, hours_opportunity, slot_opportunity, stock_economics
from .types import Candidate, DealContext, DisplacedItem, Figures, TurnDecision

TIER_LABEL = {"front_line": "front-line", "manager": "manager", "owner": "owner"}


def _max_tier(*tiers: Optional[str]) -> str:
    tiers = [t for t in tiers if t]
    return max(tiers, key=lambda t: TIER_ORDER[t]) if tiers else "front_line"


def _fmt(x: Decimal) -> str:
    v = one_dp(x)
    return f"{v:.0f}" if v == v.to_integral_value() else f"{v:.1f}"


# --------------------------------------------------------------------------- one candidate

def compute(profile: BusinessProfile, ctx: DealContext, cand: Candidate) -> Figures:
    """Revenue, costs, contribution, opportunity cost and minimum price for one candidate."""
    eco = profile.economics
    fig = Figures(offer_id=cand.offer_id, label=cand.label, kind=cand.kind, candidate=cand)
    fig.conditions = list(cand.conditions)
    n = int(ctx.party_size or 0)
    posture = ctx.posture

    # ---------------------------------------------------------------- placement
    chosen: dict[str, list[str]] = {}
    session_type = None
    high_demand = False
    if cand.placement is not None:
        pl = cand.placement
        for part in pl.parts:
            res = profile.resource(part.resource_id)
            if res.kind != "unit":
                continue
            units, why = pick_units(ctx.calendar, res, pl.start, pl.end, part.units, part.unit_ids)
            if units is None:
                fig.available = False
                fig.unavailable_reason = why
                fig.decision = "not_offerable"
                fig.rule = f"not offerable at any price: {why}"
                return fig
            chosen[res.id] = units
            for occ in overlapping_occurrences(profile, res, pl.start, pl.end):
                high_demand = high_demand or is_high_demand(profile, ctx.calendar, res, occ)
        first = profile.resource(pl.parts[0].resource_id)
        session_type = pl.session_type or session_type_at(profile, first, pl.start)
    fig.session_type = session_type
    fig.high_demand = high_demand

    # ---------------------------------------------------------------- concessions
    perks: list[tuple[Perk, int]] = [(profile.perk(c.perk_id), int(c.qty)) for c in cand.concessions]
    discount = ZERO
    discount_on = "floor"
    fee_waived: dict[str, int] = {}
    extra_hours = ZERO
    threshold = None
    for perk, qty in perks:
        if perk.type == "price_discount":
            discount = max(discount, D(perk.discount_pct))
            discount_on = perk.scope or "floor"
        elif perk.type == "fee_waiver":
            fee_waived[perk.fee_id] = fee_waived.get(perk.fee_id, 0) + qty
        elif perk.type == "extension":
            extra_hours += D(perk.extra_hours) * qty
        elif perk.type == "threshold_reward":
            threshold = max(D(perk.threshold), threshold or ZERO)
    credit_days = max([cand.terms.credit_days] + [p.credit_days for p, _ in perks])
    deposit = cand.terms.deposit or any(p.deposit for p, _ in perks)
    fig.has_price_discount = discount > 0

    cogs_pct = D(eco.cogs_pct)
    if posture is not None and getattr(posture, "cogs_pct", None) is not None:
        cogs_pct = D(posture.cogs_pct)

    # ---------------------------------------------------------------- revenue
    fees = ZERO
    for fee_id, fee in profile.fees.items():
        if fee.charged != "always":
            continue
        if fee.per == "bottle":
            charged = max(0, ctx.byo_bottles - fee_waived.get(fee_id, 0))
            fees += D(fee.amount) * charged
        elif fee.per == "booking" and (cand.placement is not None or cand.basket):
            fees += D(fee.amount) * (0 if fee_waived.get(fee_id) else 1)
    breakdown: list[tuple[str, Decimal]] = []
    variable = ZERO
    revenue_core = ZERO
    spend_case = "base"
    price_quoted: Optional[Decimal] = None

    if cand.pricing == "minimum_spend":
        if cand.floor is not None:
            floor = D(cand.floor)
        else:
            floor = ZERO
            if cand.placement is not None:
                for part in cand.placement.parts:
                    res = profile.resource(part.resource_id)
                    floor += D(res.floors.get(session_type or "default", res.floors.get("default", 0))) * part.units
        spend = profile.spend_case(session_type)
        s = ZERO
        if spend is not None:
            # A lowered floor removes the protection the base case relied on.
            spend_case = "conservative" if discount > 0 else "base"
            s = D(spend.conservative if discount > 0 else spend.base)
        organic = D(n) * s
        if discount_on == "bill":
            revenue_core = max(floor, organic) * (ONE - discount)
        else:
            revenue_core = max(floor * (ONE - discount), organic)
        if threshold is not None:
            revenue_core = max(revenue_core, threshold)
        price_quoted = floor * (ONE - discount) if discount_on != "bill" else floor
        cogs = cogs_pct * revenue_core
        service = D(eco.service_cost_per_guest) * n
        byo = D(eco.byo_displaced_margin_per_bottle) * ctx.byo_bottles
        variable += cogs + service + byo
        breakdown += [("F&B revenue", revenue_core), ("fees", fees), ("COGS", -cogs), ("service", -service), ("BYO displaced margin", -byo)]
        fig.revenue_fnb = revenue_core
    elif cand.pricing == "fixed":
        if cand.price is not None:
            price = D(cand.price)
        else:
            price = ZERO
            for part in cand.placement.parts if cand.placement else []:
                res = profile.resource(part.resource_id)
                unit_price = D(res.prices.get(session_type or "default", res.prices.get("default", 0)))
                hrs = cand.placement.hours if res.price_per == "hour" else ONE
                price += unit_price * part.units * hrs
        price_quoted = price * (ONE - discount)
        revenue_core = price_quoted
        breakdown.append(("price", revenue_core))
    else:  # basket
        for line in cand.basket:
            prod = profile.product(line.product_id)
            unit_price = profile.business.tax.net(D(line.unit_price if line.unit_price is not None else prod.price))
            scope_hit = discount > 0 and (discount_on in ("floor", "bill", "all") or discount_on == prod.category)
            line_rev = unit_price * line.qty * (ONE - discount if scope_hit else ONE)
            revenue_core += line_rev
            v, o, lines = stock_economics(profile, ctx, prod, line.qty, unit_price)
            variable += v
            fig.opportunity_cost += o
            fig.displaced += lines
        if threshold is not None:
            revenue_core = max(revenue_core, threshold)
        price_quoted = revenue_core
        breakdown.append(("basket revenue", revenue_core))

    # Resource cost to serve (staffing, cleaning, energy) and people/machine hours used.
    service_hours: dict[str, Decimal] = {}
    if cand.placement is not None:
        hours = cand.placement.hours + extra_hours
        for part in cand.placement.parts:
            res = profile.resource(part.resource_id)
            cts = res.cost_to_serve
            if res.kind == "hours":
                hrs = D(part.hours) if part.hours is not None else cand.placement.hours
                service_hours[res.id] = service_hours.get(res.id, ZERO) + hrs
                cost = D(cts.per_booking) + D(cts.per_guest) * n + D(cts.per_hour) * hrs
            else:
                cost = D(cts.per_booking) + D(cts.per_guest) * n + D(cts.per_hour) * hours * part.units
            if cost:
                variable += cost
                breakdown.append((f"cost to serve {res.label}", -cost))
    for line in cand.basket:
        prod = profile.product(line.product_id)
        if prod.resource_id and prod.hours and not (cand.placement and any(p.resource_id == prod.resource_id for p in cand.placement.parts)):
            service_hours[prod.resource_id] = service_hours.get(prod.resource_id, ZERO) + D(prod.hours) * line.qty
    for perk, qty in perks:
        if perk.type == "item" and perk.product_id:
            prod = profile.product(perk.product_id)
            if prod.resource_id and prod.hours:
                service_hours[prod.resource_id] = service_hours.get(prod.resource_id, ZERO) + D(prod.hours) * qty
    when = cand.placement.start if cand.placement is not None else ctx.now
    weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][when.weekday()]
    for rid, hrs in service_hours.items():
        res = profile.resource(rid)
        o, lines = hours_opportunity(profile, ctx, res, when.date().isoformat(), weekday, hrs)
        fig.opportunity_cost += o
        fig.displaced += lines
        cap = D(res.hours_capacity_per_day)
        if cap > 0:
            booked = D(ctx.calendar.hours_booked.get(f"{rid}:{when.date().isoformat()}", 0))
            forecast = D(res.forecast_hours_per_day.get(weekday, res.forecast_hours_per_day.get("default", 0)))
            if (booked + forecast) / cap > D(profile.policy.high_demand.utilisation_above) or \
                    when.date().isoformat() in ctx.calendar.high_demand:
                high_demand = True
    fig.high_demand = high_demand

    revenue = revenue_core + fees
    fig.fees = fees

    # ---------------------------------------------------------------- perk costs (each impact counts once)
    perk_cost = ZERO
    for perk, qty in perks:
        units = qty
        if perk.type in ("item", "threshold_reward"):
            if perk.product_id:
                prod = profile.product(perk.product_id)
                v, o, lines = stock_economics(profile, ctx, prod, units)
                perk_cost += v + D(perk.displaced_margin) * units
                fig.opportunity_cost += o
                fig.displaced += lines
            else:
                perk_cost += perk.unit_true_cost() * units
        elif perk.type in ("extension", "terms"):
            perk_cost += perk.unit_true_cost() * units
    if perk_cost:
        breakdown.append(("perks given", -perk_cost))
    leakage = ZERO
    if discount > 0 and eco.price_leakage_factor:
        leakage = D(eco.price_leakage_factor) * (revenue_core / (ONE - discount) - revenue_core if discount < 1 else revenue_core)
        breakdown.append(("price-integrity leakage", -leakage))
    fig.concession_true_cost = perk_cost

    contribution = revenue - variable - perk_cost - leakage

    # ---------------------------------------------------------------- opportunity cost
    if cand.placement is not None:
        o, lines = slot_opportunity(profile, ctx, cand.placement, chosen)
        fig.opportunity_cost += o
        fig.displaced += lines
        if extra_hours > 0:
            o, lines, ok = extension_opportunity(profile, ctx, cand.placement, chosen, extra_hours)
            if not ok:
                fig.available = False
                fig.unavailable_reason = "the next session's units are already sold"
                fig.decision = "not_offerable"
                fig.rule = "extension not available: next session already sold"
                return fig
            fig.opportunity_cost += o
            fig.displaced += lines
    if credit_days:
        o, lines = cash_opportunity(profile, ctx, revenue, credit_days)
        fig.opportunity_cost += o
        fig.displaced += lines

    # ---------------------------------------------------------------- minimum contribution and price
    m = D(profile.policy.min_contribution)
    if cand.placement is not None:
        overrides = [D(profile.resource(p.resource_id).min_contribution) for p in cand.placement.parts
                     if profile.resource(p.resource_id).min_contribution is not None]
        if overrides:
            m = max(overrides)
    if posture is not None and not high_demand:
        m = m * D(getattr(posture, "min_contribution_multiplier", ONE))
    fig.min_contribution = m
    target = max(fig.opportunity_cost, m)
    if cand.pricing == "minimum_spend":
        other_costs = variable - cogs_pct * revenue_core  # service + BYO + cost to serve
        denom = (ONE - cogs_pct)
        fig.min_price = max(ZERO, (target + other_costs + perk_cost + leakage - fees) / denom)
    else:
        fig.min_price = max(ZERO, variable + perk_cost + leakage + target - fees)

    # Future value (off by default) and no-show risk adjust the contribution used for E.
    if eco.future_value.enabled:
        contribution += D(eco.future_value.per_booking)
        breakdown.append(("future value", D(eco.future_value.per_booking)))
    if eco.no_show.enabled and cand.placement is not None:
        r = D(eco.no_show.rate_with_deposit if deposit else eco.no_show.rate)
        contribution = (ONE - r) * contribution + r * D(eco.no_show.fee_recovered)

    fig.revenue = revenue
    fig.variable_cost = variable
    fig.contribution = contribution
    fig.incremental_value = contribution - fig.opportunity_cost
    fig.price_quoted = price_quoted
    fig.spend_case = spend_case
    fig.breakdown = breakdown
    return fig


def _basis(cand: Candidate) -> Candidate:
    """The same placement and package on standard terms: no concessions, no special terms."""
    b = copy.copy(cand)
    b.concessions = []
    b.new_concessions = []
    b.offer_id = f"{cand.offer_id}:basis"
    return b


# --------------------------------------------------------------------------- close rate

def p_book_for(profile: BusinessProfile, ctx: DealContext, cand: Candidate) -> tuple[Decimal, str]:
    defaults = dict(profile.policy.p_book_defaults)
    by_blocker = profile.policy.p_book_by_blocker.get(ctx.blocker, {})
    def p_of(cls: str) -> Decimal:
        if cls in ctx.p_overrides:
            return D(ctx.p_overrides[cls])
        if cls in by_blocker:
            return D(by_blocker[cls])
        return D(defaults.get(cls, defaults.get("standard", Decimal("0.4"))))

    if cand.p_class:
        return p_of(cand.p_class), cand.p_class
    if cand.kind == "standard":
        return p_of("standard"), "standard"
    if cand.kind == "clarify":
        return p_of("clarify"), "clarify"
    classes = [profile.perk(c.perk_id).p_class for c in cand.concessions] or ["alternative" if cand.kind == "alternative" else "standard"]
    best = max(classes, key=p_of)
    return p_of(best), best


# --------------------------------------------------------------------------- all candidates

def evaluate_offers(profile: BusinessProfile, ctx: DealContext, candidates: list[Candidate]) -> TurnDecision:
    """Score every candidate and pick the turn's decision.

    Policy order: covers opportunity cost -> minimum contribution -> session rule
    -> beats the standard quote -> cumulative concession cap -> authority tier.
    """
    pol = profile.policy
    figs: list[Figures] = []
    for cand in candidates:
        fig = compute(profile, ctx, cand)
        if fig.available:
            if cand.concessions:
                # Value given up vs the same placement on standard terms. Using incremental
                # value (C - O) also catches concessions whose cost sits in O: an extension
                # into a sellable session, scarce or short-dated stock given away.
                basis = compute(profile, ctx, _basis(cand))
                fig.concession_cost = max(ZERO, basis.incremental_value - fig.incremental_value)
            fig.cumulative_concession = fig.concession_cost
            fig.p_book, cls = p_book_for(profile, ctx, cand)
            fig.expected_contribution = fig.p_book * fig.contribution + (ONE - fig.p_book) * fig.opportunity_cost
            fig.assumptions = {
                "spend_case": fig.spend_case, "p_book": str(fig.p_book), "p_class": cls,
                "opportunity_cost": str(one_dp(fig.opportunity_cost)), "high_demand": fig.high_demand,
            }
        figs.append(fig)

    standard = next((f for f in figs if f.kind == "standard" and f.available), None)
    e_std = standard.expected_contribution if standard else None

    budget_spent = posture_budget_exhausted(ctx)
    sender_rank = TIER_ORDER.get(ctx.sender_tier, 0)

    for fig in figs:
        if not fig.available:
            continue
        cand = fig.candidate
        o, c, m = fig.opportunity_cost, fig.contribution, fig.min_contribution
        if e_std is not None and c > o:
            fig.breakeven_p_book = prob((e_std - o) / (c - o)) if e_std > o else ZERO
        checks = {
            "covers_opportunity": c >= o,
            "min_contribution": c >= m,
            "session_rule": not (fig.high_demand and fig.has_price_discount and pol.high_demand.block_price_discounts),
            "beats_standard": True if (fig.kind == "standard" or e_std is None) else fig.expected_contribution > e_std,
            "within_cap": fig.cumulative_concession <= D(pol.concession_cap),
        }
        # Authority: the cumulative figure, each perk's own approval tier, and the cap.
        tier = pol.tier_for_amount(fig.cumulative_concession)
        if budget_spent and fig.cumulative_concession > 0:
            tier = _max_tier(tier, "manager")
        if not checks["within_cap"]:
            tier = _max_tier(tier, "manager")
        for cl in cand.concessions:
            perk = profile.perk(cl.perk_id)
            t = perk.tier_for_qty(cl.qty)
            tier = _max_tier(tier, t if t else "owner")
        fig.authority_required = tier
        checks["within_authority"] = TIER_ORDER[tier] <= sender_rank and checks["within_cap"]
        fig.checks = checks

        e = _fmt(fig.expected_contribution)
        if fig.kind == "standard":
            if checks["covers_opportunity"] and checks["min_contribution"] and checks["session_rule"]:
                fig.decision, fig.sendable = "keep_quote", True
                fig.rule = f"standard terms clear policy: C({_fmt(c)}) >= max(O {_fmt(o)}, M {_fmt(m)})"
            else:
                fig.decision = "not_offerable"
                fig.rule = f"below minimum price {_fmt(fig.min_price)}: C({_fmt(c)}) < max(O {_fmt(o)}, M {_fmt(m)})"
            continue
        failed = [k for k in ("covers_opportunity", "min_contribution", "session_rule") if not checks[k]]
        if failed:
            fig.decision = "rejected"
            reason = {
                "covers_opportunity": f"C({_fmt(c)}) < O({_fmt(o)})",
                "min_contribution": f"C({_fmt(c)}) < minimum contribution {_fmt(m)}",
                "session_rule": "high-demand session blocks price discounts",
            }[failed[0]]
            fig.rule = reason
            continue
        if not checks["beats_standard"]:
            fig.decision = "rejected"
            fig.rule = f"E({e}) <= E_standard({_fmt(e_std)}): standard is worth more"
            continue
        if fig.kind == "clarify":
            fig.decision, fig.sendable = "keep_quote", True
            fig.rule = "no-cost clarification; standard terms unchanged"
            continue
        limit = pol.authority_limit(tier)
        lim_txt = "unlimited" if limit == Decimal("Infinity") else _fmt(limit)
        if not checks["within_authority"]:
            fig.decision, fig.escalatable = "escalate", True
            why = (f"cumulative {_fmt(fig.cumulative_concession)} > cap {_fmt(pol.concession_cap)}" if not checks["within_cap"]
                   else f"needs {TIER_LABEL[tier]} approval")
            fig.rule = f"E({e}) > E_standard({_fmt(e_std)}) but {why}"
            continue
        fig.decision = "suggest_alternative" if fig.kind == "alternative" else "offer_approved_concession"
        fig.sendable = True
        fig.rule = (f"E({e}) > E_standard({_fmt(e_std)}); cumulative {_fmt(fig.cumulative_concession)} "
                    f"<= {TIER_LABEL[tier]} {lim_txt}")

    ranked = sorted([f for f in figs if f.available], key=lambda f: f.expected_contribution, reverse=True)
    sendables = [f for f in ranked if f.sendable]
    escalatables = [f for f in ranked if f.escalatable]
    best_send = _best(sendables)
    best_esc = escalatables[0] if escalatables else None
    if best_esc and (best_send is None or best_esc.expected_contribution > best_send.expected_contribution):
        return TurnDecision("escalate", best_esc, standard, best_send, ranked,
                            f"{best_esc.label}: {best_esc.rule}; fallback {best_send.label if best_send else 'none'}")
    if best_send:
        return TurnDecision(best_send.decision, best_send, standard, best_send, ranked, f"{best_send.label}: {best_send.rule}")
    return TurnDecision("not_offerable", None, standard, None, ranked or figs,
                        "no candidate clears policy; offer the nearest placement at its minimum price")


def _best(sendables: list[Figures]) -> Optional[Figures]:
    """Highest E; ties go to the cheaper concession, then to the standard quote."""
    if not sendables:
        return None
    top = max(f.expected_contribution for f in sendables)
    tied = [f for f in sendables if f.expected_contribution == top]
    return sorted(tied, key=lambda f: (f.concession_cost, 0 if f.kind in ("standard", "clarify") else 1))[0]


def posture_budget_exhausted(ctx: DealContext) -> bool:
    p = ctx.posture
    return bool(p is not None and getattr(p, "concession_budget_remaining", None) is not None
                and p.concession_budget_remaining <= 0)
