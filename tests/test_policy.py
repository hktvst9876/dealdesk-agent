"""Policy layer: validation on load, cumulative caps, authority, high demand, holds, callbacks."""
from datetime import datetime
from decimal import Decimal

import pytest
import yaml

from dealdesk.demo import SALT_NOW, SALT_START, salt_context, salt_placement
from dealdesk.engine.evaluate import evaluate_offers
from dealdesk.engine.types import CalendarView, Candidate, ConcessionLine
from dealdesk.money import whole
from dealdesk.policy.rules import callback_deadline, escalation_deadline, hold_expiry, in_hours, perk_eligible
from dealdesk.profile.loader import PROFILES_DIR, load_profile, load_profile_dict


@pytest.fixture(scope="module")
def salt():
    return load_profile("salt_beach_club")


def _salt_dict():
    return yaml.safe_load((PROFILES_DIR / "salt_beach_club.yaml").read_text())


# --------------------------------------------------------------------------- validation on load

def test_spec_welcome_drink_limit_is_rejected():
    """Front line to 10 guests x S$14 = S$140 exceeds front-line authority S$100."""
    data = _salt_dict()
    for p in data["perks"]:
        if p["id"] == "welcome_drink":
            p["approval"] = {"front_line": 10, "manager": 10}
    with pytest.raises(ValueError, match="welcome_drink: front_line may give 10"):
        load_profile_dict(data, PROFILES_DIR)


def test_manager_authority_below_front_line_is_rejected():
    data = _salt_dict()
    data["policy"]["authority"] = {"front_line": 300, "manager": 100}
    with pytest.raises(ValueError, match="manager authority"):
        load_profile_dict(data, PROFILES_DIR)


def test_unknown_fee_reference_is_rejected():
    data = _salt_dict()
    data["perks"].append({"id": "x", "label": "x", "type": "fee_waiver", "fee_id": "nope"})
    with pytest.raises(ValueError, match="unknown fee nope"):
        load_profile_dict(data, PROFILES_DIR)


# --------------------------------------------------------------------------- cumulative concessions

def _cand(oid, perks, new):
    return Candidate(oid, oid, "concession", salt_placement(), concessions=[ConcessionLine(p, q) for p, q in perks], new_concessions=new)


def test_second_request_is_evaluated_as_waiver_plus_new_ask(salt):
    """Case 8: waiver given, now asks for cake: S$50 + S$35 = S$85, inside front-line S$100."""
    td = evaluate_offers(salt, salt_context(salt, "occasion"), [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("W+C", [("corkage_waiver", 1), ("cake", 1)], ["cake"]),
    ])
    f = {x.offer_id: x for x in td.ranked}["W+C"]
    assert whole(f.cumulative_concession) == 85
    assert f.authority_required == "front_line" and f.sendable
    assert td.decision == "offer_approved_concession"


def test_third_request_does_not_reset_the_allowance(salt):
    """Case 9: + prosecco S$68 -> cumulative S$153 > cap S$150; not worth it -> keep the quote."""
    td = evaluate_offers(salt, salt_context(salt, "occasion"), [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("W+C+P", [("corkage_waiver", 1), ("cake", 1), ("prosecco", 1)], ["prosecco"]),
    ])
    f = {x.offer_id: x for x in td.ranked}["W+C+P"]
    assert whole(f.cumulative_concession) == 153
    assert f.checks["within_cap"] is False
    assert not f.sendable
    assert td.decision == "keep_quote"


def test_four_bottle_waiver_at_default_close_rate_is_declined_not_escalated(salt):
    """Spec case 7 as written: S$200 waiver needs a 74.7% close rate, so the standard wins."""
    td = evaluate_offers(salt, salt_context(salt, "fee"), [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("W4", [("corkage_waiver", 4)], ["corkage_waiver"]),
    ])
    f = {x.offer_id: x for x in td.ranked}["W4"]
    assert whole(f.cumulative_concession) == 200
    assert str(round(f.breakeven_p_book * 100, 1)) == "74.7"
    assert f.decision == "rejected" and td.decision == "keep_quote"


def test_cheapest_concession_that_unblocks_wins(salt):
    """At the same close rate, one bottle beats four: never give more than the booking needs."""
    ctx = salt_context(salt, "fee", p_overrides={"fee_concession": Decimal("0.80")})
    td = evaluate_offers(salt, ctx, [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("W1", [("corkage_waiver", 1)], ["corkage_waiver"]),
        _cand("W4", [("corkage_waiver", 4)], ["corkage_waiver"]),
    ])
    assert td.decision == "offer_approved_concession" and td.chosen.offer_id == "W1"


