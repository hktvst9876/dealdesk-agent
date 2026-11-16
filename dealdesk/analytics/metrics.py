"""Weekly metrics and the auto-send rollout gates.

Headline: contribution per eligible enquiry (booked and lost, resale included).
It penalises refusing a free gesture (lost booking), buying a booking that would
have happened anyway (lost margin), and filling a peak slot with a group that
pays less than the regulars it pushed out.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..ledger.store import Store
from ..money import D, ZERO
from ..policy.rules import in_hours
from ..profile.schema import BusinessProfile

GATES = {
    "keep_quote": {"acceptance": Decimal("0.85")},
    "offer_approved_concession": {"acceptance": Decimal("0.80"), "min_outcomes": 30},
    "suggest_alternative": {"acceptance": Decimal("0.80")},
}


@dataclass
class WeekMetrics:
    start: datetime
    enquiries: int = 0
    booked: int = 0
    contribution_total: Decimal = ZERO
    concession_total: Decimal = ZERO
    zero_concession_bookings: int = 0
    first_reply_minutes_in: list[float] = field(default_factory=list)
    first_reply_minutes_out: list[float] = field(default_factory=list)
    drafts: dict[str, dict[str, int]] = field(default_factory=dict)  # class -> {sent, edited, rejected, auto}
    factual_errors: dict[str, int] = field(default_factory=dict)
    escalations: int = 0
    escalations_in_time: int = 0
    spend_forecast_vs_actual: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    @property
    def booking_rate(self) -> Optional[Decimal]:
        return D(self.booked) / D(self.enquiries) if self.enquiries else None

    @property
    def contribution_per_enquiry(self) -> Optional[Decimal]:
        return self.contribution_total / D(self.enquiries) if self.enquiries else None

    @property
    def concession_per_booking(self) -> Optional[Decimal]:
        return self.concession_total / D(self.booked) if self.booked else None

    def acceptance(self, cls: str) -> Optional[Decimal]:
        d = self.drafts.get(cls, {})
        decided = d.get("sent", 0) + d.get("edited", 0) + d.get("rejected", 0)
        return D(d.get("sent", 0)) / D(decided) if decided else None

    @staticmethod
    def _pct(xs: list[float], q: float) -> Optional[float]:
        if not xs:
            return None
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]

    def lines(self, symbol: str) -> list[str]:
        f = lambda x, fmt: "-" if x is None else fmt(x)
        out = [
            f"Week of {self.start:%d %b}: {self.enquiries} eligible enquiries, {self.booked} booked "
            f"({f(self.booking_rate, lambda v: f'{v * 100:.0f}%')})",
            f"Contribution per eligible enquiry: {f(self.contribution_per_enquiry, lambda v: f'{symbol}{v:.0f}')}",
            f"First reply (median / p90): in hours {f(self._pct(self.first_reply_minutes_in, .5), lambda v: f'{v:.0f}')} / "
            f"{f(self._pct(self.first_reply_minutes_in, .9), lambda v: f'{v:.0f}')} min; out of hours "
            f"{f(self._pct(self.first_reply_minutes_out, .5), lambda v: f'{v:.0f}')} / "
            f"{f(self._pct(self.first_reply_minutes_out, .9), lambda v: f'{v:.0f}')} min",
            f"Concession cost per booking: {f(self.concession_per_booking, lambda v: f'{symbol}{v:.0f}')}; "
            f"bookings with no concession: {self.zero_concession_bookings}/{self.booked}",
        ]
        for cls in GATES:
            a = self.acceptance(cls)
            d = self.drafts.get(cls, {})
            out.append(f"Drafts '{cls}': accepted as-is {f(a, lambda v: f'{v * 100:.0f}%')} "
                       f"(sent {d.get('sent', 0)}, edited {d.get('edited', 0)}, rejected {d.get('rejected', 0)})")
        out.append(f"Escalations answered before deadline: {self.escalations_in_time}/{self.escalations}")
        if self.spend_forecast_vs_actual:
            fc = sum(x for x, _ in self.spend_forecast_vs_actual) / len(self.spend_forecast_vs_actual)
            ac = sum(y for _, y in self.spend_forecast_vs_actual) / len(self.spend_forecast_vs_actual)
            out.append(f"Spend per guest: forecast {symbol}{fc:.0f} vs actual {symbol}{ac:.0f}")
        return out


def week_metrics(store: Store, profile: BusinessProfile, start: datetime) -> WeekMetrics:
    end = start + timedelta(days=7)
    m = WeekMetrics(start)
    bid = profile.business.id
    convs = [dict(r) for r in store.q("SELECT * FROM conversations WHERE business_id=? AND created_at>=? AND created_at<?",
                                      (bid, start.isoformat(), end.isoformat()))]
    import json
    outcomes = {o["conversation_id"]: o for o in store.outcomes(bid)}
    for c in convs:
        state = json.loads(c["state"])
        offers = store.offers(c["id"])
        sent = [o for o in offers if o["status"] in ("sent", "accepted")]
        if not sent:
            continue
        m.enquiries += 1
        first_in, first_out = state.get("first_inbound_at"), state.get("first_reply_at")
        if first_in and first_out:
            t_in = datetime.fromisoformat(first_in)
            mins = (datetime.fromisoformat(first_out) - t_in).total_seconds() / 60
            (m.first_reply_minutes_in if in_hours(profile, t_in) else m.first_reply_minutes_out).append(mins)
        last = sent[-1]["offer"]
        fig = last.get("figures", {})
        o = outcomes.get(c["id"])
        if o and o["booked"]:
            m.booked += 1
            m.contribution_total += D(o["contribution"]) if o["contribution"] else D(fig.get("contribution", 0))
            cost = D(fig.get("cumulative_concession", 0))
            m.concession_total += cost
            if cost == 0:
                m.zero_concession_bookings += 1
            if o["actual_spend"] and o["guests"]:
                spend = profile.spend_case(None)
                if spend:
                    m.spend_forecast_vs_actual.append((D(spend.base), D(o["actual_spend"]) / D(o["guests"])))
        elif o and o["resold"]:
            m.contribution_total += D(fig.get("opportunity_cost", 0))  # the capacity earned its next-best use
    for e in store.events(bid, "review_decided", since=start):
        if datetime.fromisoformat(e["created_at"]) >= end:
            continue
        p = e["payload"]
        cls = p.get("class", "keep_quote")
        cls = {"confirm": "keep_quote", "unavailable": "keep_quote", "fallback": "keep_quote"}.get(cls, cls)
        d = m.drafts.setdefault(cls, {})
        d[p["action"]] = d.get(p["action"], 0) + 1
        if p["action"] == "rejected" and "fact" in (p.get("reason") or "").lower():
            m.factual_errors[cls] = m.factual_errors.get(cls, 0) + 1
    for r in store.q("SELECT * FROM escalations WHERE created_at>=? AND created_at<?", (start.isoformat(), end.isoformat())):
        m.escalations += 1
        if r["decided_at"] and r["status"] in ("approved", "declined") and r["decided_at"] <= r["deadline"]:
            m.escalations_in_time += 1
    return m


def gate_status(store: Store, profile: BusinessProfile, now: datetime) -> dict[str, tuple[bool, str]]:
    """Auto-send is enabled per decision class, in order, each only when its gate is met two weeks running."""
    week = now - timedelta(days=now.weekday())
    week = week.replace(hour=0, minute=0, second=0, microsecond=0)
    w1, w2 = week_metrics(store, profile, week - timedelta(days=7)), week_metrics(store, profile, week - timedelta(days=14))
    status: dict[str, tuple[bool, str]] = {}
    previous_ok = True
    for cls, gate in GATES.items():
        a1, a2 = w1.acceptance(cls), w2.acceptance(cls)
        ok = a1 is not None and a2 is not None and a1 > gate["acceptance"] and a2 > gate["acceptance"]
        why = f"acceptance {fmt_p(a2)} then {fmt_p(a1)} (needs > {gate['acceptance'] * 100:.0f}% both weeks)"
        if cls == "keep_quote" and (w1.factual_errors.get(cls) or w2.factual_errors.get(cls)):
            ok, why = False, why + "; factual errors logged"
        if cls == "offer_approved_concession":
            n = len([o for o in store.outcomes(profile.business.id) if o["p_class"]])
            if n < gate["min_outcomes"]:
                ok, why = False, why + f"; only {n} outcomes with a close-rate class (needs {gate['min_outcomes']})"
            over = [b for b in (w1, w2) if b.concession_per_booking is not None and b.concession_per_booking > profile.policy.concession_cap]
            if over:
                ok, why = False, why + "; concession cost above cap"
        if not previous_ok:
            ok, why = False, why + "; previous gate not yet met"
        status[cls] = (ok, why)
        previous_ok = ok
    status["escalate"] = (False, "escalations stay human-approved")
    return status


def fmt_p(x: Optional[Decimal]) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"
