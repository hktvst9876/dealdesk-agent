"""Step 3: candidate offers, placement first.

Where could the group go (requested slot, nearest free slot, a quieter session,
a package)? Then the standard quote plus up to two alternatives aimed at the
blocker, cheapest kind first: no-cost fix -> perk -> fee -> price. Every
candidate is built from the catalogue; anything else cannot be expressed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from ..engine.calendar_math import occurrences, overlapping_occurrences, pick_units, session_type_at
from ..engine.types import CalendarView, Candidate, ConcessionLine, Line, Placement, PlacementPart, Terms
from ..money import D
from ..policy.rules import perk_eligible
from ..profile.schema import BusinessProfile, Perk

TYPE_ORDER = {"clarify": 0, "item": 1, "threshold_reward": 1, "extension": 1, "terms": 2, "fee_waiver": 3, "price_discount": 4}

TOPIC_FACTS = {
    "fit": ["layout"], "risk": ["wet_weather", "cancellation"], "process": ["card_on_file", "cancellation"],
    "weather": ["wet_weather"], "layout": ["layout"], "pool": ["pool"], "cancellation": ["cancellation", "card_on_file"],
    "payment": ["card_on_file"], "children": ["children"],
}


@dataclass
class Enquiry:
    """The conversation's enquiry fields, merged across turns."""
    date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    session_id: Optional[str] = None
    party_size: Optional[int] = None
    occasion: Optional[str] = None
    seating: list[dict] = field(default_factory=list)
    byo_bottles: int = 0
    offered_price: Optional[float] = None
    basket: list[dict] = field(default_factory=list)
    deadline: Optional[str] = None

    @classmethod
    def from_state(cls, d: dict) -> "Enquiry":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


def package_resources(profile: BusinessProfile) -> set[str]:
    return {p.resource_id for p in profile.perks if p.type == "package" and p.resource_id}


def bookable_resources(profile: BusinessProfile) -> list:
    return [r for r in profile.inventory if r.kind == "unit" and r.pricing != "none"]


def _hhmm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(min(h, 23), m if h < 24 else 59)


def resolve_time(profile: BusinessProfile, enq: Enquiry, resource) -> Optional[tuple[datetime, datetime]]:
    if not enq.date:
        return None
    day = date.fromisoformat(enq.date)
    occs = occurrences(profile, resource, day) if resource is not None else []
    if enq.session_id:
        for o in occs:
            if o.session.id == enq.session_id or o.session.type == enq.session_id:
                return o.start, o.end
    if enq.start_time:
        start = datetime.combine(day, _hhmm(enq.start_time))
        if enq.end_time:
            end = datetime.combine(day, _hhmm(enq.end_time))
            if end <= start:
                end += timedelta(days=1)
            return start, end
        for o in occs:
            if o.start <= start < o.end:
                return start, o.end
        return start, start + timedelta(hours=3)
    return None


def default_seating(profile: BusinessProfile, party: int) -> list[PlacementPart]:
    """Fewest units of the main seat types that fit the group (packages excluded)."""
    parts: list[PlacementPart] = []
    left = party
    for r in bookable_resources(profile):
        if r.id in package_resources(profile) or not r.guests_per_unit or left <= 0:
            continue
        need = min(r.units, math.ceil(left / r.guests_per_unit))
        parts.append(PlacementPart(r.id, need))
        left -= need * r.guests_per_unit
    return parts


def requested_placement(profile: BusinessProfile, enq: Enquiry) -> Optional[Placement]:
    if not bookable_resources(profile):
        hours_res = [r for r in profile.inventory if r.kind == "hours"]
        if hours_res and enq.date and enq.start_time and enq.basket:
            hrs = sum(D(profile.product(b["product_id"]).hours or 0) * int(b["qty"]) for b in enq.basket)
            start = datetime.combine(date.fromisoformat(enq.date), _hhmm(enq.start_time))
            per_day = max(D(1), D(hours_res[0].hours_capacity_per_day) / D(8))
            end = start + timedelta(hours=float(max(D(1), hrs / per_day)))
            parts = [PlacementPart(hours_res[0].id, hours=hrs)]
            return Placement(parts, start, end, placement_label(profile, parts, start, end))
        return None
    if enq.seating:
        parts = [PlacementPart(s["resource_id"], int(s["units"])) for s in enq.seating]
    elif enq.party_size:
        parts = default_seating(profile, enq.party_size)
    else:
        return None
    if not parts:
        return None
    window = resolve_time(profile, enq, profile.resource(parts[0].resource_id))
    if window is None:
        return None
    return Placement(parts, window[0], window[1], placement_label(profile, parts, window[0], window[1]))


