from datetime import datetime, timedelta
from decimal import Decimal

from dealdesk.agent.models import Inbound
from dealdesk.agent.turn import Agent
from dealdesk.ledger.store import Store
from dealdesk.profile.loader import load_profile

OPEN = ("Hi, this is Mei. Looking to book for my mum's birthday on Sunday 22 Nov, 5pm till late, 17 pax, "
        "3 beds and the bean bag section. We'll bring 4 bottles of wine.")


class Convo:
    """A customer chat driven through the real agent, with staff pressing Send on every draft."""

    def __init__(self, profile="salt_beach_club", start=datetime(2026, 11, 14, 15, 0), cakes=3, brain=None,
                 auto_staff_send=True, chat="chat42", store=None, agent=None):
        self.profile = load_profile(profile) if isinstance(profile, str) else profile
        self.store = store or Store(":memory:")
        self.store.set_signal(self.profile.business.id, "stock", "cakes", cakes, start - timedelta(days=1))
        self.agent = agent or Agent(self.profile, self.store, brain=brain)
        self.t = start
        self.n = 0
        self.chat = chat
        self.auto = auto_staff_send
        self.results = []

    def say(self, text, minutes=1, message_id=None):
        self.t += timedelta(minutes=minutes)
        self.n += 1
        r = self.agent.handle_customer(Inbound("telegram", self.chat, message_id or str(self.n), text, self.t, "Mei Tan"))
        self.results.append(r)
        if self.auto and r.review_id and not r.autosent and self.store.review(r.review_id)["status"] == "pending":
            self.agent.staff_send(r.review_id, "Aisha", self.t)
        return r

    def chosen(self, r):
        """The engine figures for the offer this turn recorded."""
        offers = [o for o in self.store.offers(r.conversation_id)]
        return offers[-1]["offer"]["figures"] if offers else None

    def approve_p(self, key, value):
        self.store.approve_calibration(self.profile.business.id, key, Decimal(value), "owner", self.t)
