"""Session grid and unit availability: which regular sessions a booking would block."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from ..money import D
from ..profile.schema import DAYS, BusinessProfile, Resource, Session
from .types import Booking, CalendarView, Placement


@dataclass
class Occurrence:
    session: Session
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        return f"{self.start.date().isoformat()}:{self.session.id}"

    @property
    def day(self) -> str:
        return DAYS[self.start.weekday()]


def _t(value: str, late: str) -> time:
    if value == "late":
        value = late
    h, m = (int(x) for x in value.split(":"))
    if h == 24:
        return time(23, 59)
    return time(h, m)


def occurrences(profile: BusinessProfile, resource: Resource, day: date) -> list[Occurrence]:
    out = []
    for s in profile.sessions:
        if s.resources is not None and resource.id not in s.resources:
            continue
        if DAYS[day.weekday()] not in s.days:
            continue
        start = datetime.combine(day, _t(s.start, profile.business.late_close))
        end = datetime.combine(day, _t(s.end, profile.business.late_close))
        if end <= start:
            end += timedelta(days=1)
        out.append(Occurrence(s, start, end))
    return out


def overlapping_occurrences(profile: BusinessProfile, resource: Resource, start: datetime, end: datetime) -> list[Occurrence]:
    """Regular sessions that [start - setup, end + cleanup] would block."""
    s = start - timedelta(minutes=resource.setup_minutes)
    e = end + timedelta(minutes=resource.cleanup_minutes)
    seen = []
    for d in {(s - timedelta(days=1)).date(), s.date(), e.date()}:
        for occ in occurrences(profile, resource, d):
            if occ.start < e and s < occ.end:
                seen.append(occ)
    return sorted(seen, key=lambda o: o.start)


def session_type_at(profile: BusinessProfile, resource: Resource, start: datetime) -> Optional[str]:
    for occ in occurrences(profile, resource, start.date()):
        if occ.start <= start < occ.end:
            return occ.session.type
    occ = overlapping_occurrences(profile, resource, start, start + timedelta(minutes=1))
    return occ[0].session.type if occ else None


def _overlaps(b: Booking, start: datetime, end: datetime) -> bool:
    return b.start < end and start < b.end


def busy_units(cal: CalendarView, resource: Resource, start: datetime, end: datetime, exclude_ref: str = "") -> set[str]:
    return {
        b.unit_id for b in cal.bookings
        if b.resource_id == resource.id and _overlaps(b, start, end) and (not exclude_ref or b.ref != exclude_ref)
    }


def confirmed_units(cal: CalendarView, resource: Resource, start: datetime, end: datetime) -> set[str]:
    return {b.unit_id for b in cal.bookings if b.resource_id == resource.id and b.status == "confirmed" and _overlaps(b, start, end)}


def pick_units(cal: CalendarView, resource: Resource, start: datetime, end: datetime, units: int,
               wanted: Optional[list[str]] = None, exclude_ref: str = "") -> tuple[Optional[list[str]], str]:
    """Choose free units for [start, end] (setup and cleanup included). Never touches a confirmed booking."""
    s = start - timedelta(minutes=resource.setup_minutes)
    e = end + timedelta(minutes=resource.cleanup_minutes)
    taken = busy_units(cal, resource, s, e, exclude_ref)
    if wanted:
        clash = [u for u in wanted if u in taken]
        if clash:
            return None, f"{', '.join(clash)} already booked or held for that time"
        return list(wanted), ""
    free = [u for u in resource.unit_ids if u not in taken]
    if len(free) < units:
        return None, f"only {len(free)} of {units} {resource.label} free for that time"
    return free[:units], ""


def utilisation(profile: BusinessProfile, cal: CalendarView, resource: Resource, occ: Occurrence) -> Decimal:
    """Forecast utilisation for one session: staff signal > calendar fill > profile baseline."""
    for key in (f"{occ.key}", f"{occ.start.date().isoformat()}:{occ.session.type}", f"{occ.start.date().isoformat()}"):
        if key in cal.utilisation:
            return D(cal.utilisation[key])
    booked = len(busy_units(cal, resource, occ.start, occ.end))
    actual = D(booked) / D(resource.units) if resource.units else D(0)
    base = D(0)
    if resource.regular:
        fu = resource.regular.forecast_utilisation
        for key in (f"{occ.day}:{occ.session.id}", f"{occ.day}:{occ.session.type}", occ.session.id, occ.session.type):
            if key in fu:
                base = D(fu[key])
                break
    return max(actual, base)


def is_high_demand(profile: BusinessProfile, cal: CalendarView, resource: Resource, occ: Occurrence) -> bool:
    d = occ.start.date().isoformat()
    hd = profile.policy.high_demand
    if d in cal.high_demand or occ.key in cal.high_demand or f"{d}:{occ.session.type}" in cal.high_demand:
        return True
    if d in hd.event_dates or d in hd.public_holidays:
        return True
    return utilisation(profile, cal, resource, occ) > hd.utilisation_above