def placement_label(profile: BusinessProfile, parts: list[PlacementPart], start: datetime, end: datetime) -> str:
    def one(p: PlacementPart) -> str:
        r = profile.resource(p.resource_id)
        if r.kind == "hours":
            return "your appointment"
        if r.units == 1:
            return f"the {r.label}"
        if p.units == 1:
            return f"a {r.label}"
        return f"{p.units} {r.label}s"
    what = " and ".join(one(p) for p in parts)
    return f"{what}, {start:%A} {start.day} {start:%b} from {start:%H:%M}"


def shifted(profile: BusinessProfile, pl: Placement, day_delta: int, occ_start: datetime, occ_end: datetime) -> Placement:
    return Placement([PlacementPart(p.resource_id, p.units, None, p.hours) for p in pl.parts], occ_start, occ_end,
                     placement_label(profile, pl.parts, occ_start, occ_end))


def placement_free(profile: BusinessProfile, cal: CalendarView, pl: Placement) -> bool:
    for p in pl.parts:
        res = profile.resource(p.resource_id)
        if res.kind != "unit":
            continue
        units, _ = pick_units(cal, res, pl.start, pl.end, p.units, p.unit_ids)
        if units is None:
            return False
    return True


def nearby_placements(profile: BusinessProfile, cal: CalendarView, pl: Placement, now: datetime, limit: int = 2,
                      same_day_first: bool = True) -> list[Placement]:
    """Free placements near the requested one: other sessions that day, then the same session on nearby days."""
    res = profile.resource(pl.parts[0].resource_id)
    found: list[Placement] = []
    want_type = session_type_at(profile, res, pl.start)
    order = [0, 1, -1, 2, -2, 3, -3, 7] if same_day_first else [1, -1, 2, -2, 7]
    for delta in order:
        day = (pl.start + timedelta(days=delta)).date()
        occs = occurrences(profile, res, day)
        occs.sort(key=lambda o: (o.session.type != want_type, abs((o.start - pl.start).total_seconds())))
        for o in occs:
            if o.start <= now + timedelta(hours=1):
                continue
            if delta == 0 and o.start == pl.start:
                continue
            cand = shifted(profile, pl, delta, o.start, o.end)
            if placement_free(profile, cal, cand) and all(cand.key() != f.key() for f in found):
                found.append(cand)
            if len(found) >= limit:
                return found
    return found


# --------------------------------------------------------------------------- concessions

def concession_qty(perk: Perk, party: int, requested_qty: Optional[int] = None) -> Optional[int]:
    if requested_qty:
        return min(requested_qty, perk.max_per_booking)
    if perk.cost_per == "guest":
        if party > perk.max_per_booking:
            return None  # cannot cover everyone: a perk for half the table is not a gesture
        return max(1, party)
    return 1


@dataclass
class Plan:
    candidates: list[Candidate]
    requested: list[str]
    declined_requests: list[str]  # explicit asks that could not even be expressed (not in catalogue / ineligible)
    placement: Optional[Placement]
    placement_unavailable: str = ""
    alternatives_for_unavailable: list[Placement] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)


