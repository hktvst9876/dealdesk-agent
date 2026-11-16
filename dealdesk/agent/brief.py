"""Step 6 input: the reply brief. Everything the writer may say, and nothing else.

The brief is built from the engine's result and the ledger, never from the model.
Its `allowed_numbers` set is what the checker compares the draft against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..engine.types import Figures, TurnDecision
from ..money import D, fmt_money, pct, whole
from ..profile.schema import BusinessProfile

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass
class OfferLine:
    label: str  # where and when, customer-facing
    price_label: str  # "a S$600 minimum spend"
    price: Decimal
    per_head: Optional[Decimal] = None
    inclusions: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    perk_ids: list[str] = field(default_factory=list)
    description: Optional[str] = None  # what a package is and who it suits


@dataclass
class ReplyBrief:
    kind: str
    business: str
    symbol: str
    tone: str
    now: datetime
    name: Optional[str] = None
    occasion: Optional[str] = None
    party: Optional[int] = None
    main: Optional[OfferLine] = None
    alternatives: list[OfferLine] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    declined_perk_ids: list[str] = field(default_factory=list)
    reframe: Optional[str] = None
    hold_expiry: Optional[datetime] = None
    next_step: str = ""
    escalation_back_by: Optional[datetime] = None
    callback_name: Optional[str] = None
    callback_by: Optional[datetime] = None
    confirm_link: Optional[str] = None
    question: Optional[str] = None
    unavailable_note: Optional[str] = None
    price_list: list[str] = field(default_factory=list)
    online_cap: Optional[int] = None
    desk_opens: Optional[datetime] = None
    scarcity_ok: bool = False
    house_rules_before_commit: list[str] = field(default_factory=list)
    extra_numbers: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------ the checker's whitelist
    def allowed_numbers(self) -> set[str]:
        nums: set[str] = set(self.extra_numbers)

        def add_num(x) -> None:
            if x is None:
                return
            d = D(x)
            for v in {d, whole(d), d.quantize(Decimal("0.01"))}:
                s = format(v, "f")
                nums.add(s)
                nums.add(s.rstrip("0").rstrip(".") if "." in s else s)
                if "." not in s:
                    nums.add(f"{int(v):,}")

        def add_text(t: Optional[str]) -> None:
            for m in re.findall(r"\b(\d{1,2}):(\d{2})\b", t or ""):
                nums.add(f"{int(m[0]):02d}:{m[1]}")
            for m in NUM_RE.findall(t or ""):
                nums.add(m)
                nums.add(m.replace(",", ""))

        def add_dt(dt: Optional[datetime]) -> None:
            if dt:
                nums.update({dt.strftime("%H:%M"), str(dt.day), dt.strftime("%d"), str(dt.hour), dt.strftime("%I").lstrip("0")})

        add_num(self.party)
        add_num(self.online_cap)
        for line in [self.main, *self.alternatives]:
            if line is None:
                continue
            add_num(line.price)
            add_num(line.per_head)
            add_text(line.label)
            add_text(line.price_label)
            for t in line.inclusions + line.conditions:
                add_text(t)
        for t in self.facts + self.price_list + self.declined + self.house_rules_before_commit:
            add_text(t)
        add_text(self.reframe)
        add_text(self.unavailable_note)
        add_text(self.question)
        add_text(self.confirm_link)
        for dt in (self.hold_expiry, self.escalation_back_by, self.callback_by, self.desk_opens):
            add_dt(dt)
        return nums

    def allowed_perk_ids(self) -> set[str]:
        ids = set(self.declined_perk_ids)
        for line in [self.main, *self.alternatives]:
            if line:
                ids.update(line.perk_ids)
        return ids


def when(dt: datetime, now: datetime) -> str:
    if dt.date() == now.date():
        return f"{dt:%H:%M} today"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"{dt:%H:%M} tomorrow"
    return f"{dt:%H:%M} on {dt:%a} {dt.day} {dt:%b}"


def offer_line(profile: BusinessProfile, fig: Figures, party: Optional[int], byo_bottles: int = 0) -> OfferLine:
    sym = profile.business.currency_symbol
    cand = fig.candidate
    price = D(fig.price_quoted or 0)
    if cand.pricing == "minimum_spend":
        price_label = f"a {fmt_money(price, sym)} minimum spend" if price > 0 else "no minimum spend"
    elif cand.pricing == "basket":
        price_label = f"{fmt_money(fig.revenue, sym)} in total"
        price = fig.revenue
    else:
        price_label = fmt_money(price, sym)
    per_head = None
    if party and cand.pricing == "minimum_spend" and price > 0:
        per_head = whole(price / party)
    inclusions, perk_ids = [], []
    for cl in cand.concessions:
        perk = profile.perk(cl.perk_id)
        perk_ids.append(perk.id)
        if perk.type == "package" and perk.floor_basis is None:
            continue
        text = perk.customer_text or perk.label
        if perk.type == "fee_waiver" and cl.qty > 1:
            text = text.replace("one bottle", f"{cl.qty} bottles")
        if perk.type == "fee_waiver" and perk.fee_id in profile.fees and profile.fees[perk.fee_id].per == "bottle":
            rest = max(0, byo_bottles - cl.qty)
            if rest:
                fee = profile.fees[perk.fee_id]
                text += f" (the other {rest} {'is' if rest == 1 else 'are'} {fmt_money(fee.amount, sym)} each)"
        inclusions.append(text)
    description = None
    if cand.package_id:
        perk_ids.append(cand.package_id)
        pk = profile.perk(cand.package_id)
        if pk.customer_text and pk.floor_basis is None:
            description = pk.customer_text
    label = cand.placement.label if cand.placement else (", ".join(
        f"{l.qty} x {profile.product(l.product_id).label}" for l in cand.basket))
    conditions = list(dict.fromkeys(fig.conditions + [profile.perk(c.perk_id).condition for c in cand.concessions
                                                      if c.perk_id in cand.new_concessions and profile.perk(c.perk_id).condition]))
    return OfferLine(label, price_label, price, per_head, inclusions, conditions, perk_ids, description)


# --------------------------------------------------------------------------- staff-facing text

def staff_rationale(profile: BusinessProfile, td: TurnDecision, blocker: str) -> str:
    """Four lines: blocker, options compared (E for each), the rule applied, what would change it."""
    sym = profile.business.currency_symbol
    opts = " | ".join(f"{f.offer_id} {f.label[:28]} E {fmt_money(f.expected_contribution, sym)}"
                      + ("" if f.sendable or f.escalatable else " x") for f in td.ranked[:6])
    chosen = td.chosen
    change = "-"
    if chosen is not None and td.standard is not None:
        if chosen.kind == "standard":
            alts = [f for f in td.ranked if f.kind != "standard" and f.breakeven_p_book is not None]
            if alts:
                a = min(alts, key=lambda f: f.breakeven_p_book)
                change = f"{a.label} would win if it lifted P(book) above {pct(a.breakeven_p_book)} (assumed {pct(a.p_book)})"
        elif chosen.breakeven_p_book is not None:
            change = (f"if fewer than {pct(chosen.breakeven_p_book)} of these customers book on these terms, "
                      f"the standard quote is worth more (assumed {pct(chosen.p_book)})")
        if chosen.high_demand:
            change += "; high-demand slot: price discounts blocked"
    return "\n".join([
        f"1. Blocker: {blocker}",
        f"2. Options: {opts}",
        f"3. Rule: {td.rule}",
        f"4. Would change: {change}",
    ])


def escalation_packet(profile: BusinessProfile, td: TurnDecision, blocker: str, deadline: datetime) -> dict:
    sym = profile.business.currency_symbol
    f = td.chosen
    return {
        "blocker": blocker,
        "recommended": f.label,
        "recommendation": f"Approve {f.label}: E {fmt_money(f.expected_contribution, sym)} vs standard "
                          f"{fmt_money(td.standard.expected_contribution, sym) if td.standard else '-'}",
        "offers": [{"id": x.offer_id, "label": x.label, "contribution": str(whole(x.contribution)),
                    "opportunity_cost": str(whole(x.opportunity_cost)), "p_book": str(x.p_book),
                    "expected": str(whole(x.expected_contribution)), "decision": x.decision} for x in td.ranked[:6]],
        "concession_true_cost": str(whole(f.concession_cost)),
        "cumulative": str(whole(f.cumulative_concession)),
        "assumptions": f.assumptions,
        "blocked_by": f.rule,
        "authority_required": f.authority_required,
        "fallback": td.fallback.label if td.fallback else None,
        "fallback_offer_id": td.fallback.offer_id if td.fallback else None,
        "chosen_offer_id": f.offer_id,
        "deadline": deadline.isoformat(timespec="minutes"),
    }


def figures_table(profile: BusinessProfile, td: TurnDecision) -> str:
    sym = profile.business.currency_symbol
    rows = []
    for f in td.ranked[:6]:
        be = pct(f.breakeven_p_book) if f.breakeven_p_book is not None else "-"
        rows.append(f"{f.offer_id}: C {fmt_money(f.contribution, sym)} | O {fmt_money(f.opportunity_cost, sym)} | "
                    f"p {pct(f.p_book)} | E {fmt_money(f.expected_contribution, sym)} | cost {fmt_money(f.concession_cost, sym)} "
                    f"| b/e {be} | {f.decision}")
    return "\n".join(rows)
