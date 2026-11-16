"""Telegram: customers chat with the business's bot privately; staff work in one private group.

Long polling (getUpdates), so the bot runs on any always-on computer without a
public web address. Customer messages go through the agent; drafts land in the
staff group as review cards with Send / Edit / Reject buttons; escalations get
Approve / Decline. Staff commands (/deal, /busy, /stock ...) work in the group.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

import httpx

from ..agent.models import Inbound, Outbound
from ..agent.turn import Agent
from ..agent.watchers import tick
from ..profile.schema import TIER_ORDER
from ..staff import commands

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramAPI(Protocol):
    def get_updates(self, offset: int, timeout: int) -> list[dict]: ...
    def send_message(self, chat_id: int | str, text: str, buttons: Optional[list[tuple[str, str]]] = None,
                     reply_to: Optional[int] = None, force_reply: bool = False) -> dict: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def clear_buttons(self, chat_id: int | str, message_id: int) -> None: ...


class HttpTelegram:
    """Thin Bot API client over httpx."""

    def __init__(self, token: str, base: str = "https://api.telegram.org", timeout: float = 45.0):
        self._url = f"{base}/bot{token}"
        self._http = httpx.Client(timeout=timeout)

    def _call(self, method: str, **params: Any) -> Any:
        for attempt in range(4):
            try:
                r = self._http.post(f"{self._url}/{method}", json=params)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == 3:
                    raise TelegramError(f"{method}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry and attempt < 3:
                time.sleep(int(retry))
                continue
            raise TelegramError(f"{method}: {data.get('description')}")

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])

    def send_message(self, chat_id, text, buttons=None, reply_to=None, force_reply=False) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
        elif force_reply:
            params["reply_markup"] = {"force_reply": True, "selective": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", **params)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    def clear_buttons(self, chat_id, message_id) -> None:
        try:
            self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass

    def get_me(self) -> dict:
        return self._call("getMe")


class TelegramRunner:
    def __init__(self, agent: Agent, api: TelegramAPI, staff_chat_id: Optional[int] = None):
        self.agent = agent
        self.api = api
        self.tz = ZoneInfo(agent.profile.business.timezone)
        bid = agent.profile.business.id
        configured = staff_chat_id or agent.profile.channels.telegram.staff_chat_id
        stored = agent.store.get_setting(bid, "staff_chat_id")
        self.staff_chat_id: Optional[int] = int(configured) if configured else (int(stored) if stored else None)
        self.offset = int(agent.store.get_setting(bid, "telegram_offset", "0"))

    # ------------------------------------------------------------------ time and identity
    def local(self, unix: Optional[int] = None) -> datetime:
        dt = datetime.fromtimestamp(unix, timezone.utc) if unix else datetime.now(timezone.utc)
        return dt.astimezone(self.tz).replace(tzinfo=None)

    def staff_identity(self, user: dict) -> tuple[str, str]:
        prof, store = self.agent.profile, self.agent.store
        uid = user.get("id")
        for s in prof.staff:
            if s.telegram_user_id and s.telegram_user_id == uid:
                return s.name, s.tier
        linked = store.get_setting(prof.business.id, f"staff:{uid}")
        if linked:
            s = next((s for s in prof.staff if s.name == linked), None)
            if s:
                return s.name, s.tier
        return user.get("first_name") or "staff", "front_line"

    def is_staff_user(self, user: dict) -> bool:
        prof = self.agent.profile
        uid = user.get("id")
        return any(s.telegram_user_id == uid for s in prof.staff) or \
            self.agent.store.get_setting(prof.business.id, f"staff:{uid}") is not None

    # ------------------------------------------------------------------ delivery
    def deliver(self, outs: list[Outbound]) -> None:
        for o in outs:
            try:
                if o.to == "customer":
                    self.api.send_message(int(o.chat_ref), o.text)
                elif self.staff_chat_id is not None:
                    msg = self.api.send_message(self.staff_chat_id, o.text, o.buttons or None)
                    if o.review_id and msg:
                        self.agent.store.set_review_message_ref(o.review_id, str(msg.get("message_id")))
                else:
                    log.warning("No staff chat configured; dropping staff message: %s", o.text[:120])
            except TelegramError as e:
                log.error("delivery failed: %s", e)

    def _staff_reply(self, text: str, reply_to: Optional[int] = None) -> None:
        if self.staff_chat_id is not None:
            self.api.send_message(self.staff_chat_id, text, reply_to=reply_to)

    # ------------------------------------------------------------------ updates
    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            return self._on_button(upd["callback_query"])
        msg = upd.get("message")
        if not msg or "text" not in msg:
            return
        chat = msg["chat"]
        user = msg.get("from", {})
        now = self.local(msg.get("date"))
        text = msg["text"]
        bid = self.agent.profile.business.id
        if chat.get("type") in ("group", "supergroup"):
            if self.staff_chat_id is None and text.split("@")[0].strip() == "/setup":
                self.staff_chat_id = chat["id"]
                self.agent.store.set_setting(bid, "staff_chat_id", str(chat["id"]))
                self._staff_reply("This group is now the staff chat for " + self.agent.profile.business.name +
                                  ". Each of you: send /iam <your name> to link your account. /help for commands.")
                return
            if chat["id"] != self.staff_chat_id:
                return  # the bot only serves private customer chats and the one staff group
            return self._on_staff_message(msg, user, text, now)
        if chat.get("type") == "private":
            if text.startswith("/") and self.is_staff_user(user) and not text.startswith("/start"):
                name, tier = self.staff_identity(user)
                for r in commands.handle(self.agent, text, name, tier, now, user.get("id")):
                    if isinstance(r, Outbound):
                        self.deliver([r])
                    else:
                        self.api.send_message(chat["id"], r)
                return
            if text.startswith("/start"):
                text = "Hi"
            inbound = Inbound("telegram", str(chat["id"]), str(msg["message_id"]), text, now,
                              user.get("first_name"), user.get("id"))
            result = self.agent.handle_customer(inbound)
            self.deliver(result.outbound)

    def _on_staff_message(self, msg: dict, user: dict, text: str, now: datetime) -> None:
        name, tier = self.staff_identity(user)
        store, bid = self.agent.store, self.agent.profile.business.id
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        if replied:
            pending = store.get_setting(bid, f"prompt:{replied}")
            if pending:
                action, rid = pending.split(":")
                store.set_setting(bid, f"prompt:{replied}", "")
                if action == "edit":
                    return self.deliver(self.agent.staff_edit(int(rid), name, text, now))
                if action == "reject":
                    return self.deliver(self.agent.staff_reject(int(rid), name, text, now))
            rows = store.q("SELECT id FROM reviews WHERE staff_message_ref=? AND status='pending'", (str(replied),))
            if rows and not text.startswith("/"):
                return self.deliver(self.agent.staff_edit(rows[0]["id"], name, text, now))
        if text.startswith("/"):
            for r in commands.handle(self.agent, text, name, tier, now, user.get("id")):
                self.deliver([r]) if isinstance(r, Outbound) else self._staff_reply(r, msg.get("message_id"))

    def _on_button(self, cq: dict) -> None:
        data = cq.get("data", "")
        user = cq.get("from", {})
        name, tier = self.staff_identity(user)
        now = self.local()
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id != self.staff_chat_id:
            self.api.answer_callback(cq["id"], "Not allowed here.")
            return
        kind, action, ident = (data.split(":") + ["", "", ""])[:3]
        store, bid = self.agent.store, self.agent.profile.business.id
        if kind == "rv" and action == "send":
            outs = self.agent.staff_send(int(ident), name, now)
            self.api.answer_callback(cq["id"], "Sent" if any(o.to == "customer" for o in outs) else "Already handled")
            self.api.clear_buttons(chat_id, msg.get("message_id"))
            return self.deliver(outs)
        if kind == "rv" and action in ("edit", "reject"):
            prompt = "Reply to this message with the full text to send." if action == "edit" else \
                "Reply to this message with the reason for rejecting (it trains the agent)."
            sent = self.api.send_message(chat_id, f"#{ident} {prompt}", force_reply=True, reply_to=msg.get("message_id"))
            store.set_setting(bid, f"prompt:{sent.get('message_id')}", f"{action}:{ident}")
            self.api.answer_callback(cq["id"])
            return
        if kind == "esc":
            outs = self.agent.decide_escalation(int(ident), action == "approve", name, tier, now)
            self.api.answer_callback(cq["id"], outs[-1].text[:150] if outs else "")
            if any(o.to == "customer" for o in outs):
                self.api.clear_buttons(chat_id, msg.get("message_id"))
            return self.deliver(outs)
        self.api.answer_callback(cq["id"], "Unknown action")

    # ------------------------------------------------------------------ loop
    def poll_once(self, timeout: int = 30) -> int:
        updates = self.api.get_updates(self.offset, timeout)
        for upd in updates:
            try:
                self.handle_update(upd)
            except Exception:  # one bad update must not stop the bot
                log.exception("update %s failed", upd.get("update_id"))
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            self.agent.store.set_setting(self.agent.profile.business.id, "telegram_offset", str(self.offset))
        return len(updates)

    def run_forever(self, tick_seconds: int = 30) -> None:
        last_tick = 0.0
        log.info("DealDesk running for %s (staff chat: %s)", self.agent.profile.business.name, self.staff_chat_id or "not set")
        while True:
            try:
                self.poll_once(timeout=25)
            except TelegramError as e:
                log.error("polling failed: %s", e)
                time.sleep(5)
            if time.monotonic() - last_tick >= tick_seconds:
                last_tick = time.monotonic()
                try:
                    self.deliver(tick(self.agent, self.local()))
                except Exception:
                    log.exception("watcher tick failed")
