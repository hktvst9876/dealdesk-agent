"""The economics beyond Salt: stock, staff hours, cash terms, P&L posture, calibration, Excel profiles."""
from datetime import datetime
from decimal import Decimal

import pytest
import yaml

from dealdesk.engine.evaluate import evaluate_offers
from dealdesk.engine.opportunity import _poisson_tail, stock_economics
from dealdesk.engine.types import Candidate, ConcessionLine, DealContext, Line, Placement, PlacementPart
from dealdesk.finance.calibration import propose_close_rates, propose_spend
from dealdesk.finance.posture import derive_posture
from dealdesk.money import whole
from dealdesk.profile.loader import PROFILES_DIR, load_profile, load_profile_dict
from dealdesk.profile.loader_xlsx import load_profile_xlsx, profile_dict_to_xlsx


def ranked(td):
    return {f.offer_id: f for f in td.ranked}


# --------------------------------------------------------------------------- stock

def test_poisson_tail():
    assert _poisson_tail(0, 1) == 0.0
    assert _poisson_tail(5, 0) == 1.0
    assert abs(_poisson_tail(10, 6) - 0.9329) < 1e-3


def test_perishable_stock_is_nearly_free_scarce_stock_is_not():
    bar = load_profile("neighbourhood_bar")
    ctx = DealContext(now=datetime(2026, 11, 17, 16, 0))
    v, o, _ = stock_economics(bar, ctx, bar.product("dessert_of_day"), 6)
    assert v == 0  # already paid for: sunk
    assert o < 10  # 6 desserts that mostly would have gone in the bin
    v, o, _ = stock_economics(bar, ctx, bar.product("prosecco_bottle"), 1)
    assert v == 24  # replenishable: replacement cost
    assert o > 45  # last bottles before the restock would have sold at full price


def _bar_cands(day):
    pl = Placement([PlacementPart("table", 1)], day, day.replace(hour=22, minute=30))
    mk = lambda oid, perks: Candidate(oid, oid, "concession", pl, concessions=[ConcessionLine(p, q) for p, q in perks])
    return [Candidate("std", "standard", "standard", pl), mk("platter", [("platter_with_minimum", 1)]),
            mk("dessert", [("dessert_round", 6)]), mk("happy_hour", [("happy_hour_bill", 1)])]


def test_bar_prefers_a_deal_that_changes_behaviour_over_a_bill_discount():
    bar = load_profile("neighbourhood_bar")
    day = datetime(2026, 11, 17, 19, 30)  # quiet Tuesday
    td = evaluate_offers(bar, DealContext(now=day.replace(hour=16), party_size=6, blocker="price"), _bar_cands(day))
    f = ranked(td)
    assert td.chosen.offer_id == "platter"  # S$300 minimum spend + platter: lifts spend, costs nothing net
    assert f["platter"].concession_cost == 0
    assert f["happy_hour"].decision == "rejected"  # 15% off what they would spend anyway loses money


def test_bar_busy_friday_blocks_bill_discount():
    bar = load_profile("neighbourhood_bar")
    day = datetime(2026, 11, 20, 19, 30)
    td = evaluate_offers(bar, DealContext(now=day.replace(hour=16), party_size=6, blocker="price"), _bar_cands(day))
    f = ranked(td)
    assert f["std"].high_demand
    assert f["happy_hour"].decision == "rejected"
    assert f["std"].opportunity_cost > 100  # a Friday table resells


# --------------------------------------------------------------------------- wholesale: stock + cash

def _ws_cands():
    basket = [Line("house_red_case", 20), Line("premium_gin_case", 2)]
    mk = lambda oid, perk: Candidate(oid, oid, "concession", pricing="basket", basket=basket, concessions=[ConcessionLine(perk)])
    return [Candidate("std", "standard", "standard", pricing="basket", basket=basket),
            mk("v5", "volume_5"), mk("v10", "volume_10"), mk("keg", "keg_on_us"), mk("c30", "credit_30"), mk("c14", "credit_14")]


def test_wholesale_scarce_gin_and_payment_terms():
    ws = load_profile("retail_wholesale")
    td = evaluate_offers(ws, DealContext(now=datetime(2026, 11, 17, 10), blocker="price"), _ws_cands())
    f = ranked(td)
    gin = [d for d in f["std"].displaced if "gin" in d.what][0]
    assert gin.amount > 200  # 2 of the last 6 cases would have sold at full price before restock
    # 30 days of credit on ~S$4.5k: ~S$44 carrying cost at 12% + ~S$45 expected bad debt
    assert 80 < f["c30"].concession_cost < 100
    assert f["c30"].authority_required == "manager"
    assert f["v10"].concession_cost > 400 and not f["v10"].sendable
    assert f["keg"].concession_cost > 0  # short-dated keg costs its salvage value, not nothing


def test_tight_cash_makes_credit_more_expensive():
    ws_data = yaml.safe_load((PROFILES_DIR / "retail_wholesale.yaml").read_text())
    ws_data["finance"] = {"pnl": {"period": "2026-11", "days_in_period": 30, "days_elapsed": 10, "revenue_to_date": 90000,
                                  "cogs_to_date": 60000, "fixed_costs_period": 40000, "cash_on_hand": 20000,
                                  "monthly_cash_burn": 15000}}
    ws = load_profile_dict(ws_data, PROFILES_DIR)
    post = derive_posture(ws)
    assert post.prefer_deposits and post.cost_of_capital == Decimal("0.25")
    calm = evaluate_offers(load_profile("retail_wholesale"), DealContext(now=datetime(2026, 11, 17, 10)), _ws_cands())
    tight = evaluate_offers(ws, DealContext(now=datetime(2026, 11, 17, 10), posture=post), _ws_cands())
    assert ranked(tight)["c30"].concession_cost > ranked(calm)["c30"].concession_cost


