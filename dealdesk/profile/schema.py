"""Business profile: everything that makes one business different from another.

The engine never hard-codes beach beds, corkage or Singapore dollars. A new
business is a new profile (YAML or the Excel template) plus, optionally, adapters.
Validation runs on load and rejects inconsistent caps, tiers, floors and
references, so a bad profile fails loudly before it can produce a bad deal.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..money import D

Tier = Literal["front_line", "manager", "owner"]
TIER_ORDER: dict[str, int] = {"front_line": 0, "manager": 1, "owner": 2}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

Blocker = Literal["price", "fee", "occasion", "fit", "risk", "process", "timing", "none"]
PClass = Literal["standard", "fee_concession", "perk", "price", "alternative", "terms", "clarify"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


def expand_days(key: str) -> list[str]:
    """'mon_fri' -> mon..fri, 'sat_sun' -> sat, sun, 'daily' -> all, 'tue' -> tue."""
    key = key.strip().lower()
    if key in ("daily", "all", "every_day"):
        return list(DAYS)
    if "_" in key:
        a, b = key.split("_", 1)
        if a in DAYS and b in DAYS:
            i, j = DAYS.index(a), DAYS.index(b)
            return DAYS[i : j + 1] if i <= j else DAYS[i:] + DAYS[: j + 1]
    if key in DAYS:
        return [key]
    raise ValueError(f"unknown day group '{key}' (use mon..sun, mon_fri, sat_sun or daily)")


def _hhmm(value: str) -> str:
    h, m = value.split(":")
    if not (0 <= int(h) <= 24 and 0 <= int(m) < 60):
        raise ValueError(f"bad time {value}")
    return f"{int(h):02d}:{int(m):02d}"


# --------------------------------------------------------------------------- business

class ReplySLA(Strict):
    staffed_minutes: int = 15
    out_of_hours_minutes: int = 60


class Tax(Strict):
    """Engine works on net revenue. If list prices include tax, they are netted first."""
    prices_include_tax: bool = False
    gst_rate: Decimal = Decimal("0")
    service_charge_rate: Decimal = Decimal("0")

    def net(self, amount: Decimal) -> Decimal:
        if not self.prices_include_tax:
            return D(amount)
        return D(amount) / ((1 + self.service_charge_rate) * (1 + self.gst_rate))


class BusinessInfo(Strict):
    id: str
    name: str
    kind: str = "hospitality"
    currency: str = "SGD"
    currency_symbol: str = "$"
    timezone: str = "UTC"
    reply_sla: ReplySLA = ReplySLA()
    staffed_hours: dict[str, str] = Field(default_factory=lambda: {"daily": "09:00-22:00"})
    desk_hours: Optional[dict[str, str]] = None
    online_cap_guests: Optional[int] = None
    booking_url: Optional[str] = None
    paylink_url: Optional[str] = None
    tone: str = "Warm, specific, short. Lead with the plan for their group, then the number, then one next step."
    house_rules: dict[str, str] = Field(default_factory=dict)
    tax: Tax = Tax()
    late_close: str = "23:59"

    @field_validator("staffed_hours", "desk_hours")
    @classmethod
    def _check_hours(cls, v):
        if v is None:
            return v
        for k, span in v.items():
            expand_days(k)
            a, b = span.split("-")
            _hhmm(a), _hhmm(b)
        return v

    def hours_for(self, day: str, desk: bool = False) -> Optional[tuple[str, str]]:
        table = (self.desk_hours or self.staffed_hours) if desk else self.staffed_hours
        for k, span in table.items():
            if day in expand_days(k):
                a, b = span.split("-")
                return _hhmm(a), _hhmm(b)
        return None


class Session(Strict):
    id: str
    type: str
    days: list[str]
    start: str
    end: str  # "late" maps to business.late_close
    resources: Optional[list[str]] = None

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        out: list[str] = []
        for d in v:
            out.extend(expand_days(d))
        return out


# --------------------------------------------------------------------------- inventory

class CostToServe(Strict):
    per_booking: Decimal = Decimal("0")
    per_guest: Decimal = Decimal("0")
    per_hour: Decimal = Decimal("0")


class RegularDemand(Strict):
    """What this capacity earns from regular customers if the deal does not take it.

    mode 'matrix': booking chance per unit = P(resell) from the deal-policy matrix
                   (lead time x forecast utilisation), unless a forecast overrides it.
    mode 'chance': explicit booking chance per session (karaoke-style), and a regular
                   customer is absorbed (not lost) when an equivalent unit is free.
    """
    mode: Literal["matrix", "chance"] = "matrix"
    contribution: dict[str, Decimal] = Field(default_factory=dict)  # session type or id -> per unit
    chance: dict[str, Decimal] = Field(default_factory=dict)  # "sat:id", id or type -> booking chance
    forecast_utilisation: dict[str, Decimal] = Field(default_factory=dict)  # "sun:type", id or type -> 0..1


class Resource(Strict):
    id: str
    label: str
    kind: Literal["unit", "hours", "unlimited"] = "unit"
    units: int = 1
    unit_ids: Optional[list[str]] = None
    guests_per_unit: Optional[int] = None
    setup_minutes: int = 0
    cleanup_minutes: int = 0
    pricing: Literal["minimum_spend", "fixed", "none"] = "minimum_spend"
    floors: dict[str, Decimal] = Field(default_factory=dict)  # session type -> minimum spend per unit
    prices: dict[str, Decimal] = Field(default_factory=dict)  # session type -> fixed price per unit
    price_per: Literal["booking", "hour"] = "booking"
    cost_to_serve: CostToServe = CostToServe()
    regular: Optional[RegularDemand] = None
    outdoor: bool = False
    # kind == "hours": people or machine time
    hours_capacity_per_day: Decimal = Decimal("0")
    forecast_hours_per_day: dict[str, Decimal] = Field(default_factory=dict)  # weekday -> expected regular hours
    contribution_per_hour: Decimal = Decimal("0")
    overtime_premium_per_hour: Optional[Decimal] = None
    min_contribution: Optional[Decimal] = None
    estimate: bool = False

    @model_validator(mode="after")
    def _units(self):
        if self.unit_ids is None:
            self.unit_ids = [f"{self.id}#{i+1}" for i in range(self.units)]
        if len(self.unit_ids) != self.units:
            raise ValueError(f"resource {self.id}: unit_ids has {len(self.unit_ids)} ids but units={self.units}")
        return self


class Product(Strict):
    """A stocked item (drinks, food, retail goods). Drives the stock opportunity-cost model."""
    id: str
    label: str
    category: str = "general"
    price: Decimal
    unit_cost: Decimal
    on_hand: Optional[int] = None  # None = not tracked: plentiful and replenishable
    daily_velocity: Decimal = Decimal("0")  # full-price units expected to sell per day
    replenishable: bool = True
    days_to_restock: Decimal = Decimal("7")
    expiry_days: Optional[Decimal] = None
    salvage_value: Decimal = Decimal("0")
    estimate: bool = False


class Fee(Strict):
    amount: Decimal
    per: Literal["bottle", "booking", "guest", "item", "hour"] = "booking"
    label: Optional[str] = None


# --------------------------------------------------------------------------- economics

class SpendPerGuest(Strict):
    base: Decimal
    conservative: Decimal

    @model_validator(mode="after")
    def _order(self):
        if self.conservative > self.base:
            raise ValueError("conservative spend per guest must not exceed the base case")
        return self


class FutureValue(Strict):
    enabled: bool = False
    per_booking: Decimal = Decimal("0")
    evidence: Optional[str] = None


class NoShow(Strict):
    enabled: bool = False
    rate: Decimal = Decimal("0")
    rate_with_deposit: Decimal = Decimal("0")
    fee_recovered: Decimal = Decimal("0")


class Economics(Strict):
    cogs_pct: Decimal = Decimal("0.30")
    service_cost_per_guest: Decimal = Decimal("0")
    spend_per_guest: Optional[SpendPerGuest] = None
    spend_by_session: dict[str, SpendPerGuest] = Field(default_factory=dict)
    byo_displaced_margin_per_bottle: Decimal = Decimal("0")
    future_value: FutureValue = FutureValue()
    cost_of_capital_annual: Decimal = Decimal("0.10")
    bad_debt_rate: Decimal = Decimal("0")
    no_show: NoShow = NoShow()
    price_leakage_factor: Decimal = Decimal("0")  # share of a price cut assumed to leak to other buyers


# --------------------------------------------------------------------------- concessions

class Eligibility(Strict):
    occasions: Optional[list[str]] = None
    min_guests: int = 0
    max_guests: Optional[int] = None
    or_occasion: bool = False  # eligible if min_guests met OR a celebration is stated
    outdoor_only: bool = False
    max_lead_time_hours: Optional[Decimal] = None  # e.g. floor discount: lead time under 48h ...
    max_utilisation: Optional[Decimal] = None  # ... or utilisation under 60%
    lead_or_utilisation: bool = False  # True: either condition is enough
    min_basket: Decimal = Decimal("0")
    resources: Optional[list[str]] = None


class Perk(Strict):
    """One concession the agent may mention. If it is not here, the agent cannot offer it."""
    id: str
    label: str
    type: Literal["item", "fee_waiver", "price_discount", "package", "extension", "terms", "clarify", "threshold_reward"]
    cost: Decimal = Decimal("0")
    cost_per: Literal["unit", "guest"] = "unit"
    displaced_margin: Decimal = Decimal("0")
    product_id: Optional[str] = None
    fee_id: Optional[str] = None
    discount_pct: Optional[Decimal] = None
    scope: Optional[str] = None  # price_discount on a product category, e.g. "food"
    resource_id: Optional[str] = None  # package: where the group goes
    floor: Optional[Decimal] = None  # package: minimum spend for the package
    price: Optional[Decimal] = None  # package: fixed price
    floor_basis: Optional[Literal["requested"]] = None  # package priced at the requested placement's floor
    extra_hours: Decimal = Decimal("0")
    threshold: Optional[Decimal] = None  # threshold_reward: spend at least this
    availability: Optional[str] = None
    stock_key: Optional[str] = None
    eligibility: Eligibility = Eligibility()
    max_per_booking: int = 1
    approval: Union[Tier, dict[Tier, int]] = "front_line"
    blockers: list[Blocker] = Field(default_factory=list)
    p_class: PClass = "perk"
    customer_text: str = ""
    condition: Optional[str] = None
    estimate: bool = False

    def unit_true_cost(self) -> Decimal:
        return D(self.cost) + D(self.displaced_margin)

    def tier_for_qty(self, qty: int) -> Optional[str]:
        """Lowest tier allowed to give `qty`, or None if above every tier's limit."""
        if isinstance(self.approval, str):
            return self.approval if qty <= self.max_per_booking else None
        for tier in sorted(self.approval, key=lambda t: TIER_ORDER[t]):
            if qty <= self.approval[tier]:
                return tier
        return None


