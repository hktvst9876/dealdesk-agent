"""Build Spec v2 figures, to the dollar: the capacity test and the Salt worked numbers."""
from decimal import Decimal

import pytest

from dealdesk.demo import capacity_report, karaoke_case, salt_worked_numbers
from dealdesk.money import one_dp, whole


def by_id(td):
    return {f.offer_id: f for f in td.ranked}


# --------------------------------------------------------------------------- capacity test

@pytest.mark.parametrize("case,sat,sun", [
    # case: (Saturday displaced, minimum, sendable, value) / (Sunday ...)
    (1, (510, 670, False, -80), (150, 310, True, 280)),
    (2, (210, 370, True, 220), (150, 310, True, 280)),
    (3, (0, None, True, 430), (150, 310, True, 280)),
    (4, (240, 400, True, 190), (150, 310, True, 280)),
    (6, (510, 705, False, -115), (150, 345, True, 245)),
])
def test_capacity_cases(case, sat, sun):
    rows = {r["option"].split(",")[0]: r for r in capacity_report(case)}
    for name, (displaced, minimum, sendable, value) in (("Saturday peak", sat), ("Sunday afternoon", sun)):
        r = rows[name]
        assert r["opportunity_cost"] == displaced, (case, name)
        if minimum is not None:
            assert r["min_price"] == minimum, (case, name)
        assert r["sendable_at_590"] is sendable, (case, name)
        assert r["incremental_value"] == value, (case, name)


def test_capacity_case_5_overlap_is_not_offerable_at_any_price():
    rows = capacity_report(5)
    assert rows[0]["offerable"] is False
    assert rows[1]["offerable"] is True and rows[1]["sendable_at_590"] is True


def test_capacity_recommendation_changes_with_demand_only():
    """Cases 1 and 2: same request, same price, different answer because demand changed."""
    base = {r["option"]: r for r in capacity_report(1)}
    low = {r["option"]: r for r in capacity_report(2)}
    sat = "Saturday peak, four hours"
    assert base[sat]["sendable_at_590"] is False and low[sat]["sendable_at_590"] is True


def test_no_option_touches_a_confirmed_booking():
    td = karaoke_case(5)
    assert all(f.offer_id != "SAT" for f in td.ranked)


# --------------------------------------------------------------------------- Salt worked numbers

EXPECTED = {
    # offer: contribution, p, E, concession, break-even, decision
    "A": (707, "0.40", 448, 0, "40.0", "keep_quote"),
    "B": (657, "0.50", 466, 50, "45.3", "offer_approved_concession"),
    "C": (672, "0.55", 494, 35, "43.5", "offer_approved_concession"),
    "D": (588, "0.55", 447, 119, "55.3", "rejected"),
    "E": (892, "0.35", 509, 0, "24.7", "suggest_alternative"),
}


@pytest.mark.parametrize("oid", list(EXPECTED))
def test_salt_worked_numbers(oid):
    f = by_id(salt_worked_numbers())[oid]
    c, p, e, conc, be, decision = EXPECTED[oid]
    assert whole(f.contribution) == c
    assert f.p_book == Decimal(p)
    assert whole(f.expected_contribution) == e
    assert whole(f.concession_cost) == conc
    assert one_dp(f.breakeven_p_book * 100) == Decimal(be)
    assert f.decision == decision


def test_salt_decision_schema_matches_spec_example():
    c = by_id(salt_worked_numbers())["C"].to_schema()
    assert c["revenue"] == 1135 and c["contribution"] == 671.5 and c["opportunity_cost"] == 276
    assert c["p_book"] == 0.55 and c["expected_contribution"] == pytest.approx(493.525)
    assert c["breakeven_p_book"] == pytest.approx(0.4354, abs=1e-4)
    assert c["concession_cost"] == 35 and c["cumulative_concession"] == 35
    assert c["checks"]["min_contribution"] and c["checks"]["session_rule"]
    assert c["checks"]["beats_standard"] and c["checks"]["within_cap"]
    assert c["authority_required"] == "front_line"
    assert c["decision"] == "offer_approved_concession" and c["sendable"] is True
    assert c["rule"] == "E(493.5) > E_standard(448.2); cumulative 35 <= front-line 100"


def test_vip_ranks_first_and_floor_discount_last():
    td = salt_worked_numbers()
    assert [f.offer_id for f in td.ranked] == ["E", "C", "B", "A", "D"]


def test_floor_discount_switches_to_conservative_spend():
    d = by_id(salt_worked_numbers())["D"]
    assert d.spend_case == "conservative"
    assert d.revenue_fnb == Decimal("765")  # 17 x S$45 beats the S$510 discounted floor
    assert d.authority_required == "manager"  # S$119 exceeds front-line S$100 anyway
