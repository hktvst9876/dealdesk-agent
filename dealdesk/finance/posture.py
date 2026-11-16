"""Profit-and-loss adaptation.

Reads a P&L snapshot for the current period and derives the business's financial
posture. The posture can only move settings inside bounds the owner set in the
profile; it never moves the hard floors (price >= variable cost + opportunity
cost, catalogue only, cumulative cap, sender authority).

    contribution margin ratio  = (revenue - COGS - variable costs) / revenue
    break-even revenue         = fixed costs / CM ratio
    target revenue             = (fixed costs + profit target) / CM ratio
    pace                       = revenue to date / (target revenue x days elapsed / days in period)
    runway                     = cash on hand / monthly cash burn
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..money import ONE, ZERO, D, fmt_money, pct
from ..profile.schema import BusinessProfile, PnLSnapshot


@dataclass
class FinancialPosture:
    period: str
    cm_ratio: Decimal
    actual_cogs_pct: Decimal
    breakeven_revenue: Decimal
    target_revenue: Decimal
    expected_to_date: Decimal
    pace: Decimal
    pace_label: str
    min_contribution_multiplier: Decimal = ONE
    cogs_pct: Optional[Decimal] = None  # override used by the engine when actual COGS runs higher
    cogs_drift: Decimal = ZERO
    concession_budget: Optional[Decimal] = None
    concession_budget_remaining: Optional[Decimal] = None
    runway_months: Optional[Decimal] = None
    cost_of_capital: Optional[Decimal] = None
    prefer_deposits: bool = False
    notes: list[str] = field(default_factory=list)
    estimate: bool = False

    def summary(self, symbol: str = "$") -> list[str]:
        lines = [
            f"Period {self.period}: contribution margin {pct(self.cm_ratio)}, actual COGS {pct(self.actual_cogs_pct)}",
            f"Break-even revenue {fmt_money(self.breakeven_revenue, symbol)}; target {fmt_money(self.target_revenue, symbol)}",
            f"Pace {pct(self.pace)} of target to date ({self.pace_label})",
        ]
        if self.concession_budget is not None:
            lines.append(f"Concession budget {fmt_money(self.concession_budget, symbol)}, remaining "
                         f"{fmt_money(self.concession_budget_remaining, symbol)}")
        if self.runway_months is not None:
            lines.append(f"Cash runway {self.runway_months:.1f} months")
        lines += self.notes
        if self.estimate:
            lines.append("(P&L snapshot is marked as an estimate)")
        return lines


def derive_posture(profile: BusinessProfile, pnl: Optional[PnLSnapshot] = None) -> Optional[FinancialPosture]:
    pnl = pnl or profile.finance.pnl
    if pnl is None:
        return None
    b = profile.finance.bounds
    rev = D(pnl.revenue_to_date)
    if rev <= 0:
        cm_ratio = ONE - D(profile.economics.cogs_pct)
        actual_cogs = D(profile.economics.cogs_pct)
    else:
        cm_ratio = (rev - D(pnl.cogs_to_date) - D(pnl.variable_costs_to_date)) / rev
        actual_cogs = D(pnl.cogs_to_date) / rev
    cm_ratio = max(cm_ratio, Decimal("0.01"))
    breakeven = D(pnl.fixed_costs_period) / cm_ratio
    target = (D(pnl.fixed_costs_period) + D(pnl.profit_target_period)) / cm_ratio
    elapsed = max(1, pnl.days_elapsed)
    expected = target * D(elapsed) / D(pnl.days_in_period)
    pace = rev / expected if expected > 0 else ONE
    label = "on pace"
    if pace < D(b.behind_pace_below):
        label = "behind pace"
    elif pace > D(b.ahead_pace_above):
        label = "ahead of pace"

    post = FinancialPosture(pnl.period, cm_ratio, actual_cogs, breakeven, target, expected, pace, label,
                            estimate=pnl.estimate)

    # 1. Behind pace: relax the minimum contribution on quiet (non-high-demand) capacity,
    #    by how far behind we are, capped by the owner's bound. Opportunity cost still applies.
    if label == "behind pace":
        relief = min(D(b.max_min_contribution_relief_pct), D(b.behind_pace_below) - pace)
        post.min_contribution_multiplier = ONE - relief
        post.notes.append(f"Behind pace: minimum contribution on quiet capacity relaxed by {pct(relief)} "
                          f"(owner cap {pct(b.max_min_contribution_relief_pct)}); busy sessions unchanged.")
    elif label == "ahead of pace":
        post.notes.append("Ahead of pace: no extra give-aways needed; settings unchanged.")

    # 2. Cost drift: if real COGS runs above the configured ratio, price with the higher one.
    post.cogs_drift = actual_cogs - D(profile.economics.cogs_pct)
    if rev > 0 and abs(post.cogs_drift) > D(b.cogs_drift_alert):
        direction = "above" if post.cogs_drift > 0 else "below"
        post.notes.append(f"COGS is running at {pct(actual_cogs)}, {direction} the configured "
                          f"{pct(profile.economics.cogs_pct)}: update the profile.")
        if post.cogs_drift > 0 and b.use_higher_cogs:
            post.cogs_pct = actual_cogs

    # 3. Concession budget for the period.
    budget = profile.policy.period_concession_budget
    if budget is not None:
        post.concession_budget = D(budget)
        post.concession_budget_remaining = D(budget) - D(pnl.concessions_to_date)
        if post.concession_budget_remaining <= 0:
            post.notes.append("Concession budget for the period is used up: any concession now needs a manager.")

    # 4. Cash: short runway makes waiting for money expensive and favours deposits.
    if pnl.cash_on_hand is not None and pnl.monthly_cash_burn:
        post.runway_months = D(pnl.cash_on_hand) / D(pnl.monthly_cash_burn)
        if post.runway_months < D(b.runway_alert_months):
            post.cost_of_capital = D(b.stressed_cost_of_capital)
            post.prefer_deposits = True
            post.notes.append(f"Cash runway {post.runway_months:.1f} months: credit terms are priced at "
                              f"{pct(b.stressed_cost_of_capital)} a year and deposits are preferred.")
    return post