# --------------------------------------------------------------------------- services: hours

def _salon(day):
    sal = load_profile("salon_services")
    basket = [Line("bridal_updo", 1), Line("cut_blowdry", 4)]
    pl = Placement([PlacementPart("stylist_time", hours=Decimal("5.25"))], day, day.replace(hour=13))
    ctx = DealContext(now=datetime(2026, 11, 16, 12), party_size=5, occasion="wedding", blocker="price")
    cands = [Candidate("std", "standard", "standard", pl, pricing="basket", basket=basket),
             Candidate("mask", "mask", "concession", pl, pricing="basket", basket=basket, concessions=[ConcessionLine("mask_for_group", 5)]),
             Candidate("g15", "15%", "concession", pl, pricing="basket", basket=basket, concessions=[ConcessionLine("group_15")])]
    return evaluate_offers(sal, ctx, cands)


def test_salon_quiet_tuesday_vs_full_saturday():
    tue, sat = ranked(_salon(datetime(2026, 11, 17, 10))), ranked(_salon(datetime(2026, 11, 21, 10)))
    assert tue["std"].opportunity_cost == 0  # idle stylist time costs nothing
    assert tue["g15"].sendable
    assert sat["std"].opportunity_cost > 100  # Saturday hours displace paying regulars (or cost overtime)
    assert sat["g15"].checks["session_rule"] is False  # full day: no price discount
    assert sat["mask"].concession_cost > tue["mask"].concession_cost  # the same perk costs more when time is scarce


# --------------------------------------------------------------------------- P&L posture

def test_posture_behind_pace_relaxes_quiet_minimum_within_bound():
    bar = load_profile("neighbourhood_bar")
    post = derive_posture(bar)
    assert post.pace_label == "behind pace"
    assert Decimal("0.80") <= post.min_contribution_multiplier < 1
    day = datetime(2026, 11, 17, 19, 30)
    with_p = evaluate_offers(bar, DealContext(now=day.replace(hour=16), party_size=6, posture=post), _bar_cands(day))
    assert ranked(with_p)["std"].min_contribution < 60  # quiet Tuesday: relaxed
    fri = datetime(2026, 11, 20, 19, 30)
    busy = evaluate_offers(bar, DealContext(now=fri.replace(hour=16), party_size=6, posture=post), _bar_cands(fri))
    assert ranked(busy)["std"].min_contribution == 60  # busy Friday: never relaxed


def test_posture_cost_drift_and_budget():
    data = yaml.safe_load((PROFILES_DIR / "neighbourhood_bar.yaml").read_text())
    data["finance"]["pnl"].update(cogs_to_date=14000, concessions_to_date=1600)  # COGS 36.8% vs 28% configured
    bar = load_profile_dict(data, PROFILES_DIR)
    post = derive_posture(bar)
    assert post.cogs_pct is not None and post.cogs_pct > Decimal("0.36")
    assert post.concession_budget_remaining < 0
    day = datetime(2026, 11, 17, 19, 30)
    td = evaluate_offers(bar, DealContext(now=day.replace(hour=16), party_size=6, posture=post), _bar_cands(day))
    assert ranked(td)["dessert"].authority_required == "manager"  # budget spent: concessions need a manager


# --------------------------------------------------------------------------- calibration

def test_calibration_needs_enough_outcomes_and_shrinks_toward_prior():
    current = {"perk": Decimal("0.55")}
    assert propose_close_rates(current, [("perk", True)] * 10) == []
    props = propose_close_rates(current, [("perk", True)] * 24 + [("perk", False)] * 16)  # 60% observed
    assert len(props) == 0 or props[0].proposed < Decimal("0.60")
    props = propose_close_rates(current, [("perk", True)] * 30 + [("perk", False)] * 10)  # 75% observed
    assert props and Decimal("0.60") < props[0].proposed < Decimal("0.75")


def test_spend_calibration():
    props = propose_spend(Decimal("55"), Decimal("45"), [Decimal(x) for x in [60, 62, 58, 61, 59] * 5])
    assert {p.key for p in props} == {"spend_per_guest.base", "spend_per_guest.conservative"}


# --------------------------------------------------------------------------- Excel round trip

@pytest.mark.parametrize("name", ["salt_beach_club", "neighbourhood_bar", "retail_wholesale", "salon_services", "karaoke_demo"])
def test_excel_round_trip(tmp_path, name):
    src = PROFILES_DIR / f"{name}.yaml"
    data = yaml.safe_load(src.read_text())
    original = load_profile(src)
    out = profile_dict_to_xlsx(data, tmp_path / f"{name}.xlsx", original.resell_matrix)
    again = load_profile_xlsx(out)
    a = original.model_dump(exclude={"resell_matrix", "policy"})
    b = again.model_dump(exclude={"resell_matrix", "policy"})
    assert a == b
    assert again.policy.model_dump(exclude={"p_resell_matrix"}) == original.policy.model_dump(exclude={"p_resell_matrix"})
    assert again.resell_matrix == original.resell_matrix
