"""Claude for steps 1 (understand, frame the blocker) and 6 (wording).

The model never computes or chooses a number. Step 1 returns enquiry fields and
catalogue ids; step 6 turns the engine's brief into words, and the checker
compares every number in the draft with the brief before anyone sees it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from ..money import fmt_money
from ..profile.schema import BusinessProfile
from .brief import ReplyBrief, when
from .models import ReplyText, Understanding

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"


class BrainUnavailable(RuntimeError):
    pass


def catalogue_text(profile: BusinessProfile) -> str:
    b = profile.business
    lines = [f"Business: {b.name} ({b.kind}), currency {b.currency}, timezone {b.timezone}."]
    if b.online_cap_guests:
        lines.append(f"Online booking takes groups up to {b.online_cap_guests}; larger groups come to this chat.")
    if profile.sessions:
        lines.append("Sessions (id: days start-end):")
        lines += [f"  {s.id}: {','.join(s.days)} {s.start}-{s.end}" for s in profile.sessions]
    units = [r for r in profile.inventory if r.kind == "unit"]
    if units:
        lines.append("Seating / inventory (id: label, guests per unit, aliases):")
        lines += [f"  {r.id}: {r.label}, {r.guests_per_unit or '-'} guests, aliases {r.aliases}" for r in units]
    if profile.products:
        lines.append("Products (id: label):")
        lines += [f"  {p.id}: {p.label}" for p in profile.products]
    lines.append("Perk catalogue (id: label; words customers use):")
    lines += [f"  {p.id}: {p.label}; {p.mention_keywords()}" for p in profile.perks]
    return "\n".join(lines)


UNDERSTAND_SYSTEM = """You read customer messages for a business that sells bookings, food, drinks, products or services, \
and you extract the enquiry. You do not reply to the customer and you never quote or invent prices.

Rules:
- Resolve relative dates ("this Sunday", "tomorrow") against the current local date and time you are given.
- Only use ids that appear in the catalogue below. If the customer asks for something not in the catalogue, leave it out.
- `requested` holds catalogue perks the customer explicitly asked for (e.g. "can you waive corkage" -> the corkage waiver; \
"all 4 bottles" -> qty 4). Do not add perks they did not ask for.
- `blocker` is what stops them booking now: price (floor or total), fee (corkage, cancellation, deposit), occasion \
(they want the celebration marked), fit (seating, comfort, layout, privacy), risk (weather, what-ifs), process \
(how to confirm, wants a call), timing (slot or duration), or none.
- `intent`: accept only when they clearly agree to go ahead; request_call when they ask to talk by phone.
- `clarifying_question`: ONE short question, only if the blocker is unclear AND the answer would change the offer \
(good: "Is it the corkage or the total that's the concern?"). Never ask about budget. Usually null.
- Fields you cannot find stay null. Earlier turns' fields are already known; only report what this message adds or changes.

{catalogue}"""

WRITE_SYSTEM = """You write one short chat reply for {business}, on behalf of its team, to a customer on {channel}.
Tone: {tone}

You are given a brief from the business's pricing engine. Write ONLY what the brief contains:
- Lead with the plan for their group, then the number, then exactly one next step.
- Use only prices, times, dates and quantities that appear in the brief, written the same way. Never add numbers.
- Never mention margins, costs, contribution, probabilities, policies, approval limits, thresholds, or other customers.
- Never say "not possible": when declining something, give the reason in the customer's terms and the alternative in the brief.
- Only say something is held, or state an expiry, if the brief has a hold expiry. Never invent urgency or scarcity.
- Never confirm a booking; never ask for card details in chat; use the link in the brief if there is one.
- Use the customer's own words for the occasion. Plain, warm, specific. No more than 90 words. No markdown, no emojis \
unless the customer used them.
- If you received feedback from the checker, fix exactly those problems."""


class ClaudeBrain:
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None, client=None):
        import anthropic

        self.model = model or os.environ.get("DEALDESK_MODEL", DEFAULT_MODEL)
        if client is not None:
            self.client = client
        elif api_key:
            self.client = anthropic.Anthropic(api_key=api_key)
        else:
            self.client = anthropic.Anthropic()
        self._anthropic = anthropic

    def _parse(self, system: str, messages: list[dict], output_format, effort: str):
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=messages,
                output_format=output_format,
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.APIError as e:  # network, rate limit, 5xx, bad request
            raise BrainUnavailable(str(e)) from e
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise BrainUnavailable(f"no usable output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def understand(self, profile: BusinessProfile, text: str, now: datetime, state: dict, transcript: list[dict]) -> Understanding:
        system = UNDERSTAND_SYSTEM.format(catalogue=catalogue_text(profile))
        history = "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-12:])
        known = {k: v for k, v in (state.get("enquiry") or {}).items() if v not in (None, [], "")}
        user = (f"Current local time: {now:%A %Y-%m-%d %H:%M}\n"
                f"Known so far: {json.dumps(known)}\n"
                f"Offers made so far: {json.dumps(state.get('offers_summary', []))}\n\n"
                f"Conversation:\n{history}\n\nLatest customer message:\n{text}")
        return self._parse(system, [{"role": "user", "content": user}], Understanding, "low")

    def write(self, profile: BusinessProfile, brief: ReplyBrief, channel: str, feedback: Optional[str] = None,
              transcript: Optional[list[dict]] = None) -> str:
        b = profile.business
        system = WRITE_SYSTEM.format(business=b.name, channel=channel, tone=b.tone)
        user = "Brief:\n" + json.dumps(brief_for_model(brief), indent=1)
        if transcript:
            user = "Recent conversation:\n" + "\n".join(f"{t['role']}: {t['text']}" for t in transcript[-6:]) + "\n\n" + user
        if feedback:
            user += f"\n\nThe checker rejected your previous draft: {feedback}. Rewrite it."
        out = self._parse(system, [{"role": "user", "content": user}], ReplyText, "medium")
        return out.text.strip()


def brief_for_model(b: ReplyBrief) -> dict:
    def line(l):
        if l is None:
            return None
        return {"what_and_when": l.label, "price": l.price_label,
                "per_head": f"{b.symbol}{l.per_head} a head, all toward food and drinks" if l.per_head else None,
                "includes": l.inclusions, "condition": l.conditions}

    d = {
        "reply_type": b.kind, "customer_first_name": b.name, "occasion": b.occasion, "party_size": b.party,
        "main_offer": line(b.main), "alternatives": [line(a) for a in b.alternatives],
        "facts_to_state": b.facts, "terms_to_state_before_they_commit": b.house_rules_before_commit,
        "requests_we_are_not_doing": b.declined, "reframe": b.reframe,
        "hold_expires": when(b.hold_expiry, b.now) if b.hold_expiry else None,
        "next_step": b.next_step, "back_to_customer_by": when(b.escalation_back_by, b.now) if b.escalation_back_by else None,
        "callback": {"staff_first_name": b.callback_name, "by": when(b.callback_by, b.now)} if b.callback_name else None,
        "confirmation_link": b.confirm_link, "question_to_ask": b.question, "availability_note": b.unavailable_note,
        "price_list": b.price_list, "online_booking_cap": b.online_cap,
        "team_calls_back_from": when(b.desk_opens, b.now) if b.desk_opens else None,
    }
    return {k: v for k, v in d.items() if v not in (None, [], "")}