def test_profitable_concession_over_authority_escalates(salt):
    """Spec case 7 premise: the customer turned down one bottle and the 4-bottle waiver IS
    profitable (calibrated close rate 80%), so it goes to a manager with the standard as fallback."""
    ctx = salt_context(salt, "fee", p_overrides={"fee_concession": Decimal("0.80")})
    td = evaluate_offers(salt, ctx, [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("W4", [("corkage_waiver", 4)], ["corkage_waiver"]),
    ])
    assert td.decision == "escalate"
    assert td.chosen.offer_id == "W4"
    assert td.chosen.authority_required == "manager"
    assert td.fallback.offer_id == "A"  # best approved offer if the manager does not answer in time
    assert "cap 150" in td.chosen.rule


def test_vip_upgrade_at_bed_floor_needs_a_manager(salt):
    from dealdesk.demo import salt_vip_placement

    td = evaluate_offers(salt, salt_context(salt, "fit"), [
        Candidate("A", "standard", "standard", salt_placement()),
        Candidate("V", "VIP at bed floor", "alternative", salt_vip_placement(), floor=Decimal("600"),
                  concessions=[ConcessionLine("vip_upgrade_bed_tier", 1)], new_concessions=["vip_upgrade_bed_tier"]),
    ])
    v = {x.offer_id: x for x in td.ranked}["V"]
    assert v.authority_required == "manager" and v.escalatable


def test_high_demand_blocks_price_discounts_but_not_perks(salt):
    ctx = salt_context(salt, "price", calendar=CalendarView(high_demand={"2026-11-22"}, stock={"cakes": 2}))
    td = evaluate_offers(salt, ctx, [
        Candidate("A", "standard", "standard", salt_placement()),
        _cand("D", [("floor_discount", 1)], ["floor_discount"]),
        _cand("C", [("cake", 1)], ["cake"]),
    ])
    f = {x.offer_id: x for x in td.ranked}
    assert f["D"].checks["session_rule"] is False and f["D"].decision == "rejected"
    assert f["C"].checks["session_rule"] is True and f["C"].sendable


# --------------------------------------------------------------------------- eligibility

def test_cake_needs_kitchen_stock(salt):
    ok, _ = perk_eligible(salt, salt.perk("cake"), party_size=17, occasion="birthday", placement=salt_placement(),
                          calendar=CalendarView(stock={"cakes": 1}), now=SALT_NOW)
    assert ok
    ok, why = perk_eligible(salt, salt.perk("cake"), party_size=17, occasion="birthday", placement=salt_placement(),
                            calendar=CalendarView(stock={"cakes": 0}), now=SALT_NOW)
    assert not ok and "cakes" in why


def test_floor_discount_only_inside_matrix(salt):
    # 8 days out, 50% utilisation: utilisation under 60% -> eligible
    ok, _ = perk_eligible(salt, salt.perk("floor_discount"), party_size=17, occasion=None, placement=salt_placement(),
                          calendar=CalendarView(), now=SALT_NOW)
    assert ok
    busy = CalendarView(utilisation={"2026-11-22:weekend_eve": Decimal("0.7")})
    ok, _ = perk_eligible(salt, salt.perk("floor_discount"), party_size=17, occasion=None, placement=salt_placement(),
                          calendar=busy, now=SALT_NOW)
    assert not ok


# --------------------------------------------------------------------------- time rules

def test_hold_rules(salt):
    afternoon = datetime(2026, 11, 14, 15, 0)
    assert hold_expiry(salt, afternoon, SALT_START, False) == datetime(2026, 11, 14, 17, 0)
    evening = datetime(2026, 11, 14, 20, 39)
    assert hold_expiry(salt, evening, SALT_START, False) == datetime(2026, 11, 15, 10, 0)
    assert hold_expiry(salt, afternoon, SALT_START, True) == datetime(2026, 11, 14, 15, 30)
    # Never past session start minus 3 hours; none at all when the session is too close.
    assert hold_expiry(salt, datetime(2026, 11, 22, 13, 30), SALT_START, False) == datetime(2026, 11, 22, 14, 0)
    assert hold_expiry(salt, datetime(2026, 11, 22, 14, 30), SALT_START, False) is None


def test_callback_deadlines(salt):
    now = datetime(2026, 11, 22, 14, 0)
    assert callback_deadline(salt, now, datetime(2026, 11, 22, 17, 0)) == datetime(2026, 11, 22, 14, 10)
    assert callback_deadline(salt, SALT_NOW, SALT_START) == datetime(2026, 11, 14, 15, 30)


def test_escalation_deadline_is_a_clean_time(salt):
    assert escalation_deadline(salt, datetime(2026, 11, 14, 15, 2), SALT_START) == datetime(2026, 11, 14, 15, 35)


def test_out_of_hours(salt):
    assert in_hours(salt, datetime(2026, 11, 14, 15, 0))
    assert not in_hours(salt, datetime(2026, 11, 14, 20, 39))  # Sat 20:39, desk closes 20:00
