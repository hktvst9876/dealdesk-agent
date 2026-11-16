"""What the language model may return. Everything here is words and catalogue ids, never prices."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal["enquire", "negotiate", "accept", "decline", "request_call", "ask_question", "thinking", "cancel", "chit_chat"]
BlockerT = Literal["price", "fee", "occasion", "fit", "risk", "process", "timing", "none"]
Topic = Literal["vip", "weather", "layout", "pool", "cancellation", "payment", "menu", "children", "parking", "other"]


class SeatRequest(BaseModel):
    resource_id: str = Field(description="id from the business's inventory list")
    units: int


class BasketItem(BaseModel):
    product_id: str = Field(description="id from the business's product list")
    qty: int


class ConcessionAsk(BaseModel):
    perk_id: str = Field(description="id from the perk catalogue that the customer explicitly asked for")
    qty: int = 1


class Understanding(BaseModel):
    """Step 1 output: the enquiry fields found in the latest message and the likely blocker."""
    customer_first_name: Optional[str] = None
    date: Optional[str] = Field(None, description="YYYY-MM-DD, resolved against today's date")
    start_time: Optional[str] = Field(None, description="HH:MM 24h")
    end_time: Optional[str] = Field(None, description="HH:MM 24h, or null")
    session_id: Optional[str] = Field(None, description="id from the business's session list if the customer named one")
    party_size: Optional[int] = None
    occasion: Optional[str] = Field(None, description="one lowercase word: birthday, anniversary, wedding, celebration, corporate ...")
    seating: list[SeatRequest] = Field(default_factory=list)
    byo_bottles: Optional[int] = Field(None, description="bottles the group plans to bring")
    offered_price: Optional[float] = Field(None, description="a price the customer offered, if any")
    basket: list[BasketItem] = Field(default_factory=list)
    deadline: Optional[str] = Field(None, description="YYYY-MM-DD HH:MM if the customer must confirm by a time")
    intent: Intent = "enquire"
    blocker: BlockerT = "none"
    requested: list[ConcessionAsk] = Field(default_factory=list)
    requested_discount_pct: Optional[float] = Field(None, description="percentage the customer asked off, e.g. 15")
    asks_about: list[Topic] = Field(default_factory=list)
    clarifying_question: Optional[str] = Field(
        None, description="ONE question, only if the blocker is unclear AND the answer would change the offer. Never ask about budget.")


class ReplyText(BaseModel):
    text: str


@dataclass
class Inbound:
    channel: str
    chat_ref: str
    message_id: str
    text: str
    at: datetime  # business-local time
    sender_name: Optional[str] = None
    sender_user_id: Optional[int] = None


@dataclass
class Outbound:
    to: Literal["customer", "staff"]
    chat_ref: Optional[str]
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, callback data)
    review_id: Optional[int] = None


@dataclass
class TurnResult:
    conversation_id: str
    decision: str
    draft: Optional[str] = None
    outbound: list[Outbound] = field(default_factory=list)
    review_id: Optional[int] = None
    escalation_id: Optional[int] = None
    hold_ids: list[str] = field(default_factory=list)
    figures: list[dict] = field(default_factory=list)
    rationale: str = ""
    duplicate: bool = False
    autosent: bool = False
    understanding: Optional[Understanding] = None
    notes: list[str] = field(default_factory=list)
