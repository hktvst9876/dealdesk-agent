"""Background loops: holds, escalation deadlines, callbacks, reply-time alerts, retention.

Each tick is idempotent: running it twice in the same minute changes nothing twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..policy.rules import reply_sla_minutes
from .models import Outbound
from .turn import Agent


def tick(agent: Agent, now: datetime, retention_days: int = 30) -> list[Outbound]:
    out: list[Outbound] = []
    store, b = agent.store, agent.profile.business
    # 1. Holds release automatically at expiry; never extended silently.
    for h in store.expire_holds(now):
        store.log(b.id, h["conversation_id"], "hold_expired", {"hold": h["id"], "unit": h["unit_id"]}, now)
    # 2. Escalations past their deadline fall back to the best approved offer rather than silence.
    for esc in store.escalations("open"):
        if datetime.fromisoformat(esc["deadline"]) <= now:
            out += agent.decide_escalation(esc["id"], False, "deadline", "front_line", now, timed_out=True)
    # 3. Callbacks due: remind the named person once, at the deadline.
    for cb in store.callbacks("open"):
        if datetime.fromisoformat(cb["deadline"]) <= now and store.claim(f"cb-reminder:{cb['id']}", now):
            out.append(Outbound("staff", None, f"Callback #{cb['id']} is due now: {cb['staff_name']}, please call the customer "
                                               f"(conversation {cb['conversation_id']}). Reply /done {cb['id']} when called."))
    # 4. Reply-time alerts: a draft waiting longer than the SLA gets one nudge.
    for r in store.reviews("pending"):
        inbound = datetime.fromisoformat(r["inbound_at"])
        sla = reply_sla_minutes(agent.profile, inbound)
        if now >= inbound + timedelta(minutes=sla) and store.claim(f"sla-nudge:{r['id']}", now):
            waited = int((now - inbound).total_seconds() // 60)
            out.append(Outbound("staff", None, f"Review #{r['id']} is overdue: the customer has waited {waited} min "
                                               f"(target {sla} min). Send, edit or reject it."))
    # 5. Retention: message text is kept only as long as the business allows.
    if store.claim(f"purge:{now:%Y-%m-%d}", now):
        n = store.purge_transcripts(now - timedelta(days=retention_days))
        if n:
            store.log(b.id, None, "purged", {"transcripts": n}, now)
    return out