def build_candidates(profile: BusinessProfile, enq: Enquiry, *, blocker: str, requested: list[tuple[str, int]],
                     asks_about: list[str], granted: list[tuple[str, int]], declined: list[str],
                     cal: CalendarView, now: datetime, high_demand_hint: bool = False) -> Plan:
    party = int(enq.party_size or 0)
    if not party and enq.basket:  # services: one guest per service booked
        party = sum(int(b["qty"]) for b in enq.basket if profile.product(b["product_id"]).resource_id)
        enq.party_size = party or None
    pl = requested_placement(profile, enq)
    pricing = "minimum_spend"
    basket: list[Line] = [Line(b["product_id"], int(b["qty"])) for b in enq.basket]
    if pl is None and basket:
        pricing = "basket"
    elif pl is not None and any(profile.resource(p.resource_id).kind == "hours" for p in pl.parts):
        pricing = "basket"
    elif pl is not None:
        res = profile.resource(pl.parts[0].resource_id)
        pricing = "fixed" if res.pricing == "fixed" else "minimum_spend"
    price = D(enq.offered_price) if (enq.offered_price and pricing == "fixed") else None

    plan = Plan([], [r for r, _ in requested], [], pl)
    if pl is not None and not placement_free(profile, cal, pl):
        plan.placement_unavailable = "requested time is already booked"
        plan.alternatives_for_unavailable = nearby_placements(profile, cal, pl, now)
        if not plan.alternatives_for_unavailable:
            return plan
        pl = plan.alternatives_for_unavailable[0]
        plan.placement = pl

    granted_lines = [ConcessionLine(p, q) for p, q in granted]

    def mk(oid: str, label: str, kind: str, extra: list[ConcessionLine], placement: Optional[Placement] = pl,
           floor: Optional[Decimal] = None, p_class: Optional[str] = None, package: Optional[str] = None,
           facts: Optional[list[str]] = None, conditions: Optional[list[str]] = None) -> Candidate:
        lines = list(granted_lines)
        for e in extra:
            existing = next((l for l in lines if l.perk_id == e.perk_id), None)
            if existing:
                existing.qty = max(existing.qty, e.qty)
            else:
                lines.append(e)
        c = Candidate(oid, label, kind, placement, pricing=pricing, floor=floor, price=price, basket=list(basket),
                      concessions=lines, new_concessions=[e.perk_id for e in extra], p_class=p_class, package_id=package,
                      facts=facts or [], conditions=conditions or [])
        if kind in ("concession", "clarify") and not lines and not facts:
            c.kind = "standard"
        return c

    # The standard quote (no concessions) and, if something was granted already, the current offer.
    plan.candidates.append(Candidate("STD", "Standard quote", "standard", pl, pricing=pricing, price=price, basket=list(basket)))
    if granted_lines:
        plan.candidates.append(mk("CUR", "Current offer", "concession", []))

    def eligible(perk: Perk, placement: Optional[Placement] = pl) -> tuple[bool, str]:
        total = sum((D(profile.product(l.product_id).price) * l.qty for l in basket), D(0))
        return perk_eligible(profile, perk, party_size=party, occasion=enq.occasion, placement=placement,
                             calendar=cal, now=now, basket_total=total)

    # No-cost clarification first (layout, wet-weather terms, how confirmation works).
    topics = list(dict.fromkeys(([blocker] if blocker in TOPIC_FACTS else []) + [a for a in asks_about if a in TOPIC_FACTS]))
    fact_keys = [k for t in topics for k in TOPIC_FACTS[t] if k in profile.business.house_rules]
    plan.facts = [profile.business.house_rules[k] for k in dict.fromkeys(fact_keys)]
    if plan.facts:
        plan.candidates.append(mk("INFO", "Clarify terms (no cost)", "clarify", [], facts=plan.facts))

    taken = {p for p, _ in granted}
    chosen: list[tuple[Perk, int]] = []
    # Explicit asks are always evaluated (the engine decides), even if not aimed at the blocker.
    for perk_id, qty in requested:
        if not profile.has_perk(perk_id):
            plan.declined_requests.append(perk_id)
            continue
        perk = profile.perk(perk_id)
        if perk.type == "package":
            continue
        ok, why = eligible(perk)
        q = concession_qty(perk, party, qty)
        if ok and q:
            chosen.append((perk, q))
        else:
            plan.declined_requests.append(perk_id)
    # Up to N alternatives aimed at the blocker, cheapest kind first. A customer who
    # asked for something specific also gets a no-cost or cheaper counter.
    aimed = [p for p in profile.perks
             if p.type != "package" and blocker in p.blockers and p.id not in taken and p.id not in declined
             and p.id not in {c.id for c, _ in chosen}]
    if requested and blocker == "price":
        aimed += [p for p in profile.perks if p.type in ("item", "threshold_reward") and "occasion" in p.blockers
                  and p.id not in taken and p.id not in declined and p not in aimed and p.id not in {c.id for c, _ in chosen}]
    aimed.sort(key=lambda p: (TYPE_ORDER.get(p.type, 9), p.unit_true_cost()))
    extra = 0
    for perk in aimed:
        if extra >= profile.policy.max_alternatives:
            break
        ok, _ = eligible(perk)
        q = concession_qty(perk, party)
        if ok and q:
            chosen.append((perk, q))
            extra += 1
    for perk, q in chosen:
        oid = perk.id.upper()
        cond = [perk.condition] if perk.condition else []
        plan.candidates.append(mk(oid, perk.label, "concession", [ConcessionLine(perk.id, q)], conditions=cond))
        if perk.type == "fee_waiver" and q > 1:  # counter with the cheapest version too
            plan.candidates.append(mk(f"{oid}_1", f"{perk.label} (1)", "concession", [ConcessionLine(perk.id, 1)]))

    # Alternative placements: packages the customer asked about (or on busy nights), and a quieter session.
    if pl is not None:
        hd = high_demand_hint
        packages = [p for p in profile.perks if p.type == "package"]
        for perk in packages:
            asked = perk.id in plan.requested or any(t in k for t in asks_about for k in perk.mention_keywords())
            if perk.floor_basis == "requested":
                # An upgrade at the customer's current floor is a give-away: only after they
                # have turned down the package price, and it needs whoever the catalogue says.
                siblings = [p.id for p in packages if p.resource_id == perk.resource_id and p.id != perk.id]
                include = perk.id in plan.requested or (asked and any(s in declined for s in siblings))
            else:
                include = asked or (hd and blocker == "price")
            if not include or perk.id in declined:
                continue
            alt = Placement([PlacementPart(perk.resource_id, 1)], pl.start, pl.end,
                            placement_label(profile, [PlacementPart(perk.resource_id, 1)], pl.start, pl.end))
            ok, _ = eligible(perk, alt)
            if not ok:
                continue
            if perk.floor_basis == "requested":
                std_floor = sum(D(profile.resource(p.resource_id).floors.get(
                    session_type_at(profile, profile.resource(p.resource_id), pl.start) or "", 0)) * p.units for p in pl.parts)
                plan.candidates.append(mk(perk.id.upper(), perk.label, "alternative", [ConcessionLine(perk.id, 1)],
                                          placement=alt, floor=std_floor, package=perk.id))
            else:
                plan.candidates.append(mk(perk.id.upper(), perk.label, "alternative", [], placement=alt,
                                          floor=perk.floor, p_class=perk.p_class, package=perk.id))
        if blocker in ("timing", "price") and (hd or blocker == "timing"):
            for i, q in enumerate(nearby_placements(profile, cal, pl, now, limit=2, same_day_first=True)):
                plan.candidates.append(mk(f"ALT{i+1}", f"Different time: {q.label}", "alternative", [], placement=q,
                                          p_class="alternative"))
    return plan


def repriced_standard(cand: Candidate, min_price: Decimal) -> Candidate:
    """High-demand slot: the quote rises to the minimum price for that slot (rounded up to 10)."""
    step = D(10)
    target = (D(min_price) / step).to_integral_value(rounding="ROUND_CEILING") * step
    c = Candidate(f"{cand.offer_id}_MIN", f"{cand.label} at minimum price", "standard", cand.placement, pricing=cand.pricing,
                  basket=list(cand.basket))
    if cand.pricing == "fixed":
        c.price = target
    else:
        c.floor = target
    return c
