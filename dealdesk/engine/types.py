"""Plain data passed into and out of the engine. No I/O, no clocks, no model calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from ..money import ZERO

Decision = Literal[
    "keep_quote", "offer_approved_concession", "suggest_alternative", "escalate", "rejected", "not_offerable"
]


@dataclass
class PlacementPart:
    resource_id: str
    units: int = 1
    unit_ids: Optional[list[str]] = None
    hours: Optional[Decimal] = None  # for kind == "hours"


@dataclass
class Placement:
    parts: list[PlacementPart]
    start: datetime
    end: datetime
    label: str = ""
    session_type: Optional[str] = None  # pricing key; derived from the calendar when None

    @property
    def hours(self) -> Decimal:
        return Decimal(str((self.end - self.start).total_seconds() / 3600))

    def key(self) -> str:
        parts = "+".join(f"{p.units}x{p.resource_id}" for p in self.parts)
        return f"{parts}@{self.start.isoformat(timespec='minutes')}-{self.end.strftime('%H:%M')}"


@dataclass
class Line:
    """A sold line in a basket deal (retail, wholesale, bar order)."""
    product_id: str
    qty: int
    unit_price: Optional[Decimal] = None  # None = list price


@dataclass
class ConcessionLine:
    perk_id: str
    qty: int = 1


@dataclass
class Terms:
    deposit: bool = False
    prepay: bool = False
    credit_days: int = 0


@dataclass
class Candidate:
    offer_id: str
    label: str
    kind: Literal["standard", "concession", "alternative", "clarify"] = "standard"
    placement: Optional[Placement] = None
    pricing: Literal["minimum_spend", "fixed", "basket"] = "minimum_spend"
    floor: Optional[Decimal] = None  # minimum spend override (e.g. package floor)
    price: Optional[Decimal] = None  # fixed price override (e.g. the price the customer offered)
    basket: list[Line] = field(default_factory=list)
    concessions: list[ConcessionLine] = field(default_factory=list)  # all, including ones granted earlier
    new_concessions: list[str] = field(default_factory=list)  # perk ids new in this turn
    terms: Terms = field(default_factory=Terms)
    conditions: list[str] = field(default_factory=list)
    p_class: Optional[str] = None
    package_id: Optional[str] = None
    facts: list[str] = field(default_factory=list)  # no-cost clarifications to state (layout, wet-weather policy)


@dataclass
class Booking:
    resource_id: str
    unit_id: str
    start: datetime
    end: datetime
    status: Literal["confirmed", "held"] = "confirmed"
    ref: str = ""


@dataclass
class CalendarView:
    """Snapshot of the calendar and live signals the engine needs. Built fresh every turn."""
    bookings: list[Booking] = field(default_factory=list)
    utilisation: dict[str, Decimal] = field(default_factory=dict)  # "2026-11-22:weekend_eve" -> 0..1
    chance: dict[str, Decimal] = field(default_factory=dict)  # "2026-11-21:sat_peak_1" -> booking chance
    high_demand: set[str] = field(default_factory=set)  # "2026-11-21" or "2026-11-21:weekend_eve"
    stock: dict[str, int] = field(default_factory=dict)  # daily perk stock, e.g. {"cakes": 3}
    product_on_hand: dict[str, int] = field(default_factory=dict)  # live counts override profile
    hours_booked: dict[str, Decimal] = field(default_factory=dict)  # "resource:2026-11-22" -> hours


@dataclass
class DealContext:
    now: datetime
    party_size: int = 0
    occasion: Optional[str] = None
    byo_bottles: int = 0
    blocker: str = "none"
    sender_tier: str = "front_line"
    calendar: CalendarView = field(default_factory=CalendarView)
    posture: Any = None  # finance.posture.FinancialPosture
    p_overrides: dict[str, Decimal] = field(default_factory=dict)  # calibrated close rates by class

    def lead_time_hours(self, start: datetime) -> Decimal:
        return Decimal(str(max(0.0, (start - self.now).total_seconds() / 3600)))


@dataclass
class DisplacedItem:
    """One line of opportunity cost, kept for the staff rationale and the escalation packet."""
    what: str
    p: Decimal
    value: Decimal
    amount: Decimal
    note: str = ""


@dataclass
class Figures:
    offer_id: str
    label: str
    kind: str
    revenue: Decimal = ZERO
    revenue_fnb: Decimal = ZERO
    fees: Decimal = ZERO
    variable_cost: Decimal = ZERO
    concession_true_cost: Decimal = ZERO  # catalogue cost of perks given (cake, drinks ...)
    contribution: Decimal = ZERO
    opportunity_cost: Decimal = ZERO
    displaced: list[DisplacedItem] = field(default_factory=list)
    incremental_value: Decimal = ZERO
    min_contribution: Decimal = ZERO
    min_price: Optional[Decimal] = None
    price_quoted: Optional[Decimal] = None
    p_book: Decimal = ZERO
    expected_contribution: Decimal = ZERO
    breakeven_p_book: Optional[Decimal] = None
    concession_cost: Decimal = ZERO  # contribution given up vs the standard terms on the same basis
    cumulative_concession: Decimal = ZERO
    spend_case: str = "base"
    checks: dict[str, bool] = field(default_factory=dict)
    authority_required: str = "front_line"
    decision: Decision = "rejected"
    rule: str = ""
    conditions: list[str] = field(default_factory=list)
    sendable: bool = False
    escalatable: bool = False
    has_price_discount: bool = False
    available: bool = True
    unavailable_reason: str = ""
    session_type: Optional[str] = None
    high_demand: bool = False
    assumptions: dict[str, Any] = field(default_factory=dict)
    breakdown: list[tuple[str, Decimal]] = field(default_factory=list)
    candidate: Optional[Candidate] = None

    def to_schema(self) -> dict:
        """The decision schema returned by evaluate_offers (spec, Architecture section)."""
        def f(x: Optional[Decimal]) -> Optional[float]:
            return None if x is None else float(x)

        return {
            "offer_id": self.offer_id,
            "label": self.label,
            "revenue": f(self.revenue),
            "contribution": f(self.contribution),
            "opportunity_cost": f(self.opportunity_cost),
            "incremental_value": f(self.incremental_value),
            "min_price": f(self.min_price),
            "p_book": f(self.p_book),
            "expected_contribution": f(self.expected_contribution),
            "breakeven_p_book": f(self.breakeven_p_book),
            "concession_cost": f(self.concession_cost),
            "cumulative_concession": f(self.cumulative_concession),
            "checks": dict(self.checks),
            "authority_required": self.authority_required,
            "decision": self.decision,
            "rule": self.rule,
            "conditions": list(self.conditions),
            "sendable": self.sendable,
        }


@dataclass
class TurnDecision:
    """The engine's verdict across all candidates for one turn."""
    decision: Decision
    chosen: Optional[Figures]
    standard: Optional[Figures]
    fallback: Optional[Figures]  # best sendable offer, used if an escalation times out
    ranked: list[Figures]
    rule: str
