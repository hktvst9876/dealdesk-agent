"""The checker, the Claude integration (mocked API client), and chats for non-Salt businesses."""
from datetime import datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from dealdesk.agent.brief import OfferLine, ReplyBrief
from dealdesk.agent.checker import check
from dealdesk.agent.claude_brain import ClaudeBrain
from dealdesk.agent.models import ReplyText, Understanding
from dealdesk.money import D
from dealdesk.profile.loader import load_profile
from helpers import OPEN, Convo

NOW = datetime(2026, 11, 14, 15, 0)


def _brief(**kw):
    main = OfferLine("3 beachfront beds and the bean bag section, Sunday 22 Nov from 17:00", "a S$600 minimum spend", D(600),
                     D(35), ["a birthday cake on the house"], ["confirm today"], ["cake"])
    b = ReplyBrief(kind="offer", business="Salt", symbol="S$", tone="", now=NOW, name="Mei", party=17, main=main,
                   hold_expiry=datetime(2026, 11, 14, 17, 0), next_step="Reply 'confirm'.")
    for k, v in kw.items():
        setattr(b, k, v)
    return b


GOOD = ("Hi Mei! For your group of 17, the 3 beachfront beds and the bean bag section are yours on Sunday 22 Nov from 17:00 "
        "at a S$600 minimum spend (about S$35 a head), with a birthday cake on the house if you confirm today. "
        "Held until 17:00 today. Reply 'confirm'.")


@pytest.mark.parametrize("bad,why", [
    (GOOD.replace("S$600", "S$550"), "number 550"),
    (GOOD + " Our margin is tight.", "margin"),
    (GOOD + " A discount is not possible.", "not possible"),
    (GOOD + " Prosecco is on us too.", "prosecco"),
    (GOOD.replace("17:00 today", "18:30 today"), "time 18:30"),
    (GOOD + " Book fast, it's the last bed!", "invents urgency"),
    (GOOD + " Most groups book at 40%.", "number 40"),
])
def test_checker_blocks_bad_drafts(bad, why):
    salt = load_profile("salt_beach_club")
    res = check(salt, _brief(), bad)
    assert not res.ok and why in res.feedback()


def test_checker_passes_a_faithful_draft_and_requires_a_hold_for_expiry_talk():
    salt = load_profile("salt_beach_club")
    assert check(salt, _brief(), GOOD).ok
    no_hold = _brief(hold_expiry=None)
    res = check(salt, no_hold, GOOD)
    assert not res.ok and "no hold exists" in res.feedback()


# --------------------------------------------------------------------------- Claude integration, mocked

class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(script):
    msgs = FakeMessages(script)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def _resp(obj, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, parsed_output=obj)


def _understanding():
    return Understanding(customer_first_name="Mei", date="2026-11-22", start_time="17:00", party_size=17, occasion="birthday",
                         seating=[{"resource_id": "beachfront_bed", "units": 3}, {"resource_id": "beanbag_section", "units": 1}],
                         byo_bottles=4)


def test_claude_request_shape_and_checked_wording():
    good = ("Hi Mei! 3 beachfront beds and the bean bag section, Sunday 22 Nov from 17:00, at a S$600 minimum spend: "
            "about S$35 a head, all toward food and drinks. I've held it until 17:01 today. Reply 'confirm' to book.")
    client, msgs = _client([_resp(_understanding()), _resp(ReplyText(text="Only S$550 for you! Our margin allows it.")),
                            _resp(ReplyText(text=good))])
    c = Convo(brain=ClaudeBrain(client=client))
    r = c.say(OPEN)
    assert r.draft == good
    assert any("checker rejected draft 1" in n for n in r.notes)
    first = msgs.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in first["betas"] and first["output_format"] is Understanding
    assert first["output_config"] == {"effort": "low"}
    assert "Perk catalogue" in first["system"] and "corkage_waiver" in first["system"]
    # The wording call sees the brief, never raw engine internals.
    brief_text = msgs.calls[1]["messages"][0]["content"]
    assert "S$600 minimum spend" in brief_text and "contribution" not in brief_text and "p_book" not in brief_text


def test_claude_down_falls_back_to_rules_and_template():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = _client([err, err])
    c = Convo(brain=ClaudeBrain(client=client))
    r = c.say(OPEN)
    assert r.decision == "keep_quote" and "S$600 minimum spend" in r.draft
    assert any("model unavailable" in n for n in r.notes)


def test_claude_twice_rejected_uses_template():
    client, _ = _client([_resp(_understanding()), _resp(ReplyText(text="S$1 deal!")), _resp(ReplyText(text="S$2 deal!"))])
    c = Convo(brain=ClaudeBrain(client=client))
    r = c.say(OPEN)
    assert "S$600 minimum spend" in r.draft and "S$1 deal" not in r.draft


def test_claude_refusal_is_treated_as_unavailable():
    client, _ = _client([_resp(None, stop="refusal"), _resp(None, stop="refusal")])
    c = Convo(brain=ClaudeBrain(client=client))
    r = c.say(OPEN)
    assert r.decision == "keep_quote"


# --------------------------------------------------------------------------- other businesses

def test_bar_quiet_night_deal_and_staff_confirmation():
    c = Convo(profile="neighbourhood_bar", start=datetime(2026, 11, 17, 16, 30))
    r = c.say("Hi! 6 of us coming tonight around 7:30pm, any deals on drinks?")
    assert r.decision == "offer_approved_concession"
    assert "S$300 minimum spend" in r.draft and "sharing platter on us" in r.draft
    r = c.say("sounds good, book it")
    assert "Our team will confirm it" in r.draft  # no paylink configured: staff confirm


def test_wholesale_price_ask_and_payment_terms_escalation():
    c = Convo(profile="retail_wholesale", start=datetime(2026, 11, 17, 10, 0))
    r = c.say("Hi, I'd like 20 cases of house red and 2 cases of premium gin. Can you do a better price?")
    assert r.decision == "offer_approved_concession" and "keg" in r.draft
    assert "%" not in r.draft  # a discount on scarce gin and on what they'd buy anyway is not offered
    r = c.say("Could we get 30 days to pay?")
    assert r.decision == "escalate"


def test_salon_same_group_different_day():
    tue = Convo(profile="salon_services", start=datetime(2026, 11, 17, 9, 0))
    r = tue.say("Hi! Bridal party today at 10am: 1 bridal updo and 4 blow-dries. Any group discount?")
    ranked = {f["offer_id"]: f for f in r.figures}
    assert ranked["GROUP_15"]["sendable"]
    sat = Convo(profile="salon_services", start=datetime(2026, 11, 17, 9, 0))
    r = sat.say("Hi! Bridal party on Saturday at 10am: 1 bridal updo and 4 blow-dries. Any group discount?")
    ranked = {f["offer_id"]: f for f in r.figures}
    assert ranked["GROUP_15"]["checks"]["session_rule"] is False
    assert "hair mask" in r.draft
