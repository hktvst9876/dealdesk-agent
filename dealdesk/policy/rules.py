"""Policy as data: perk eligibility and availability, holds, callbacks, escalation deadlines.

The ordered financial checks (opportunity cost -> minimum -> session rule ->
beats standard -> cap -> authority) live in engine.evaluate so the decision and
its numbers are produced in one place. This module holds the time rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..engine.calendar_math import busy_units, overlapping_occurrences, utilisation
from ..engine.types import CalendarView, Placement
from ..money import D
from ..profile.schema import BusinessProfile, Perk

CELEBRATIONS = {"birthday", "anniversary", "celebration", "graduation", "engagement", "wedding", "farewell",
                "promotion", "hen", "stag", "baby shower", "christmas", "new year", "party"}


def perk_eligible(profile: BusinessProfile, perk: Perk, *, party_size: int, occasion: Optional[str],
                  placement: Optional[Placement], calendar: CalendarView, now: datetime,
                  basket_total: Decimal = Decimal("0")) -> tuple[bool, str]:
    """Is this catalogue entry allowed and actually available for this enquiry?"""
    el = perk.eligibility
    occ = (occasion or "").lower().strip()
    occ_ok = bool(occ) and (el.occasions is None or occ in [o.lower() for o in el.occasions] or
                            (el.or_occasion and occ in CELEBRATIONS))
    size_ok = party_size >= el.min_guests and (el.max_guests is None or party_size <= el.max_guests)
    if el.or_occasion:
        if not (size_ok or occ_ok):
            return False, f"needs {el.min_guests}+ guests or a celebration"
    else:
        if el.occasions is not None and not occ_ok:
            return False, f"only for {', '.join(el.occasions)}"
        if not size_ok:
            return False, f"needs {el.min_guests}+ guests"
    if el.min_basket and basket_total < el.min_basket:
        return False, f"needs a basket of {el.min_basket}+"
    if placement is not None:
        resources = [profile.resource(p.resource_id) for p in placement.parts]
        if el.outdoor_only and not any(r.outdoor for r in resources):
            return False, "only for outdoor bookings"
        if el.resources and not any(r.id in el.resources for r in resources):
            return False, "not for this seating"
        if el.max_lead_time_hours is not None or el.max_utilisation is not None:
            lead = D((placement.start - now).total_seconds() / 3600)
            lead_ok = el.max_lead_time_hours is not None and lead < D(el.max_lead_time_hours)
            util_ok = False
            if el.max_utilisation is not None:
                res = resources[0]
                occs = overlapping_occurrences(profile, res, placement.start, placement.end)
                if occs:
                    util_ok = utilisation(profile, calendar, res, occs[0]) < D(el.max_utilisation)
            if el.lead_or_utilisation:
                if not (lead_ok or util_ok):
                    return False, "outside the discount matrix (lead time and utilisation)"
            elif (el.max_lead_time_hours is not None and not lead_ok) or (el.max_utilisation is not None and not util_ok):
                return False, "outside the discount matrix"
    # Availability: the perk must exist before the word appears in a draft.
    if perk.stock_key is not None and calendar.stock.get(perk.stock_key, 0) <= 0:
        return False, f"none available today ({perk.stock_key})"
    if perk.availability and calendar.stock.get(perk.availability, 1) <= 0:
        return False, f"{perk.availability.replace('_', ' ')} says none available"
    if perk.availability == "vip_unbooked" and perk.resource_id and placement is not None:
        res = profile.resource(perk.resource_id)
        if len(busy_units(calendar, res, placement.start, placement.end)) >= res.units:
            return False, f"{res.label} is booked"
    if perk.type == "package" and perk.resource_id and party_size and profile.resource(perk.resource_id).guests_per_unit:
        cap = profile.resource(perk.resource_id).guests_per_unit
        if party_size > cap:
            return False, f"{profile.resource(perk.resource_id).label} holds {cap}"
    return True, ""


# --------------------------------------------------------------------------- holds

def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    if h >= 24:  # "24:00" = midnight at the end of the day
        return day.replace(hour=0, minute=m, second=0, microsecond=0) + timedelta(days=1)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def hold_expiry(profile: BusinessProfile, now: datetime, session_start: datetime, high_demand: bool) -> Optional[datetime]:
    """Default 2 hours, or until 10:00 next day for evening enquiries; 30 minutes on
    high-demand sessions; never later than session start minus 3 hours.
    Returns None when no hold can be offered (the session is too close)."""
    hp = profile.policy.hold
    if high_demand:
        expiry = now + timedelta(minutes=profile.policy.high_demand.hold_minutes)
    else:
        expiry = now + timedelta(minutes=hp.default_minutes)
        if now >= _at(now, hp.evening_enquiry_after) or now < _at(now, hp.evening_enquiry_until):
            next_morning = _at(now if now < _at(now, hp.evening_enquiry_until) else now + timedelta(days=1),
                               hp.evening_enquiry_until)
            expiry = max(expiry, next_morning)
    latest = session_start - timedelta(hours=float(hp.never_within_hours_of_session))
    expiry = min(expiry, latest)
    return expiry if expiry > now else None


# --------------------------------------------------------------------------- callbacks and escalations

def callback_deadline(profile: BusinessProfile, now: datetime, session_start: Optional[datetime]) -> datetime:
    """Default 30 minutes; 10 minutes if the session is under 4 hours away."""
    minutes = profile.callbacks.default_minutes
    if session_start is not None:
        hours_away = (session_start - now).total_seconds() / 3600
        for within, mins in sorted(profile.callbacks.if_session_within_hours.items()):
            if hours_away < within:
                minutes = min(minutes, mins)
                break
    return now + timedelta(minutes=minutes)


def escalation_deadline(profile: BusinessProfile, now: datetime, session_start: Optional[datetime]) -> datetime:
    """Manager decision deadline, derived from the session time; the customer is told this time."""
    deadline = now + timedelta(minutes=profile.policy.escalation_minutes)
    if session_start is not None:
        latest = session_start - timedelta(hours=float(profile.policy.hold.never_within_hours_of_session))
        if latest > now + timedelta(minutes=5):
            deadline = min(deadline, latest)
        else:
            deadline = now + timedelta(minutes=5)
    # Round up to the next 5 minutes so the customer gets a clean time ("by 15:30").
    extra = (5 - deadline.minute % 5) % 5
    return (deadline + timedelta(minutes=extra)).replace(second=0, microsecond=0)


def in_hours(profile: BusinessProfile, now: datetime, desk: bool = True) -> bool:
    from ..profile.schema import DAYS

    span = profile.business.hours_for(DAYS[now.weekday()], desk=desk)
    if span is None:
        return False
    a, b = (_at(now, x) for x in span)
    if b <= a:  # past midnight
        return now >= a or now < b
    return a <= now < b


def reply_sla_minutes(profile: BusinessProfile, now: datetime) -> int:
    sla = profile.business.reply_sla
    return sla.staffed_minutes if in_hours(profile, now) else sla.out_of_hours_minutes
