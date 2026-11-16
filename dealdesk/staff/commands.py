"""Staff commands in the private staff chat (or a private chat with a registered staff member)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..agent.rules_brain import _date
from ..agent.turn import Agent
from ..analytics.metrics import gate_status, week_metrics
from ..finance.calibration import propose_close_rates
from ..profile.schema import TIER_ORDER

HELP = """Staff commands
/deal <situation>  e.g. /deal table of 6 tonight 7:30pm, want a deal on drinks
/busy <date> [session]   mark a date or session as high demand (no price discounts, short holds)
/quiet <date> [session]  mark it as quiet (low forecast utilisation)
/normal <date>           clear busy/quiet for that date
/stock <item> <n>        perks available today, e.g. /stock cakes 3
/onhand <product> <n>    live stock count for a product
/book <resource> <n> <YYYY-MM-DD> <HH:MM>-<HH:MM>   record a booking made by phone or walk-in
/confirm <conversation>  card on file / paid: turn the hold into a booking and tell the customer
/done <callback id>      mark a callback as made
/outcome <conversation> booked|lost|resold [spend] [guests]
/holds  /queue  /pnl  /metrics  /gates
/autosend <class> on|off (owner)    /calibration    /approve <key> <value> (owner)
/iam <name>              link your Telegram account to your staff profile"""


def handle(agent: Agent, text: str, staff_name: str, staff_tier: str, now: datetime, user_id: Optional[int] = None) -> list:
    """Returns a list of Outbound or str (str = reply to the staff chat)."""
    store, prof, b = agent.store, agent.profile, agent.profile.business
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    rest = text.strip()[len(parts[0]):].strip()

    def day_key(a: list[str]) -> Optional[tuple[str, Optional[str]]]:
        if not a:
            return None
        d = _date(a[0], now) if not a[0][:4].isdigit() else datetime.fromisoformat(a[0]).date()
        if d is None:
            return None
        return d.isoformat(), (a[1] if len(a) > 1 else None)

    if cmd in ("/help", "/start"):
        return [HELP]
    if cmd == "/iam":
        if not args:
            return ["Usage: /iam <your name as in the profile>"]
        match = next((s for s in prof.staff if s.name.lower() == args[0].lower()), None)
        if match is None:
            return [f"No staff member called {args[0]} in the profile. Staff: {', '.join(s.name for s in prof.staff)}"]
        store.set_setting(b.id, f"staff:{user_id}", match.name)
        return [f"Linked. You are {match.name} ({match.tier})."]
    if cmd == "/deal":
        return [agent.quick_deal(rest, now, staff_tier)] if rest else ["Usage: /deal <situation>"]
    if cmd in ("/busy", "/quiet", "/normal"):
        dk = day_key(args)
        if dk is None:
            return [f"Usage: {cmd} <today|tomorrow|YYYY-MM-DD|weekday> [session id]"]
        day, session = dk
        key = f"{day}:{session}" if session else day
        if cmd == "/busy":
            store.set_signal(b.id, "high_demand", key, "1", now)
            return [f"{key} is now HIGH DEMAND: price discounts blocked, holds shortened to "
                    f"{prof.policy.high_demand.hold_minutes} min."]
        if cmd == "/quiet":
            store.set_signal(b.id, "high_demand", key, "0", now)
            store.set_signal(b.id, "utilisation", key, "0.3", now)
            return [f"{key} marked quiet (forecast utilisation 30%)."]
        store.set_signal(b.id, "high_demand", key, "0", now)
        store.x("DELETE FROM signals WHERE business_id=? AND kind='utilisation' AND key=?", (b.id, key))
        return [f"{key} back to normal forecasts."]
    if cmd == "/stock" and len(args) == 2:
        store.set_signal(b.id, "stock", args[0], int(args[1]), now, expires_at=(now + timedelta(days=1)).replace(hour=4, minute=0))
        return [f"{args[0]} available today: {args[1]}"]
    if cmd == "/onhand" and len(args) == 2:
        prof.product(args[0])
        store.set_signal(b.id, "product_on_hand", args[0], int(args[1]), now)
        return [f"{args[0]} on hand: {args[1]}"]
    if cmd == "/book" and len(args) == 4:
        res = prof.resource(args[0])
        start_s, end_s = args[3].split("-")
        start = datetime.fromisoformat(f"{args[2]}T{start_s}")
        end = datetime.fromisoformat(f"{args[2]}T{end_s}")
        if end <= start:
            end += timedelta(days=1)
        cal = store.calendar_view(b.id, now)
        from ..engine.calendar_math import pick_units
        units, why = pick_units(cal, res, start, end, int(args[1]))
        if units is None:
            return [f"Cannot book: {why}"]
        ids = [store.add_booking(b.id, res.id, u, start, end, source="staff", now=now) for u in units]
        return [f"Booked {', '.join(units)} ({', '.join(ids)})"]
    if cmd == "/confirm" and args:
        cid = args[0] if ":" in args[0] else _conv_for_hold(store, args[0])
        return agent.confirm(cid, staff_name, now) if cid else [f"No conversation or hold {args[0]}"]
    if cmd == "/done" and args:
        store.close_callback(int(args[0]))
        store.log(b.id, None, "callback_done", {"id": int(args[0]), "by": staff_name}, now)
        return [f"Callback #{args[0]} done."]
    if cmd == "/outcome" and len(args) >= 2:
        cid, what = args[0], args[1].lower()
        spend = _dec(args[2]) if len(args) > 2 else None
        guests = int(args[3]) if len(args) > 3 else None
        last = store.last_sent_offer(cid)
        p_class = (last or {}).get("offer", {}).get("p_class")
        store.record_outcome(cid, b.id, what == "booked", now, actual_spend=spend, guests=guests, resold=(what == "resold") or None,
                             p_class=p_class)
        return [f"Outcome for {cid}: {what}."]
    if cmd == "/holds":
        hs = store.active_holds(now=now)
        return ["\n".join(f"{h['id']} {h['unit_id']} {h['start'][:16]} until {h['expires_at'][11:16]} ({h['conversation_id']})"
                          for h in hs) or "No active holds."]
    if cmd == "/queue":
        rs = store.reviews("pending")
        return ["\n".join(f"#{r['id']} {r['decision_class']} for {r['conversation_id']} (in {r['inbound_at'][11:16]})"
                          for r in rs) or "Review queue is empty."]
    if cmd == "/pnl":
        if agent.posture is None:
            return ["No P&L snapshot in the profile. Add finance.pnl to switch on profit-and-loss adaptation."]
        return ["\n".join(agent.posture.summary(b.currency_symbol))]
    if cmd == "/metrics":
        week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return ["\n".join(week_metrics(store, prof, week).lines(b.currency_symbol) +
                          ["", "Last week:"] + week_metrics(store, prof, week - timedelta(days=7)).lines(b.currency_symbol))]
    if cmd == "/gates":
        return ["\n".join(f"{cls}: {'MET' if ok else 'not met'} - {why}" for cls, (ok, why) in gate_status(store, prof, now).items())]
    if cmd == "/autosend" and len(args) == 2:
        if TIER_ORDER.get(staff_tier, 0) < TIER_ORDER["owner"]:
            return ["Only the owner can switch auto-send."]
        cls, on = args[0], args[1].lower() == "on"
        if cls == "escalate":
            return ["Escalations stay human-approved."]
        ok, why = gate_status(store, prof, now).get(cls, (False, "unknown class"))
        store.set_setting(b.id, f"autosend.{cls}", "1" if on else "0")
        store.log(b.id, None, "autosend_changed", {"class": cls, "on": on, "by": staff_name, "gate_met": ok}, now)
        warn = "" if ok or not on else f" WARNING: gate not met ({why})."
        return [f"Auto-send for {cls}: {'ON' if on else 'OFF'}.{warn}"]
    if cmd == "/calibration":
        outs = [(o["p_class"], bool(o["booked"])) for o in store.outcomes(b.id) if o["p_class"]]
        props = propose_close_rates(dict(prof.policy.p_book_defaults) | {k.split('.', 1)[1]: v for k, v in store.calibrated(b.id).items()
                                                                          if k.startswith("p_book.")}, outs)
        if not props:
            return [f"No calibration proposals yet ({len(outs)} outcomes logged; 30 per class needed)."]
        return ["\n".join(f"{p.key}: {p.current} -> {p.proposed} ({p.note}). /approve {p.key} {p.proposed}" for p in props)]
    if cmd == "/approve" and len(args) == 2:
        if TIER_ORDER.get(staff_tier, 0) < TIER_ORDER["owner"]:
            return ["Only the owner can approve calibration changes."]
        v = _dec(args[1])
        if v is None or not (0 <= v <= 1):
            return ["Value must be between 0 and 1."]
        store.approve_calibration(b.id, args[0], v, staff_name, now)
        return [f"Approved {args[0]} = {v}. It applies from the next enquiry."]
    return [f"Unknown or incomplete command. {HELP.splitlines()[0]}: /help"]


def _dec(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s)
    except (InvalidOperation, TypeError):
        return None


def _conv_for_hold(store, hold_id: str) -> Optional[str]:
    rows = store.q("SELECT conversation_id FROM holds WHERE id=?", (hold_id,))
    return rows[0]["conversation_id"] if rows else None
