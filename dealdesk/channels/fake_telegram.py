"""An in-memory Telegram for tests and demos: same interface as HttpTelegram."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Optional


class FakeTelegram:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.callbacks: list[tuple[str, str]] = []
        self.cleared: list[tuple] = []
        self._uid = itertools.count(1)
        self._mid = itertools.count(1000)

    # --- what the bot calls
    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        out = [u for u in self.updates if u["update_id"] >= offset]
        return out

    def send_message(self, chat_id, text, buttons=None, reply_to=None, force_reply=False) -> dict:
        m = {"message_id": next(self._mid), "chat_id": chat_id, "text": text, "buttons": buttons or [],
             "reply_to": reply_to, "force_reply": force_reply}
        self.sent.append(m)
        return m

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))

    def clear_buttons(self, chat_id, message_id) -> None:
        self.cleared.append((chat_id, message_id))

    # --- what a test does
    def _ts(self, at: Optional[datetime], tz) -> int:
        at = at or datetime.now()
        return int(at.replace(tzinfo=tz).astimezone(timezone.utc).timestamp())

    def customer_says(self, chat_id: int, text: str, at: datetime, tz, first_name: str = "Mei", user_id: Optional[int] = None) -> dict:
        u = {"update_id": next(self._uid), "message": {"message_id": next(self._mid), "date": self._ts(at, tz), "text": text,
                                                       "chat": {"id": chat_id, "type": "private"},
                                                       "from": {"id": user_id or chat_id, "first_name": first_name}}}
        self.updates.append(u)
        return u

    def staff_says(self, group_id: int, user_id: int, text: str, at: datetime, tz, first_name: str = "Daniel",
                   reply_to: Optional[int] = None) -> dict:
        msg = {"message_id": next(self._mid), "date": self._ts(at, tz), "text": text,
               "chat": {"id": group_id, "type": "supergroup"}, "from": {"id": user_id, "first_name": first_name}}
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        u = {"update_id": next(self._uid), "message": msg}
        self.updates.append(u)
        return u

    def press(self, group_id: int, user_id: int, data: str, message_id: int, first_name: str = "Daniel") -> dict:
        u = {"update_id": next(self._uid), "callback_query": {"id": f"cq{next(self._mid)}", "data": data,
                                                               "from": {"id": user_id, "first_name": first_name},
                                                               "message": {"message_id": message_id,
                                                                           "chat": {"id": group_id, "type": "supergroup"}}}}
        self.updates.append(u)
        return u

    def to(self, chat_id) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == chat_id]
