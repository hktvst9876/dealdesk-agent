"""Fixtures from Build Spec v2: the capacity-allocation test and the Salt worked numbers.

Used by the CLI (`dealdesk demo capacity`, `dealdesk demo salt`) and by the tests,
so what you see on screen is exactly what the test suite checks.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from .engine.evaluate import evaluate_offers
from .engine.types import (Booking, CalendarView, Candidate, ConcessionLine, DealContext, Placement,
                           PlacementPart, TurnDecision)
from .money import D, fmt_money, pct, whole
from .profile.loader import load_profile
from .profile.schema import BusinessProfile

# ------------------------------------------------------------------ Salt worked numbers

SALT_NOW = datetime(2026, 11, 14, 15, 0)         # Saturday afternoon, when the enquiry arrives
SALT_START = datetime(2026, 11, 22, 17, 0)       # Sunday 17:00 - late
SALT_END = datetime(2026, 11, 22, 22, 0)


def salt_placement() -> Placement:
    return Placement(parts=[PlacementPart("beachfront_bed", 3), PlacementPart("beanbag_section", 1)],
                     start=SALT_START, end=SALT_END, label="3 beachfront beds + bean bag section, Sunday 17:00-late")


def salt_vip_placement() -> Placement:
    return Placement(parts=[PlacementPart("vip_area", 1)], start=SALT_START, end=SALT_END,
                     label="VIP area, Sunday 17:00-late")


def salt_context(profile: BusinessProfile, blocker: str = "none", **overrides) -> DealContext:
    ctx = DealContext(now=SALT_NOW, party_size=17, occasion="birthday", byo_bottles=4, blocker=blocker,
                      calendar=CalendarView(stock={"cakes": 3}))
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def salt_candidates(profile: BusinessProfile) -> list[Candidate]:
    pl = salt_placement()
    return [
        Candidate("A", "Standard quote", "standard", pl),
        Candidate("B", "Waive corkage on 1 bottle", "concession", pl, concessions=[ConcessionLine("corkage_waiver", 1)],
                  new_concessions=["corkage_waiver"], conditions=["confirm at the quoted S$600 minimum today"]),
        Candidate("C", "Birthday cake (hospitality)", "concession", pl, concessions=[ConcessionLine("cake", 1)],
                  new_concessions=["cake"], conditions=["confirm at quoted S$600 minimum"]),
        Candidate("D", "15% off the floor", "concession", pl, concessions=[ConcessionLine("floor_discount", 1)],
                  new_concessions=["floor_discount"]),
        Candidate("E", "VIP upgrade at S$1,200", "alternative", salt_vip_placement(), floor=Decimal("1200"),
                  package_id="vip_group_rate", p_class="alternative"),
    ]


def salt_worked_numbers(profile: Optional[BusinessProfile] = None) -> TurnDecision:
    profile = profile or load_profile("salt_beach_club")
    return evaluate_offers(profile, salt_context(profile), salt_candidates(profile))


# ------------------------------------------------------------------ capacity test (karaoke)

KARAOKE_NOW = datetime(2026, 11, 16, 12, 0)
SAT = datetime(2026, 11, 21)
SUN = datetime(2026, 11, 22)


def _at(day: datetime, hh: int, mm: int = 0, plus_days: int = 0) -> datetime:
    return day.replace(hour=hh, minute=mm) + __import__("datetime").timedelta(days=plus_days)


def karaoke_calendar(case: int) -> CalendarView:
    """Baseline: Room B is confirmed on Saturday evening and Sunday afternoon."""
    bookings = [
        Booking("karaoke_room", "Room B", _at(SAT, 19), _at(SAT, 23), "confirmed", "other-party-sat"),
        Booking("karaoke_room", "Room B", _at(SUN, 14), _at(SUN, 18), "confirmed", "other-party-sun"),
    ]
    cal = CalendarView(bookings=bookings)
    if case == 2:
        cal.chance = {"2026-11-21:sat_eve_1": Decimal("0.4"), "2026-11-21:sat_eve_2": Decimal("0.3")}
    if case == 3:
        cal.bookings = [b for b in bookings if b.ref != "other-party-sat"]
    if case == 5:
        cal.bookings.append(Booking("karaoke_room", "Room A", _at(SAT, 20), _at(SAT, 22), "confirmed", "clash"))
    return cal


def karaoke_options(case: int) -> list[Candidate]:
    sat_start, sat_end = (_at(SAT, 21), _at(SAT, 1, plus_days=1)) if case == 4 else (_at(SAT, 19), _at(SAT, 23))
    perks = [ConcessionLine("cake", 1)] if case == 6 else []
    return [
        Candidate("SAT", "Saturday peak, four hours", "standard",
                  Placement([PlacementPart("karaoke_room", 1)], sat_start, sat_end, "Saturday peak"),
                  pricing="fixed", price=Decimal("590"), concessions=perks),
        Candidate("SUN", "Sunday afternoon, four hours", "alternative",
                  Placement([PlacementPart("karaoke_room", 1)], _at(SUN, 14), _at(SUN, 18), "Sunday afternoon"),
                  pricing="fixed", price=Decimal("590"), concessions=perks),
    ]


def karaoke_case(case: int, profile: Optional[BusinessProfile] = None) -> TurnDecision:
    profile = profile or load_profile("karaoke_demo")
    ctx = DealContext(now=KARAOKE_NOW, party_size=12, occasion="birthday", calendar=karaoke_calendar(case))
    return evaluate_offers(profile, ctx, karaoke_options(case))


CAPACITY_CASES = {
    1: "Baseline",
    2: "Saturday booking chances fall to 40% and 30%",
    3: "A second room of the same size is free on Saturday",
    4: "Start time shifts so only the 80% session is blocked",
    5: "Requested slot overlaps a confirmed booking",
    6: "Customer asks for a S$35 cake",
}


def capacity_report(case: int) -> list[dict]:
    td = karaoke_case(case)
    rows = []
    by_id = {f.offer_id: f for f in td.ranked}
    for c in karaoke_options(case):
        f = by_id.get(c.offer_id)
        if f is None:
            rows.append({"option": c.label, "offerable": False, "reason": "overlaps a confirmed booking"})
            continue
        rows.append({
            "option": c.label,
            "offerable": True,
            "displaced": [(d.what, str(d.p), str(whole(d.value)), str(whole(d.amount)), d.note) for d in f.displaced if d.amount or d.note],
            "opportunity_cost": whole(f.opportunity_cost),
            "contribution_at_590": whole(f.contribution),
            "incremental_value": whole(f.incremental_value),
            "min_price": whole(f.min_price),
            "sendable_at_590": f.contribution >= max(f.opportunity_cost, f.min_contribution),
        })
    return rows


def format_capacity(case: int) -> str:
    out = [f"Case {case}: {CAPACITY_CASES[case]}"]
    rows = capacity_report(case)
    for r in rows:
        if not r["offerable"]:
            out.append(f"  {r['option']}: NOT OFFERABLE at any price ({r['reason']})")
            continue
        iv = fmt_money(r["incremental_value"], "S$")
        out.append(f"  {r['option']}: displaces S${r['opportunity_cost']}, minimum S${r['min_price']}, "
                   f"S$590 {'sendable' if r['sendable_at_590'] else 'not sendable'} at {'+' if r['incremental_value'] >= 0 else ''}{iv}")
        for what, p, value, amount, note in r["displaced"]:
            out.append(f"      - {what}: {int(D(p) * 100)}% x S${value} = S${amount} {('(' + note + ')') if note else ''}")
    best = [r for r in rows if r["offerable"] and r["sendable_at_590"]]
    blocked = [r for r in rows if r["offerable"] and not r["sendable_at_590"]]
    if best and blocked:
        out.append(f"  -> Quote {blocked[0]['option'].split(',')[0]} at S${blocked[0]['min_price']} and offer "
                   f"{best[0]['option'].split(',')[0]} at S$590")
    elif best:
        out.append(f"  -> S$590 works for: {', '.join(r['option'].split(',')[0] for r in best)}")
    if not any(r["offerable"] for r in rows[:1]):
        out.append("  -> Offer the nearest free slot instead")
    return "\n".join(out)


def format_salt() -> str:
    td = salt_worked_numbers()
    std = td.standard
    lines = ["Salt enquiry: 17 guests, S$600 floor, 4 bottles brought, standard close rate 40%",
             f"{'Offer':34} {'C if booked':>11} {'P(book)':>8} {'E':>6} {'Concession':>10} {'Break-even':>10}  Decision"]
    order = {f.offer_id: f for f in td.ranked}
    for oid in "ABCDE":
        f = order[oid]
        be = pct(f.breakeven_p_book) if f.breakeven_p_book is not None else "-"
        decision = {"keep_quote": "Keep the quote (baseline)", "offer_approved_concession": "Offer - approved (front line)",
                    "suggest_alternative": "Suggest alternative package", "rejected": f"Keep the quote - {f.rule}",
                    "escalate": "Escalate"}.get(f.decision, f.decision)
        lines.append(f"{oid}. {f.label:31} {fmt_money(f.contribution, 'S$'):>11} {pct(f.p_book):>8} "
                     f"{fmt_money(f.expected_contribution, 'S$'):>6} {fmt_money(f.concession_cost, 'S$'):>10} {be:>10}  {decision}")
    lines.append(f"Ranked by expected contribution: {' > '.join(f.offer_id for f in td.ranked)}")
    lines.append(f"Turn decision: {td.decision} -> {td.chosen.label if td.chosen else '-'}")
    return "\n".join(lines)
