"""The template writer: deterministic customer wording built only from the brief.

Used when no model is configured, when the model is unavailable, and as the
safe fallback when a model draft fails the checker twice. Plan, number, one step.
"""
from __future__ import annotations

from .brief import OfferLine, ReplyBrief, when


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _hi(b: ReplyBrief) -> str:
    return f"Hi {b.name}!" if b.name else "Hi!"


def _group(b: ReplyBrief) -> str:
    bits = []
    if not b.party and b.main is not None and " x " in b.main.label:
        return "your order"
    if b.party:
        bits.append(f"your group of {b.party}")
    else:
        bits.append("your group")
    if b.occasion:
        bits[0] += f" ({b.occasion})"
    return bits[0]


def _per_head(b: ReplyBrief, line: OfferLine) -> str:
    if not line.per_head:
        return ""
    return f" That is about {b.symbol}{line.per_head} a head, all toward food and drinks."


def _incl(line: OfferLine) -> str:
    if not line.inclusions:
        return ""
    if len(line.inclusions) == 1:
        return line.inclusions[0]
    return ", ".join(line.inclusions[:-1]) + " and " + line.inclusions[-1]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _hold(b: ReplyBrief) -> str:
    return f" I've held it for you until {when(b.hold_expiry, b.now)}." if b.hold_expiry else ""


def write(b: ReplyBrief) -> str:
    k = b.kind
    parts: list[str] = []
    if k == "question":
        return f"{_hi(b)} Quick check so I get this right for {_group(b)}: {b.question}"
    if k == "need_details":
        return f"{_hi(b)} Happy to help. {b.question}"
    if k == "escalate_holding":
        return (f"Thanks{' ' + b.name if b.name else ''}! I'm checking one thing with my manager and will come back to you "
                f"by {when(b.escalation_back_by, b.now)}.")
    if k == "decline_close":
        tail = f" {b.next_step}" if b.next_step else ""
        return f"No problem{' ' + b.name if b.name else ''}, thanks for letting us know.{tail}"
    if k == "thinking":
        s = "Of course, take your time."
        if b.hold_expiry and b.main:
            s += f" Your {b.main.label.split(',')[0]} stays held until {when(b.hold_expiry, b.now)}."
        return s + (f" {b.next_step}" if b.next_step else "")
    if k == "unavailable":
        s = f"Sorry{' ' + b.name if b.name else ''}, {b.unavailable_note}"
        if b.main:
            s += f" The nearest equivalent is {b.main.label} at {b.main.price_label}."
            if b.main.inclusions:
                s += f" That still includes {_incl(b.main)}."
        s += _hold(b)
        return s + (f" {b.next_step}" if b.next_step else "")
    if k == "out_of_hours":
        parts.append(f"{_hi(b)} Thanks for your message.")
        if b.online_cap and b.party and b.party > b.online_cap:
            parts.append(f"Our online booking takes groups of up to {b.online_cap}, so for {_group(b)} we arrange it directly.")
        if b.price_list:
            parts.append("Here's what it costs for your group: " + "; ".join(b.price_list) + ".")
        if b.main:
            parts.append(f"I can offer {b.main.label} at {b.main.price_label}." + _per_head(b, b.main))
            if b.main.inclusions:
                parts.append(f"That includes {_incl(b.main)}.")
        parts.append(_hold(b).strip())
        if b.desk_opens:
            parts.append(f"Our reservations team will call you back from {when(b.desk_opens, b.now)}.")
        parts.append(b.next_step)
        return " ".join(p for p in parts if p)
    if k == "confirm_link":
        parts.append(f"Great{', ' + b.name if b.name else ''}! {cap(b.main.label)} at {b.main.price_label}" +
                     (f", including {_incl(b.main)}" if b.main.inclusions else "") + "." + _per_head(b, b.main))
        parts.extend(b.house_rules_before_commit)
        parts.append(_hold(b).strip())
        if b.confirm_link:
            parts.append(f"To confirm, please complete this link: {b.confirm_link} Your booking is confirmed once that is done.")
        else:
            parts.append("Our team will confirm it with you here shortly; it's confirmed once you hear back from us.")
        return " ".join(p for p in parts if p)
    if k == "callback":
        parts.append(f"Of course{', ' + b.name if b.name else ''}. {b.callback_name} will call you by {when(b.callback_by, b.now)}.")
        if b.main:
            parts.append(f"Meanwhile, here's where we are: {b.main.label} at {b.main.price_label}" +
                         (f", with {_incl(b.main)}" if b.main.inclusions else "") + ".")
        parts.append(_hold(b).strip())
        return " ".join(p for p in parts if p)

    # offer / keep / alternative / approved / fallback
    line = b.main
    parts.append(f"{_hi(b)} For {_group(b)}, we can do {line.label} at {line.price_label}." + _per_head(b, line))
    if b.declined:
        if line.inclusions:
            parts.append(f"I'm not able to add {_join(b.declined)} as well, but your booking already includes {_incl(line)}.")
        else:
            parts.append(f"I'm not able to do {_join(b.declined)}, but here's how it works.")
        if b.reframe:
            parts.append(b.reframe)
        line_incl_said = True
    else:
        line_incl_said = False
    if line.inclusions and not line_incl_said:
        cond = f" if you {line.conditions[0]}" if line.conditions else ""
        parts.append(f"We'll include {_incl(line)}{cond}.")
    for f in b.facts:
        parts.append(f)
    for alt in b.alternatives:
        extra = f", with {_incl(alt)}" if alt.inclusions else ""
        desc = f" It's {alt.description}." if alt.description else ""
        parts.append(f"Another option: {alt.label} at {alt.price_label}{extra}.{desc}")
    parts.append(_hold(b).strip())
    parts.append(b.next_step)
    return " ".join(p for p in parts if p)