# --------------------------------------------------------------------------- policy

class HighDemand(Strict):
    utilisation_above: Decimal = Decimal("0.80")
    block_price_discounts: bool = True
    hold_minutes: int = 30
    event_dates: list[str] = Field(default_factory=list)
    public_holidays: list[str] = Field(default_factory=list)


class HoldPolicy(Strict):
    default_minutes: int = 120
    evening_enquiry_after: str = "18:00"
    evening_enquiry_until: str = "10:00"
    never_within_hours_of_session: Decimal = Decimal("3")
    one_per_customer_per_session: bool = True


class Policy(Strict):
    min_contribution: Decimal = Decimal("0")
    concession_cap: Decimal = Decimal("0")
    authority: dict[str, Decimal] = Field(default_factory=lambda: {"front_line": Decimal("0"), "manager": Decimal("0")})
    high_demand: HighDemand = HighDemand()
    hold: HoldPolicy = HoldPolicy()
    p_resell_matrix: Optional[str] = None  # CSV path, relative to the profile file
    p_resell_default: Decimal = Decimal("0.5")
    p_book_defaults: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "standard": Decimal("0.40"), "fee_concession": Decimal("0.50"), "perk": Decimal("0.55"),
            "price": Decimal("0.55"), "alternative": Decimal("0.35"), "terms": Decimal("0.45"), "clarify": Decimal("0.40"),
        }
    )
    p_book_by_blocker: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    period_concession_budget: Optional[Decimal] = None
    escalation_minutes: int = 30
    max_alternatives: int = 2

    def authority_limit(self, tier: str) -> Decimal:
        if tier == "owner":
            return Decimal("Infinity")
        return D(self.authority.get(tier, 0))

    def tier_for_amount(self, amount: Decimal) -> str:
        if amount <= self.authority_limit("front_line"):
            return "front_line"
        if amount <= self.authority_limit("manager"):
            return "manager"
        return "owner"


