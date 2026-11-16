"""The conversation record and built-in calendar (SQLite).

* Append-only offer ledger keyed by conversation: the model never reconstructs
  what was offered from chat text; every turn re-reads it from here.
* Idempotency keys on every side effect: duplicate inbound messages, retries and
  double-taps in the staff chat cannot create a second hold, booking, perk or entry.
* Built-in calendar: bookings and holds per unit, plus live staff signals
  (busy / quiet nights, cakes available today, stock counts).
* Privacy: the ledger keeps what the engine needs. Transcript text is kept only
  for the model's context and is purged on the business's retention schedule.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from ..engine.types import Booking, CalendarView

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, business_id TEXT, channel TEXT, chat_ref TEXT, customer_first_name TEXT,
  state TEXT NOT NULL DEFAULT '{}', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS processed (idem_key TEXT PRIMARY KEY, result TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, turn INTEGER, offer_id TEXT, decision TEXT,
  label TEXT, offer TEXT, conditions TEXT, expires_at TEXT, status TEXT, idem_key TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS concessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, perk_id TEXT, qty INTEGER, true_cost TEXT,
  turn INTEGER, status TEXT, idem_key TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS holds (
  id TEXT PRIMARY KEY, conversation_id TEXT, business_id TEXT, resource_id TEXT, unit_id TEXT, start TEXT, end TEXT,
  expires_at TEXT, status TEXT, idem_key TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY, conversation_id TEXT, business_id TEXT, resource_id TEXT, unit_id TEXT, start TEXT, end TEXT,
  status TEXT, source TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS callbacks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, staff_name TEXT, deadline TEXT, status TEXT,
  idem_key TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, packet TEXT, deadline TEXT, status TEXT,
  decided_by TEXT, decided_at TEXT, idem_key TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, turn INTEGER, decision_class TEXT, draft TEXT,
  final_text TEXT, figures TEXT, rationale TEXT, status TEXT, staff TEXT, reason TEXT, staff_message_ref TEXT,
  hold_ids TEXT, idem_key TEXT UNIQUE, created_at TEXT, decided_at TEXT, inbound_at TEXT);
CREATE TABLE IF NOT EXISTS transcript (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, role TEXT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT, kind TEXT, key TEXT, value TEXT, created_at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT, conversation_id TEXT, kind TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS calibration (
  id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT, key TEXT, value TEXT, approved_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS outcomes (
  conversation_id TEXT PRIMARY KEY, business_id TEXT, booked INTEGER, actual_spend TEXT, guests INTEGER,
  no_show INTEGER, resold INTEGER, p_class TEXT, blocker TEXT, contribution TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS settings (business_id TEXT, key TEXT, value TEXT, PRIMARY KEY (business_id, key));
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


class _Enc(json.JSONEncoder):
    def default(self, o: Any):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat(timespec="seconds")
        if isinstance(o, set):
            return sorted(o)
        return super().default(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, cls=_Enc, sort_keys=True)


@dataclass
class Conversation:
    id: str
    business_id: str
    channel: str
    chat_ref: str
    customer_first_name: Optional[str]
    state: dict


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid

    # ------------------------------------------------------------------ idempotency
    def seen(self, key: str) -> Optional[dict]:
        rows = self.q("SELECT result FROM processed WHERE idem_key=?", (key,))
        return json.loads(rows[0]["result"]) if rows else None

    def claim(self, key: str, now: datetime) -> bool:
        """Atomically claim an idempotency key. False if someone already did this."""
        try:
            self.x("INSERT INTO processed (idem_key, result, created_at) VALUES (?,?,?)", (key, "{}", _iso(now)))
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(self, key: str, result: dict) -> None:
        self.x("UPDATE processed SET result=? WHERE idem_key=?", (dumps(result), key))

    # ------------------------------------------------------------------ conversations
    def conversation(self, business_id: str, channel: str, chat_ref: str, first_name: Optional[str], now: datetime) -> Conversation:
        cid = f"{business_id}:{channel}:{chat_ref}"
        rows = self.q("SELECT * FROM conversations WHERE id=?", (cid,))
        if not rows:
            self.x("INSERT OR IGNORE INTO conversations VALUES (?,?,?,?,?,?,?,?)",
                   (cid, business_id, channel, chat_ref, first_name, "{}", _iso(now), _iso(now)))
            rows = self.q("SELECT * FROM conversations WHERE id=?", (cid,))
        r = rows[0]
        return Conversation(r["id"], r["business_id"], r["channel"], r["chat_ref"], r["customer_first_name"] or first_name,
                            json.loads(r["state"]))

    def get_conversation(self, cid: str) -> Optional[Conversation]:
        rows = self.q("SELECT * FROM conversations WHERE id=?", (cid,))
        if not rows:
            return None
        r = rows[0]
        return Conversation(r["id"], r["business_id"], r["channel"], r["chat_ref"], r["customer_first_name"], json.loads(r["state"]))

    def save_state(self, cid: str, state: dict, now: datetime) -> None:
        self.x("UPDATE conversations SET state=?, updated_at=? WHERE id=?", (dumps(state), _iso(now), cid))

    # ------------------------------------------------------------------ transcript (model context only)
    def add_transcript(self, cid: str, role: str, text: str, now: datetime) -> None:
        self.x("INSERT INTO transcript (conversation_id, role, text, created_at) VALUES (?,?,?,?)", (cid, role, text, _iso(now)))

    def transcript(self, cid: str, limit: int = 20) -> list[dict]:
        rows = self.q("SELECT role, text, created_at FROM transcript WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        return [dict(r) for r in reversed(rows)]

    def purge_transcripts(self, before: datetime) -> int:
        n = self.q("SELECT COUNT(*) c FROM transcript WHERE created_at < ?", (_iso(before),))[0]["c"]
        self.x("DELETE FROM transcript WHERE created_at < ?", (_iso(before),))
        return n

    # ------------------------------------------------------------------ offer ledger (append-only)
    def record_offer(self, cid: str, turn: int, offer_id: str, decision: str, label: str, offer: dict,
                     conditions: list[str], expires_at: Optional[datetime], status: str, idem_key: str, now: datetime) -> int:
        try:
            return self.x("INSERT INTO offers (conversation_id, turn, offer_id, decision, label, offer, conditions, expires_at, "
                          "status, idem_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, turn, offer_id, decision, label, dumps(offer), dumps(conditions), _iso(expires_at), status,
                           idem_key, _iso(now)))
        except sqlite3.IntegrityError:
            return self.q("SELECT id FROM offers WHERE idem_key=?", (idem_key,))[0]["id"]

    def offers(self, cid: str) -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM offers WHERE conversation_id=? ORDER BY id", (cid,)):
            d = dict(r)
            d["offer"] = json.loads(d["offer"])
            d["conditions"] = json.loads(d["conditions"])
            out.append(d)
        return out

    def mark_offer(self, offer_row_id: int, status: str) -> None:
        # Status changes are appended as events too; the original row keeps what was offered.
        self.x("UPDATE offers SET status=? WHERE id=?", (status, offer_row_id))

    def last_sent_offer(self, cid: str) -> Optional[dict]:
        rows = [o for o in self.offers(cid) if o["status"] in ("sent", "accepted")]
        return rows[-1] if rows else None

    # ------------------------------------------------------------------ concessions
    def add_concession(self, cid: str, perk_id: str, qty: int, true_cost: Decimal, turn: int, status: str,
                       idem_key: str, now: datetime) -> None:
        try:
            self.x("INSERT INTO concessions (conversation_id, perk_id, qty, true_cost, turn, status, idem_key, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (cid, perk_id, qty, str(true_cost), turn, status, idem_key, _iso(now)))
        except sqlite3.IntegrityError:
            pass

    def set_concession_status(self, cid: str, turn: int, status: str) -> None:
        self.x("UPDATE concessions SET status=? WHERE conversation_id=? AND turn=?", (status, cid, turn))

    def concessions(self, cid: str, statuses: tuple[str, ...] = ("offered", "accepted")) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return [dict(r) for r in self.q(f"SELECT * FROM concessions WHERE conversation_id=? AND status IN ({marks}) ORDER BY id",
                                        (cid, *statuses))]

    # ------------------------------------------------------------------ calendar: holds and bookings
    def place_hold(self, cid: str, business_id: str, resource_id: str, unit_id: str, start: datetime, end: datetime,
                   expires_at: datetime, idem_key: str, now: datetime) -> tuple[Optional[str], str]:
        """Place a hold only if nothing overlaps (fresh check inside the transaction)."""
        with self.tx() as c:
            prior = list(c.execute("SELECT id FROM holds WHERE idem_key=? AND unit_id=?", (idem_key, unit_id)))
            if prior:
                return prior[0]["id"], "already held"
            clash = list(c.execute(
                "SELECT 'booking' k, conversation_id FROM bookings WHERE business_id=? AND resource_id=? AND unit_id=? AND status='confirmed' "
                "AND start < ? AND ? < end UNION ALL "
                "SELECT 'hold' k, conversation_id FROM holds WHERE business_id=? AND resource_id=? AND unit_id=? AND status='active' "
                "AND expires_at > ? AND start < ? AND ? < end AND conversation_id != ?",
                (business_id, resource_id, unit_id, _iso(end), _iso(start),
                 business_id, resource_id, unit_id, _iso(now), _iso(end), _iso(start), cid)))
            if clash:
                return None, f"{unit_id} is already {'booked' if clash[0]['k'] == 'booking' else 'held'}"
            hid = f"H-{uuid.uuid4().hex[:8]}"
            c.execute("INSERT INTO holds VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (hid, cid, business_id, resource_id, unit_id, _iso(start), _iso(end), _iso(expires_at), "active",
                       idem_key, _iso(now)))
            return hid, ""

    def release_holds(self, cid: str, reason: str = "released", except_ids: tuple[str, ...] = ()) -> int:
        rows = self.q("SELECT id FROM holds WHERE conversation_id=? AND status='active'", (cid,))
        n = 0
        for r in rows:
            if r["id"] in except_ids:
                continue
            self.x("UPDATE holds SET status=? WHERE id=?", (reason, r["id"]))
            n += 1
        return n

    def release_hold_ids(self, ids: list[str], reason: str = "released") -> None:
        for hid in ids:
            self.x("UPDATE holds SET status=? WHERE id=? AND status='active'", (reason, hid))

    def active_holds(self, cid: Optional[str] = None, now: Optional[datetime] = None) -> list[dict]:
        sql = "SELECT * FROM holds WHERE status='active'"
        args: tuple = ()
        if cid:
            sql += " AND conversation_id=?"
            args += (cid,)
        if now:
            sql += " AND expires_at > ?"
            args += (_iso(now),)
        return [dict(r) for r in self.q(sql, args)]

    def expire_holds(self, now: datetime) -> list[dict]:
        rows = [dict(r) for r in self.q("SELECT * FROM holds WHERE status='active' AND expires_at <= ?", (_iso(now),))]
        for r in rows:
            self.x("UPDATE holds SET status='expired' WHERE id=?", (r["id"],))
        return rows

    def add_booking(self, business_id: str, resource_id: str, unit_id: str, start: datetime, end: datetime,
                    cid: str = "", source: str = "staff", now: Optional[datetime] = None, bid: Optional[str] = None) -> str:
        bid = bid or f"B-{uuid.uuid4().hex[:8]}"
        self.x("INSERT OR IGNORE INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?)",
               (bid, cid, business_id, resource_id, unit_id, _iso(start), _iso(end), "confirmed", source, _iso(now or datetime.now())))
        return bid

    def confirm_holds(self, cid: str, now: datetime) -> list[str]:
        """Turn this conversation's active holds into confirmed bookings (after the business confirms)."""
        ids = []
        for h in self.active_holds(cid):
            ids.append(self.add_booking(h["business_id"], h["resource_id"], h["unit_id"], _dt(h["start"]), _dt(h["end"]),
                                        cid, "agent", now, bid=f"B-{h['id'][2:]}"))
            self.x("UPDATE holds SET status='converted' WHERE id=?", (h["id"],))
        return ids

    def cancel_booking(self, bid: str) -> None:
        self.x("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))

    def calendar_view(self, business_id: str, now: datetime, exclude_conversation: Optional[str] = None) -> CalendarView:
        cal = CalendarView()
        for r in self.q("SELECT * FROM bookings WHERE business_id=? AND status='confirmed'", (business_id,)):
            cal.bookings.append(Booking(r["resource_id"], r["unit_id"], _dt(r["start"]), _dt(r["end"]), "confirmed", r["id"]))
        for r in self.q("SELECT * FROM holds WHERE business_id=? AND status='active' AND expires_at > ?", (business_id, _iso(now))):
            if exclude_conversation and r["conversation_id"] == exclude_conversation:
                continue
            cal.bookings.append(Booking(r["resource_id"], r["unit_id"], _dt(r["start"]), _dt(r["end"]), "held", r["id"]))
        for s in self.signals(business_id, now):
            v = s["value"]
            if s["kind"] == "utilisation":
                cal.utilisation[s["key"]] = Decimal(v)
            elif s["kind"] == "high_demand":
                if v == "1":
                    cal.high_demand.add(s["key"])
            elif s["kind"] == "chance":
                cal.chance[s["key"]] = Decimal(v)
            elif s["kind"] == "stock":
                cal.stock[s["key"]] = int(Decimal(v))
            elif s["kind"] == "product_on_hand":
                cal.product_on_hand[s["key"]] = int(Decimal(v))
            elif s["kind"] == "hours_booked":
                cal.hours_booked[s["key"]] = Decimal(v)
        return cal

    # ------------------------------------------------------------------ live signals from staff
    def set_signal(self, business_id: str, kind: str, key: str, value: Any, now: datetime,
                   expires_at: Optional[datetime] = None) -> None:
        self.x("DELETE FROM signals WHERE business_id=? AND kind=? AND key=?", (business_id, kind, key))
        self.x("INSERT INTO signals (business_id, kind, key, value, created_at, expires_at) VALUES (?,?,?,?,?,?)",
               (business_id, kind, key, str(value), _iso(now), _iso(expires_at)))

    def signals(self, business_id: str, now: datetime) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM signals WHERE business_id=? AND (expires_at IS NULL OR expires_at > ?) "
                                        "ORDER BY id", (business_id, _iso(now)))]

    # ------------------------------------------------------------------ callbacks and escalations
    def assign_callback(self, cid: str, staff_name: str, deadline: datetime, idem_key: str, now: datetime) -> int:
        try:
            return self.x("INSERT INTO callbacks (conversation_id, staff_name, deadline, status, idem_key, created_at) "
                          "VALUES (?,?,?,?,?,?)", (cid, staff_name, _iso(deadline), "open", idem_key, _iso(now)))
        except sqlite3.IntegrityError:
            return self.q("SELECT id FROM callbacks WHERE idem_key=?", (idem_key,))[0]["id"]

    def callbacks(self, status: str = "open") -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM callbacks WHERE status=? ORDER BY deadline", (status,))]

    def close_callback(self, cb_id: int) -> None:
        self.x("UPDATE callbacks SET status='done' WHERE id=?", (cb_id,))

    def escalate(self, cid: str, packet: dict, deadline: datetime, idem_key: str, now: datetime) -> int:
        try:
            return self.x("INSERT INTO escalations (conversation_id, packet, deadline, status, idem_key, created_at) "
                          "VALUES (?,?,?,?,?,?)", (cid, dumps(packet), _iso(deadline), "open", idem_key, _iso(now)))
        except sqlite3.IntegrityError:
            return self.q("SELECT id FROM escalations WHERE idem_key=?", (idem_key,))[0]["id"]

    def escalation(self, eid: int) -> Optional[dict]:
        rows = self.q("SELECT * FROM escalations WHERE id=?", (eid,))
        if not rows:
            return None
        d = dict(rows[0])
        d["packet"] = json.loads(d["packet"])
        return d

    def escalations(self, status: str = "open") -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM escalations WHERE status=? ORDER BY deadline", (status,)):
            d = dict(r)
            d["packet"] = json.loads(d["packet"])
            out.append(d)
        return out

    def decide_escalation(self, eid: int, status: str, by: str, now: datetime) -> bool:
        """First decision wins; a second tap on the same button changes nothing."""
        with self._lock:
            cur = self._conn.execute("UPDATE escalations SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='open'",
                                     (status, by, _iso(now), eid))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ staff review queue
    def add_review(self, cid: str, turn: int, decision_class: str, draft: str, figures: dict, rationale: str,
                   hold_ids: list[str], idem_key: str, now: datetime, inbound_at: datetime, status: str = "pending") -> int:
        try:
            return self.x("INSERT INTO reviews (conversation_id, turn, decision_class, draft, figures, rationale, status, "
                          "hold_ids, idem_key, created_at, inbound_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, turn, decision_class, draft, dumps(figures), rationale, status, dumps(hold_ids), idem_key,
                           _iso(now), _iso(inbound_at)))
        except sqlite3.IntegrityError:
            return self.q("SELECT id FROM reviews WHERE idem_key=?", (idem_key,))[0]["id"]

    def review(self, rid: int) -> Optional[dict]:
        rows = self.q("SELECT * FROM reviews WHERE id=?", (rid,))
        if not rows:
            return None
        d = dict(rows[0])
        d["figures"] = json.loads(d["figures"])
        d["hold_ids"] = json.loads(d["hold_ids"] or "[]")
        return d

    def reviews(self, status: Optional[str] = None) -> list[dict]:
        rows = self.q("SELECT id FROM reviews" + (" WHERE status=?" if status else "") + " ORDER BY id", (status,) if status else ())
        return [self.review(r["id"]) for r in rows]

    def decide_review(self, rid: int, status: str, staff: str, now: datetime, final_text: Optional[str] = None,
                      reason: Optional[str] = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reviews SET status=?, staff=?, decided_at=?, final_text=?, reason=? WHERE id=? AND status='pending'",
                (status, staff, _iso(now), final_text, reason, rid))
            return cur.rowcount == 1

    def set_review_message_ref(self, rid: int, ref: str) -> None:
        self.x("UPDATE reviews SET staff_message_ref=? WHERE id=?", (ref, rid))

    # ------------------------------------------------------------------ analytics, outcomes, calibration, settings
    def log(self, business_id: str, cid: Optional[str], kind: str, payload: dict, now: datetime) -> None:
        self.x("INSERT INTO events (business_id, conversation_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
               (business_id, cid, kind, dumps(payload), _iso(now)))

    def events(self, business_id: str, kind: Optional[str] = None, since: Optional[datetime] = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE business_id=?", [business_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if since:
            sql += " AND created_at >= ?"
            args.append(_iso(since))
        out = []
        for r in self.q(sql + " ORDER BY id", tuple(args)):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def record_outcome(self, cid: str, business_id: str, booked: bool, now: datetime, actual_spend: Optional[Decimal] = None,
                       guests: Optional[int] = None, no_show: bool = False, resold: Optional[bool] = None,
                       p_class: Optional[str] = None, blocker: Optional[str] = None, contribution: Optional[Decimal] = None) -> None:
        self.x("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (cid, business_id, int(booked), str(actual_spend) if actual_spend is not None else None, guests, int(no_show),
                None if resold is None else int(resold), p_class, blocker, str(contribution) if contribution is not None else None,
                _iso(now)))

    def outcomes(self, business_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM outcomes WHERE business_id=?", (business_id,))]

    def approve_calibration(self, business_id: str, key: str, value: Decimal, by: str, now: datetime) -> None:
        self.x("INSERT INTO calibration (business_id, key, value, approved_by, created_at) VALUES (?,?,?,?,?)",
               (business_id, key, str(value), by, _iso(now)))

    def calibrated(self, business_id: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for r in self.q("SELECT key, value FROM calibration WHERE business_id=? ORDER BY id", (business_id,)):
            out[r["key"]] = Decimal(r["value"])
        return out

    def get_setting(self, business_id: str, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self.q("SELECT value FROM settings WHERE business_id=? AND key=?", (business_id, key))
        return rows[0]["value"] if rows else default

    def set_setting(self, business_id: str, key: str, value: str) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (business_id, key, value))
