"""Salt acceptance cases 1-14 (Build Spec v2), end to end through the agent with the offline brain.

Each case checks the expected decision, the engine figures, and the shape of the reply.
"""
from datetime import datetime, timedelta

import pytest

from dealdesk.agent.checker import check
from helpers import OPEN, Convo


def test_case_01_ready_at_600_gets_confirmation_step():
    c = Convo()
    r = c.say("Hi, this is Mei. Mum's birthday on Sunday 22 Nov, 5pm till late, 17 pax, 3 beds and the bean bag "
              "section, bringing 4 bottles of wine. We're happy with the minimum spend, ready to book.")
    f = c.chosen(r)
    assert r.decision == "keep_quote"
    assert round(f["contribution"]) == 707 or f["contribution"] == 706.5
    assert "S$35 a head" in r.draft  # what the minimum covers, all toward F&B
    assert "https://pay.example.com/salt?ref=H-" in r.draft
    assert "cancellation" in r.draft.lower()
    assert "until 17:01 today" in r.draft  # genuine hold expiry
    assert c.store.active_holds()


def test_case_02_corkage_blocker_gets_one_bottle_waiver():
    c = Convo()
    c.say(OPEN)
    r = c.say("The corkage is the problem, S$200 just for our own wine. Can you waive it?")
    f = c.chosen(r)
    assert r.decision == "offer_approved_concession"
    assert f["contribution"] == 656.5 and f["concession_cost"] == 50
    assert round(f["breakeven_p_book"], 3) == 0.453
    assert f["authority_required"] == "front_line"
    assert "corkage waived on one bottle" in r.draft
    assert "the other 3 are S$50 each" in r.draft  # the remaining corkage
    assert "if you confirm today" in r.draft  # the condition
    assert "held it for you until" in r.draft


@pytest.mark.parametrize("cakes,expect_cake", [(3, True), (0, False)])
def test_case_03_birthday_gesture_gets_cake_after_kitchen_check(cakes, expect_cake):
    c = Convo(cakes=cakes)
    c.say(OPEN)
    r = c.say("It's mum's 70th birthday, could you do something special for her?")
    if expect_cake:
        f = c.chosen(r)
        assert r.decision == "offer_approved_concession"
        assert f["contribution"] == 671.5 and round(f["expected_contribution"]) == 494
        assert round(f["breakeven_p_book"], 3) == 0.435
        assert "a birthday cake on the house" in r.draft
        assert "35" not in r.draft.replace("S$35 a head", "")  # no mention of what it costs us
    else:
        assert "cake" not in r.draft.lower()  # never promise what the kitchen cannot deliver


def test_case_04_seating_or_weather_gets_clarity_not_a_concession():
    c = Convo()
    c.say(OPEN)
    r = c.say("What if it rains? And will we all be seated together?")
    f = c.chosen(r)
    assert r.decision == "keep_quote"
    assert f["contribution"] == 706.5 and f["concession_cost"] == 0  # no contribution change
    assert "side by side" in r.draft  # verified layout
    assert "indoors at no charge" in r.draft and "subject to indoor availability" in r.draft


def test_case_05_fifteen_percent_off_is_declined_with_a_no_cost_alternative():
    c = Convo()
    c.say(OPEN)
    r = c.say("Can you do 15% off the minimum spend?")
    ranked = {x["offer_id"]: x for x in r.figures}
    d = ranked["FLOOR_DISCOUNT"]
    assert d["contribution"] == 587.5 and round(d["expected_contribution"], 1) == 447.3
    assert d["concession_cost"] == 119 and d["sendable"] is False
    assert "S$35 a head" in r.draft  # what S$600 buys per head
    assert "cake" in r.draft.lower() or "corkage waived" in r.draft  # a no-cost alternative instead
    assert "I'm not able to" in r.draft


def test_case_06_vip_question_gets_alternative_package():
    c = Convo()
    c.say(OPEN)
    r = c.say("What about the VIP area?")
    ranked = r.figures
    assert ranked[0]["offer_id"] == "VIP_GROUP_RATE"  # rank 1 on expected contribution
    vip = ranked[0]
    assert vip["contribution"] == 892 and round(vip["expected_contribution"]) == 509 and vip["concession_cost"] == 0
    assert r.decision == "suggest_alternative"
    assert "S$1,200 minimum spend" in r.draft and "privacy" in r.draft and "older guests" in r.draft
    assert "held it for you until" in r.draft


def test_case_07_profitable_concession_over_authority_escalates_then_manager_decides():
    c = Convo()
    c.approve_p("p_book.fee_concession", "0.80")  # calibrated from the venue's own outcomes
    c.say(OPEN)
    c.say("Can you waive the corkage?")
    r = c.say("One bottle isn't enough. Can you waive the corkage on all 4 bottles?")
    assert r.decision == "escalate"
    assert "checking one thing with my manager" in r.draft and "by 15:35 today" in r.draft
    esc = c.store.escalation(r.escalation_id)
    p = esc["packet"]
    assert p["cumulative"] == "200" and p["authority_required"] == "manager"
    assert "cap 150" in p["blocked_by"]
    assert p["assumptions"]["p_book"] == "0.80" and p["deadline"].endswith("15:35")
    # Front line cannot approve; the manager can.
    assert "cannot approve" in c.agent.decide_escalation(r.escalation_id, True, "Aisha", "front_line", c.t)[0].text
    out = c.agent.decide_escalation(r.escalation_id, True, "Daniel", "manager", c.t + timedelta(minutes=5))
    customer = [o for o in out if o.to == "customer"][0].text
    assert "corkage waived on 4 bottles" in customer
    # A second tap changes nothing.
    assert "already decided" in c.agent.decide_escalation(r.escalation_id, True, "Daniel", "manager", c.t)[0].text