class Callbacks(Strict):
    default_minutes: int = 30
    if_session_within_hours: dict[int, int] = Field(default_factory=lambda: {4: 10})


# --------------------------------------------------------------------------- finance

class PnLSnapshot(Strict):
    period: str
    days_in_period: int
    days_elapsed: int
    revenue_to_date: Decimal
    cogs_to_date: Decimal
    variable_costs_to_date: Decimal = Decimal("0")
    fixed_costs_period: Decimal
    profit_target_period: Decimal = Decimal("0")
    concessions_to_date: Decimal = Decimal("0")
    cash_on_hand: Optional[Decimal] = None
    monthly_cash_burn: Optional[Decimal] = None
    estimate: bool = False


class FinanceBounds(Strict):
    behind_pace_below: Decimal = Decimal("0.95")
    ahead_pace_above: Decimal = Decimal("1.05")
    max_min_contribution_relief_pct: Decimal = Decimal("0.20")
    runway_alert_months: Decimal = Decimal("3")
    stressed_cost_of_capital: Decimal = Decimal("0.25")
    cogs_drift_alert: Decimal = Decimal("0.03")
    use_higher_cogs: bool = True


class Finance(Strict):
    pnl: Optional[PnLSnapshot] = None
    bounds: FinanceBounds = FinanceBounds()


