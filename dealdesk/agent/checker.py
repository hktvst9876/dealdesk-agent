"""The outbound checker: compares every number and promise in a draft with the brief.

Runs on every draft before it reaches staff or a customer. A failing model draft
is rewritten once with the checker's feedback, then replaced by the template.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..profile.schema import BusinessProfile
from .brief import ReplyBrief

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b|\b(\d{1,2})(?:[.:](\d{2}))?\s?(am|pm)\b", re.I)
NUM_RE = re.compile(r"(?<![\w/])\d+(?:[.,]\d+)*(?![\w])")
URL_RE = re.compile(r"https?://\S+")
FORBIDDEN = [
    "margin", "contribution", "probabilit", "likelihood", "break-even", "breakeven", "opportunity cost", "authority",
    "concession cap", "threshold", "close rate", "expected value", "p(book", "resell", "resale", "our system says",
    "other customer", "another customer", "another group", "other group", "algorithm", "the engine", "cogs",
]
NOT_POSSIBLE = ["not possible", "impossible", "no way we can", "cannot be done", "can't be done"]
SCARCITY = ["last bed", "last table", "last one", "only one left", "last room", "selling fast", "almost full", "nearly full"]


@dataclass
class CheckResult:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        return "; ".join(self.problems)


def check(profile: BusinessProfile, brief: ReplyBrief, text: str) -> CheckResult:
    problems: list[str] = []
    low = text.lower()
    allowed = brief.allowed_numbers()
    body = URL_RE.sub(" ", text)
    if brief.confirm_link and brief.confirm_link not in text and brief.kind == "confirm_link":
        problems.append("the confirmation link is missing")
    # Times first, so "15:30" is checked as a time and not as two numbers.
    for m in TIME_RE.finditer(body):
        if m.group(1) is not None:
            t = f"{int(m.group(1)):02d}:{m.group(2)}"
        else:
            h = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
            t = f"{h:02d}:{m.group(4) or '00'}"
        if t not in allowed:
            problems.append(f"time {m.group(0)} is not in the engine result")
    body = TIME_RE.sub(" ", body)
    for m in NUM_RE.finditer(body):
        tok = m.group(0)
        plain = tok.replace(",", "")
        if tok in allowed or plain in allowed or plain.rstrip("0").rstrip(".") in allowed:
            continue
        problems.append(f"number {tok} does not come from the engine result")
    if "%" in text:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
            if m.group(1) not in allowed:
                problems.append(f"percentage {m.group(0)} is not part of the offer")
    for w in FORBIDDEN:
        if w in low:
            problems.append(f"mentions internal term '{w}'")
    for w in NOT_POSSIBLE:
        if w in low:
            problems.append(f"says '{w}': give the reason in customer terms and an alternative instead")
    if not brief.scarcity_ok:
        for w in SCARCITY:
            if w in low:
                problems.append(f"invents urgency ('{w}') that the availability check does not show")
    if brief.hold_expiry is None and re.search(r"\b(hold|held|holding|reserved?)\b[^.]*\buntil\b", low):
        problems.append("states a hold expiry but no hold exists")
    allowed_perks = brief.allowed_perk_ids()
    for perk in profile.perks:
        if perk.id in allowed_perks:
            continue
        for kw in perk.mention_keywords():
            if kw and kw in low and not _kw_inside_allowed(kw, brief):
                problems.append(f"mentions '{kw}' ({perk.id}) which is not part of this offer")
                break
    if brief.kind in ("offer", "keep", "alternative") and brief.main is not None:
        if not any(n in text.replace(",", "") for n in {str(int(brief.main.price))} if brief.main.price):
            problems.append("the price of the offer is missing")
    return CheckResult(not problems, problems)


def _kw_inside_allowed(kw: str, brief: ReplyBrief) -> bool:
    """A keyword is fine if it only appears because an allowed text contains it (e.g. a house rule)."""
    texts = list(brief.facts) + list(brief.house_rules_before_commit) + [brief.reframe or "", brief.unavailable_note or ""]
    for line in [brief.main, *brief.alternatives]:
        if line:
            texts += [line.label, line.price_label, *line.inclusions, *line.conditions]
    return any(kw in t.lower() for t in texts)