def test_case_08_and_09_cumulative_concessions_never_reset():
    c = Convo()
    c.say(OPEN)
    c.say("Can you waive the corkage?")
    r8 = c.say("Could you throw in a cake for the birthday too?")
    f8 = c.chosen(r8)
    assert r8.decision == "offer_approved_concession"
    assert f8["cumulative_concession"] == 85 and f8["authority_required"] == "front_line"
    assert "corkage waived on one bottle" in r8.draft and "birthday cake" in r8.draft
    r9 = c.say("And a bottle of prosecco?")
    ranked = {x["offer_id"]: x for x in r9.figures}
    assert ranked["PROSECCO"]["cumulative_concession"] == 153 and ranked["PROSECCO"]["sendable"] is False
    assert r9.decision == "keep_quote"
    assert "not able to add a bottle of prosecco" in r9.draft
    assert "already includes corkage waived on one bottle" in r9.draft and "and a birthday cake" in r9.draft  # as it stands


def test_case_10_call_request_assigns_named_staff_with_short_deadline():
    c = Convo(start=datetime(2026, 11, 22, 13, 59))
    c.say("Hi, this is Mei, today 5pm till late for mum's birthday, 17 pax, 3 beds and the bean bag section, 4 bottles of wine.")
    r = c.say("Any way to call you?")
    assert r.decision == "callback"
    assert "Aisha will call you by 14:11 today" in r.draft  # session under 4 hours away: 10 minutes
    assert "S$600 minimum spend" in r.draft  # the offer restated in chat meanwhile
    cb = c.store.callbacks()[0]
    assert cb["staff_name"] == "Aisha" and cb["deadline"].endswith("14:11:00")


def test_case_11_out_of_hours_gets_substantive_reply_and_overnight_hold():
    c = Convo(start=datetime(2026, 11, 14, 20, 38))
    r = c.say(OPEN)
    assert "groups of up to 8" in r.draft  # the online cap explained
    assert "3 beachfront beds and the bean bag section: S$600 minimum spend" in r.draft  # price list for the group
    assert "until 10:00 tomorrow" in r.draft  # hold until 10:00
    assert "call you back from 09:00 tomorrow" in r.draft
    review = c.store.reviews()[0]
    assert review["status"] == "sent"
    from dealdesk.policy.rules import reply_sla_minutes
    assert reply_sla_minutes(c.profile, c.t) == 60


def test_case_12_high_demand_gets_perks_only_and_alternatives():
    c = Convo()
    c.store.set_signal("salt", "high_demand", "2026-11-22", "1", c.t)
    c.say(OPEN)
    r = c.say("Can you do 15% off the minimum spend?")
    ranked = {x["offer_id"]: x for x in r.figures}
    assert ranked["FLOOR_DISCOUNT"]["checks"]["session_rule"] is False
    assert "not possible" not in r.draft.lower()
    assert "birthday cake" in r.draft  # a perk instead
    assert "Another option" in r.draft  # a different session or the VIP area
    assert "until 15:31 today" in r.draft  # holds shortened to 30 minutes on high-demand sessions


def test_case_13_availability_changed_between_quote_and_confirmation():
    c = Convo()
    c.say(OPEN)
    c.t += timedelta(hours=3)  # the 2-hour hold lapses ...
    c.store.add_booking("salt", "beanbag_section", "beanbag_section#1", datetime(2026, 11, 22, 17), datetime(2026, 11, 22, 22),
                        source="staff", now=c.t)  # ... and someone else books the bean bag section
    r = c.say("ok let's book it")
    assert r.decision in ("keep_quote", "unavailable")
    assert "just taken" in r.draft
    assert "nearest equivalent" in r.draft or "another date" in r.draft
    assert not any(h["unit_id"] == "beanbag_section#1" and h["start"].startswith("2026-11-22T17") for h in c.store.active_holds())


def test_case_14_duplicate_inbound_has_no_duplicate_side_effects():
    c = Convo(auto_staff_send=False)
    r1 = c.say(OPEN, message_id="m-1")
    r2 = c.say(OPEN, minutes=0, message_id="m-1")
    assert r2.duplicate and not r2.outbound
    assert len(c.store.reviews()) == 1
    assert len(c.store.offers(r1.conversation_id)) == 1
    assert len({h["id"] for h in c.store.active_holds()}) == 4  # 3 beds + bean bags, once
    # Double-tap Send in the staff chat: one customer message.
    out1 = c.agent.staff_send(r1.review_id, "Aisha", c.t)
    out2 = c.agent.staff_send(r1.review_id, "Aisha", c.t)
    assert sum(o.to == "customer" for o in out1) == 1 and sum(o.to == "customer" for o in out2) == 0


def test_every_template_reply_in_the_cases_passes_the_checker():
    """The template writer is the safe fallback, so it must never fail the checker."""
    c = Convo()
    for msg in [OPEN, "Can you waive the corkage?", "Could you throw in a cake too?", "and prosecco?",
                "Can you do 15% off?", "What about the VIP area?", "What if it rains?", "Any way to call you?", "ok let's book it"]:
        r = c.say(msg)
        assert not any(n.startswith("TEMPLATE FAILED") for n in r.notes), (msg, r.notes, r.draft)
