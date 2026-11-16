"""Telegram end to end with an in-memory Bot API: customer chat, staff group, buttons, commands, watchers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dealdesk.agent.turn import Agent
from dealdesk.agent.watchers import tick
from dealdesk.channels.fake_telegram import FakeTelegram
from dealdesk.channels.telegram import TelegramRunner
from dealdesk.ledger.store import Store
from dealdesk.profile.loader import load_profile
from helpers import OPEN

TZ = ZoneInfo("Asia/Singapore")
GROUP, CUSTOMER = -100500, 777
MANAGER, OWNER, FRONT = 11, 12, 13


@pytest.fixture
def world():
    profile = load_profile("salt_beach_club")
    store = Store(":memory:")
    store.set_signal("salt", "stock", "cakes", 3, datetime(2026, 11, 13))
    agent = Agent(profile, store)
    tg = FakeTelegram()
    runner = TelegramRunner(agent, tg)
    t = datetime(2026, 11, 14, 15, 0)
    tg.staff_says(GROUP, MANAGER, "/setup", t, TZ)
    tg.staff_says(GROUP, MANAGER, "/iam Daniel", t, TZ)
    tg.staff_says(GROUP, OWNER, "/iam Owner", t, TZ, first_name="Boss")
    tg.staff_says(GROUP, FRONT, "/iam Aisha", t, TZ, first_name="Aisha")
    runner.poll_once()
    return profile, store, agent, tg, runner, t


def last_card(tg):
    return [m for m in tg.to(GROUP) if m["buttons"]][-1]


def test_customer_message_becomes_a_review_card_and_send_reaches_the_customer(world):
    profile, store, agent, tg, runner, t = world
    assert runner.staff_chat_id == GROUP
    tg.customer_says(CUSTOMER, OPEN, t + timedelta(minutes=1), TZ)
    runner.poll_once()
    assert tg.to(CUSTOMER) == []  # staff-review mode: nothing goes out unapproved
    card = last_card(tg)
    assert "Review #1" in card["text"] and "Why (staff only)" in card["text"] and "S$600 minimum spend" in card["text"]
    assert [b[0] for b in card["buttons"]] == ["Send", "Edit", "Reject"]
    tg.press(GROUP, FRONT, "rv:send:1", card["message_id"], "Aisha")
    runner.poll_once()
    assert "S$600 minimum spend" in tg.to(CUSTOMER)[-1]["text"]
    tg.press(GROUP, FRONT, "rv:send:1", card["message_id"], "Aisha")  # double tap
    runner.poll_once()
    assert len(tg.to(CUSTOMER)) == 1


def test_edit_and_reject_by_reply(world):
    profile, store, agent, tg, runner, t = world
    tg.customer_says(CUSTOMER, OPEN, t + timedelta(minutes=1), TZ)
    runner.poll_once()
    card = last_card(tg)
    tg.press(GROUP, FRONT, "rv:edit:1", card["message_id"], "Aisha")
    runner.poll_once()
    prompt = tg.to(GROUP)[-1]
    assert prompt["force_reply"]
    tg.staff_says(GROUP, FRONT, "Hi Mei! S$600 minimum for 17 on Sunday from 17:00, held until 17:01 today.",
                  t + timedelta(minutes=2), TZ, "Aisha", reply_to=prompt["message_id"])
    runner.poll_once()
    assert tg.to(CUSTOMER)[-1]["text"].startswith("Hi Mei! S$600 minimum")
    assert store.review(1)["status"] == "edited"  # logged as a training signal
    tg.customer_says(CUSTOMER, "Can you do 15% off?", t + timedelta(minutes=3), TZ)
    runner.poll_once()
    card = last_card(tg)
    tg.press(GROUP, FRONT, "rv:reject:2", card["message_id"], "Aisha")
    runner.poll_once()
    prompt = tg.to(GROUP)[-1]
    tg.staff_says(GROUP, FRONT, "wrong tone", t + timedelta(minutes=4), TZ, "Aisha", reply_to=prompt["message_id"])
    runner.poll_once()
    assert store.review(2)["status"] == "rejected" and store.review(2)["reason"] == "wrong tone"
    assert len(tg.to(CUSTOMER)) == 1


def test_escalation_card_manager_approves(world):
    profile, store, agent, tg, runner, t = world
    tg.staff_says(GROUP, OWNER, "/approve p_book.fee_concession 0.80", t, TZ, "Boss")
    runner.poll_once()
    assert "Approved p_book.fee_concession" in tg.to(GROUP)[-1]["text"]
    for i, text in enumerate([OPEN, "Can you waive the corkage?",
                              "One bottle isn't enough. Can you waive the corkage on all 4 bottles?"]):
        tg.customer_says(CUSTOMER, text, t + timedelta(minutes=1 + i), TZ)
        runner.poll_once()
        if i < 2:
            card = last_card(tg)
            tg.press(GROUP, FRONT, card["buttons"][0][1], card["message_id"], "Aisha")
            runner.poll_once()
    esc = [m for m in tg.to(GROUP) if m["buttons"] and m["buttons"][0][1].startswith("esc:")][-1]
    assert "Escalation #1" in esc["text"] and "Decide by 15:35" in esc["text"]
    hold_card = last_card(tg) if last_card(tg) is not esc else [m for m in tg.to(GROUP) if m["buttons"] and m["buttons"][0][1].startswith("rv:")][-1]
    tg.press(GROUP, FRONT, hold_card["buttons"][0][1], hold_card["message_id"], "Aisha")  # holding reply goes out
    tg.press(GROUP, FRONT, "esc:approve:1", esc["message_id"], "Aisha")  # front line cannot approve
    runner.poll_once()
    assert "back to you by 15:35" in tg.to(CUSTOMER)[-1]["text"]
    assert "cannot approve" in tg.callbacks[-1][1]
    tg.press(GROUP, MANAGER, "esc:approve:1", esc["message_id"])
    runner.poll_once()
    assert "corkage waived on 4 bottles" in tg.to(CUSTOMER)[-1]["text"]


def test_watcher_escalation_timeout_falls_back_and_staff_commands(world):
    profile, store, agent, tg, runner, t = world
    store.approve_calibration("salt", "p_book.fee_concession", __import__("decimal").Decimal("0.8"), "Owner", t)
    for i, text in enumerate([OPEN, "Can you waive the corkage?", "Please waive the corkage on all 4 bottles"]):
        tg.customer_says(CUSTOMER, text, t + timedelta(minutes=1 + i), TZ)
        runner.poll_once()
        if i < 2:
            card = last_card(tg)
            tg.press(GROUP, FRONT, card["buttons"][0][1], card["message_id"], "Aisha")
            runner.poll_once()
    outs = tick(agent, t + timedelta(minutes=40))  # nobody answered the escalation
    texts = " ".join(o.text for o in outs)
    assert "URGENT Review" in texts and "Escalation timed out" in texts
    assert "corkage waived on one bottle" in texts  # the best approved offer, not silence
    assert store.escalation(1)["status"] == "timed_out"
    # /deal for staff: the deal plus the reasoning, never sent to a customer
    tg.staff_says(GROUP, FRONT, "/deal 17 pax birthday Sunday 22 Nov 5pm, 3 beds and the bean bag section, want a discount",
                  t, TZ, "Aisha")
    runner.poll_once()
    reply = tg.to(GROUP)[-1]["text"]
    assert "Offer:" in reply and "Blocker: price" in reply and "Options:" in reply
    tg.staff_says(GROUP, FRONT, "/busy 2026-11-22", t, TZ, "Aisha")
    runner.poll_once()
    assert "HIGH DEMAND" in tg.to(GROUP)[-1]["text"]
    tg.staff_says(GROUP, FRONT, "/autosend keep_quote on", t, TZ, "Aisha")
    runner.poll_once()
    assert "Only the owner" in tg.to(GROUP)[-1]["text"]


def test_duplicate_update_and_other_groups_are_ignored(world):
    profile, store, agent, tg, runner, t = world
    u = tg.customer_says(CUSTOMER, OPEN, t + timedelta(minutes=1), TZ)
    runner.poll_once()
    runner.handle_update(u)  # Telegram redelivers the same update
    assert len(store.reviews()) == 1
    tg.staff_says(-999, 5, "hello bot", t, TZ)  # some other group
    runner.poll_once()
    assert not tg.to(-999)


def test_confirm_turns_hold_into_booking_and_tells_customer(world):
    profile, store, agent, tg, runner, t = world
    tg.customer_says(CUSTOMER, OPEN, t + timedelta(minutes=1), TZ)
    runner.poll_once()
    card = last_card(tg)
    tg.press(GROUP, FRONT, "rv:send:1", card["message_id"], "Aisha")
    runner.poll_once()
    tg.staff_says(GROUP, FRONT, f"/confirm salt:telegram:{CUSTOMER}", t + timedelta(minutes=3), TZ, "Aisha")
    runner.poll_once()
    assert tg.to(CUSTOMER)[-1]["text"].startswith("You're confirmed!")
    assert len(store.q("SELECT * FROM bookings WHERE status='confirmed'")) == 4
    assert store.outcomes("salt")[0]["booked"] == 1
