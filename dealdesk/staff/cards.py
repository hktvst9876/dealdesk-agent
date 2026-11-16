"""Staff-facing cards for the private Telegram staff chat (plain text + inline buttons)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..money import fmt_money

DECISION_LABEL = {
    "keep_quote": "Keep the quote",
    "offer_approved_concession": "Offer an approved concession",
    "suggest_alternative": "Suggest an alternative",
    "escalate": "Escalate for approval",
    "confirm": "Confirmation step",
    "question": "Clarifying question",
    "callback": "Callback assigned",
    "out_of_hours": "Out-of-hours reply",
    "unavailable": "Availability changed",
    "close": "Close politely",
    "thinking": "Customer thinking it over",
    "fallback": "Escalation timed out: best approved offer",
    "need_details": "Ask for details",
}


def review_card(rid: int, business: str, customer: str, summary: str, decision: str, draft: str, rationale: str,
                figures: str, hold_note: str, sla_note: str, urgent: bool = False) -> tuple[str, list[tuple[str, str]]]:
    head = f"{'URGENT ' if urgent else ''}Review #{rid} | {business} | {customer}"
    text = "\n".join(x for x in [
        head,
        summary,
        f"Decision: {DECISION_LABEL.get(decision, decision)}",
        "",
        "Draft to customer:",
        f"\"{draft}\"",
        "",
        "Why (staff only):",
        rationale,
        figures and "Figures:\n" + figures,
        hold_note,
        sla_note,
        "Reply to this message with new text to edit and send it.",
    ] if x is not None and x != "")
    buttons = [("Send", f"rv:send:{rid}"), ("Edit", f"rv:edit:{rid}"), ("Reject", f"rv:reject:{rid}")]
    return text, buttons


def escalation_card(eid: int, business: str, customer: str, packet: dict, symbol: str,
                    deadline: datetime) -> tuple[str, list[tuple[str, str]]]:
    offers = "\n".join(
        f"  {o['id']} {o['label'][:30]}: C {symbol}{o['contribution']} | O {symbol}{o['opportunity_cost']} | "
        f"p {o['p_book']} | E {symbol}{o['expected']} | {o['decision']}" for o in packet.get("offers", []))
    text = "\n".join([
        f"Escalation #{eid} | {business} | {customer}",
        f"Decide by {deadline:%H:%M} ({packet.get('authority_required', 'manager')} approval)",
        f"Customer's blocker: {packet.get('blocker')}",
        f"Recommendation: {packet.get('recommendation')}",
        f"Concession true cost {symbol}{packet.get('concession_true_cost')}, cumulative {symbol}{packet.get('cumulative')}",
        f"Blocked by: {packet.get('blocked_by')}",
        f"Assumptions: {packet.get('assumptions')}",
        "Offers compared:",
        offers,
        f"If nobody decides by {deadline:%H:%M}, the customer gets: {packet.get('fallback') or 'the standard quote'}",
    ])
    return text, [("Approve", f"esc:approve:{eid}"), ("Decline", f"esc:decline:{eid}")]


def callback_card(cb_id: int, business: str, customer: str, staff_name: str, deadline: datetime, chat_ref: str,
                  summary: str) -> str:
    return (f"Callback #{cb_id} | {business}\n{staff_name}: please call {customer} by {deadline:%H:%M}.\n{summary}\n"
            f"(Telegram chat {chat_ref}; the customer's number is in your Telegram contact, not stored here.)")