# --------------------------------------------------------------------------- people, rollout

class StaffMember(Strict):
    name: str
    tier: Tier = "front_line"
    telegram_user_id: Optional[int] = None
    on_duty: bool = True


class Rollout(Strict):
    autosend: dict[str, bool] = Field(
        default_factory=lambda: {"keep_quote": False, "offer_approved_concession": False, "suggest_alternative": False, "escalate": False}
    )
    out_of_hours_autosend_keep_quote: bool = False


class TelegramCfg(Strict):
    staff_chat_id: Optional[int] = None
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"


class Channels(Strict):
    telegram: TelegramCfg = TelegramCfg()


# --------------------------------------------------------------------------- root

class BusinessProfile(Strict):
    business: BusinessInfo
    sessions: list[Session] = Field(default_factory=list)
    inventory: list[Resource] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    economics: Economics = Economics()
    fees: dict[str, Fee] = Field(default_factory=dict)
    perks: list[Perk] = Field(default_factory=list)
    policy: Policy = Policy()
    callbacks: Callbacks = Callbacks()
    finance: Finance = Finance()
    staff: list[StaffMember] = Field(default_factory=list)
    rollout: Rollout = Rollout()
    channels: Channels = Channels()
    adapters: dict[str, str] = Field(default_factory=dict)
    placeholders: list[str] = Field(default_factory=list)
    # Filled by the loader, not by authors:
    resell_matrix: Optional[list[dict]] = None

    # ---------------------------------------------------------------- lookups
    def resource(self, rid: str) -> Resource:
        for r in self.inventory:
            if r.id == rid:
                return r
        raise KeyError(f"unknown resource '{rid}'")

    def product(self, pid: str) -> Product:
        for p in self.products:
            if p.id == pid:
                return p
        raise KeyError(f"unknown product '{pid}'")

    def perk(self, pid: str) -> Perk:
        for p in self.perks:
            if p.id == pid:
                return p
        raise KeyError(f"'{pid}' is not in the perk catalogue")

    def has_perk(self, pid: str) -> bool:
        return any(p.id == pid for p in self.perks)

    def spend_case(self, session_type: Optional[str]) -> Optional[SpendPerGuest]:
        if session_type and session_type in self.economics.spend_by_session:
            return self.economics.spend_by_session[session_type]
        return self.economics.spend_per_guest

    # ---------------------------------------------------------------- validation
    @model_validator(mode="after")
    def _consistency(self):
        errors: list[str] = []
        pol = self.policy
        fl = pol.authority_limit("front_line")
        mg = pol.authority_limit("manager")
        if fl < 0 or mg < 0 or pol.concession_cap < 0 or pol.min_contribution < 0:
            errors.append("authority limits, concession cap and minimum contribution must be >= 0")
        if mg < fl:
            errors.append(f"manager authority ({mg}) is below front-line authority ({fl})")
        for k, v in pol.p_book_defaults.items():
            if not (0 <= v <= 1):
                errors.append(f"p_book_defaults.{k}={v} is not a probability")
        res_ids = {r.id for r in self.inventory}
        if len(res_ids) != len(self.inventory):
            errors.append("duplicate resource ids")
        prod_ids = {p.id for p in self.products}
        perk_ids = [p.id for p in self.perks]
        if len(set(perk_ids)) != len(perk_ids):
            errors.append("duplicate perk ids")
        session_types = {s.type for s in self.sessions}
        for s in self.sessions:
            for rid in s.resources or []:
                if rid not in res_ids:
                    errors.append(f"session {s.id} references unknown resource {rid}")
        for r in self.inventory:
            for st, amount in {**r.floors, **r.prices}.items():
                if amount < 0:
                    errors.append(f"resource {r.id}: negative floor/price for {st}")
            if r.kind == "unit" and r.pricing != "none" and self.sessions:
                priced = set(r.floors) | set(r.prices)
                if priced and not priced <= (session_types | {"default"}):
                    errors.append(f"resource {r.id}: floors/prices for unknown session types {sorted(priced - session_types)}")
        for perk in self.perks:
            if perk.fee_id and perk.fee_id not in self.fees:
                errors.append(f"perk {perk.id}: unknown fee {perk.fee_id}")
            if perk.product_id and perk.product_id not in prod_ids:
                errors.append(f"perk {perk.id}: unknown product {perk.product_id}")
            if perk.resource_id and perk.resource_id not in res_ids:
                errors.append(f"perk {perk.id}: unknown resource {perk.resource_id}")
            if perk.type == "fee_waiver" and not perk.fee_id:
                errors.append(f"perk {perk.id}: fee_waiver needs fee_id")
            if perk.type == "price_discount" and perk.discount_pct is None:
                errors.append(f"perk {perk.id}: price_discount needs discount_pct")
            if perk.type == "threshold_reward" and (perk.threshold is None or not (perk.product_id or perk.cost)):
                errors.append(f"perk {perk.id}: threshold_reward needs threshold and a reward (product_id or cost)")
            # Approval limits must fit inside the authority they claim.
            unit_cost = perk.unit_true_cost()
            if perk.type == "fee_waiver" and perk.fee_id in self.fees:
                unit_cost = D(self.fees[perk.fee_id].amount)
            if perk.product_id and perk.product_id in prod_ids:
                unit_cost = max(unit_cost, D(self.product(perk.product_id).unit_cost))
            if isinstance(perk.approval, dict):
                prev = 0
                for tier in sorted(perk.approval, key=lambda t: TIER_ORDER[t]):
                    qty = perk.approval[tier]
                    if qty < prev:
                        errors.append(f"perk {perk.id}: approval quantities must rise with tier")
                    prev = qty
                    if tier != "owner" and unit_cost * qty > pol.authority_limit(tier):
                        errors.append(
                            f"perk {perk.id}: {tier} may give {qty} (true cost {unit_cost * qty}) "
                            f"but {tier} authority is {pol.authority_limit(tier)}"
                        )
                if max(perk.approval.values()) > perk.max_per_booking:
                    errors.append(f"perk {perk.id}: approval quantity exceeds max_per_booking")
            elif perk.approval != "owner" and unit_cost > 0 and unit_cost > pol.authority_limit(perk.approval):
                errors.append(
                    f"perk {perk.id}: approvable by {perk.approval} but one unit costs {unit_cost} "
                    f"(> {perk.approval} authority {pol.authority_limit(perk.approval)})"
                )
        if self.economics.spend_per_guest is None and any(r.pricing == "minimum_spend" for r in self.inventory):
            errors.append("minimum-spend resources need economics.spend_per_guest")
        if not (0 <= self.economics.cogs_pct < 1):
            errors.append("economics.cogs_pct must be in [0, 1)")
        if errors:
            raise ValueError("Business profile rejected:\n - " + "\n - ".join(errors))
        return self
