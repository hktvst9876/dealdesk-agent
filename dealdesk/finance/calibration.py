"""Learning from outcomes, with the owner in charge.

Close rates and resale chances are guesses at launch. Every outcome is logged;
this module turns the log into *proposals* using a Beta prior centred on the
current setting (so a handful of outcomes cannot swing it). Nothing changes
until the owner approves a proposal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from ..money import D, prob


@dataclass
class Proposal:
    key: str
    current: Decimal
    proposed: Decimal
    n: int
    successes: int
    note: str

    @property
    def change(self) -> Decimal:
        return self.proposed - self.current


def beta_posterior_mean(prior: Decimal, prior_strength: int, successes: int, n: int) -> Decimal:
    return prob((D(prior) * prior_strength + successes) / (prior_strength + n))


def propose_close_rates(current: dict[str, Decimal], outcomes: Iterable[tuple[str, bool]],
                        prior_strength: int = 20, min_outcomes: int = 30, min_change: Decimal = Decimal("0.02")) -> list[Proposal]:
    """outcomes: (p_class, booked?) for every offer the customer actually answered."""
    tally: dict[str, list[int]] = {}
    for cls, booked in outcomes:
        t = tally.setdefault(cls, [0, 0])
        t[0] += 1
        t[1] += 1 if booked else 0
    out = []
    for cls, (n, wins) in sorted(tally.items()):
        if cls not in current or n < min_outcomes:
            continue
        new = beta_posterior_mean(current[cls], prior_strength, wins, n)
        if abs(new - D(current[cls])) >= min_change:
            out.append(Proposal(f"p_book.{cls}", D(current[cls]), new, n, wins,
                                f"{wins}/{n} booked ({prob(D(wins) / n)}) vs assumed {current[cls]}"))
    return out


def propose_resell(cells: dict[str, Decimal], outcomes: Iterable[tuple[str, bool]],
                   prior_strength: int = 10, min_outcomes: int = 15) -> list[Proposal]:
    """outcomes: (matrix cell key, was the unused capacity later sold?)"""
    tally: dict[str, list[int]] = {}
    for cell, sold in outcomes:
        t = tally.setdefault(cell, [0, 0])
        t[0] += 1
        t[1] += 1 if sold else 0
    out = []
    for cell, (n, wins) in sorted(tally.items()):
        if cell not in cells or n < min_outcomes:
            continue
        new = beta_posterior_mean(cells[cell], prior_strength, wins, n)
        if abs(new - D(cells[cell])) >= Decimal("0.03"):
            out.append(Proposal(f"p_resell.{cell}", D(cells[cell]), new, n, wins, f"{wins}/{n} resold"))
    return out


def propose_spend(current_base: Decimal, current_conservative: Decimal, actual_per_guest: list[Decimal],
                  min_outcomes: int = 20) -> list[Proposal]:
    """Forecast vs actual spend per guest: base = mean, conservative = 20th percentile."""
    if len(actual_per_guest) < min_outcomes:
        return []
    xs = sorted(D(x) for x in actual_per_guest)
    mean = sum(xs) / len(xs)
    p20 = xs[max(0, int(len(xs) * 0.2) - 1)]
    out = []
    if abs(mean - current_base) >= 2:
        out.append(Proposal("spend_per_guest.base", current_base, mean.quantize(Decimal("1")), len(xs), 0,
                            f"mean actual spend per guest over {len(xs)} bookings"))
    if abs(p20 - current_conservative) >= 2:
        out.append(Proposal("spend_per_guest.conservative", current_conservative, p20.quantize(Decimal("1")), len(xs), 0,
                            "20th percentile of actual spend per guest"))
    return out
