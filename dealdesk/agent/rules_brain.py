"""Offline understanding: keyword and pattern extraction.

Runs when no API key is configured, in tests, and as the fallback whenever the
model call fails. It is deliberately conservative: when unsure it leaves a field
empty, and the turn asks one question instead of guessing.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from ..profile.schema import BusinessProfile
from .models import BasketItem, ConcessionAsk, SeatRequest, Understanding

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3,
            "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
WORDNUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
OCCASIONS = [("birthday", "birthday"), ("bday", "birthday"), ("b-day", "birthday"), ("anniversary", "anniversary"),
             ("wedding", "wedding"), ("bridal", "wedding"), ("bride", "wedding"), ("engagement", "engagement"),
             ("graduation", "graduation"), ("farewell", "farewell"), ("hen", "hen"), ("bachelorette", "hen"),
             ("corporate", "corporate"), ("team", "corporate"), ("celebrat", "celebration")]
ASK_CUES = ["can ", "could ", "would ", "possible", "any ", "throw in", "include", "free", "complimentary", "on the house",
            "waive", "?", "please", "discount", "off", "instead", "how about", "what about"]


def _date(text: str, now: datetime) -> Optional[date]:
    t = text.lower()
    if re.search(r"\b(today|tonight|this evening)\b", t):
        return now.date()
    if re.search(r"\b(tomorrow|tmr|tmrw)\b", t):
        return now.date() + timedelta(days=1)
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t) or \
        re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        a, b = m.group(1), m.group(2)
        day, mon = (int(a), MONTHS[b[:3]]) if a.isdigit() else (int(b), MONTHS[a[:3]])
        d = date(now.year, mon, day)
        return d if d >= now.date() else date(now.year + 1, mon, day)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)
    if m:
        d = date(now.year, int(m.group(2)), int(m.group(1)))
        return d if d >= now.date() else date(now.year + 1, d.month, d.day)
    m = re.search(r"\b(this |next |coming )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thu|fri|sat|sun)\b", t)
    if m:
        target = WEEKDAYS[m.group(2)]
        delta = (target - now.weekday()) % 7
        if (m.group(1) or "").strip() == "next" and delta == 0:
            delta = 7
        return now.date() + timedelta(days=delta)
    if "this weekend" in t or "weekend" in t:
        return now.date() + timedelta(days=(5 - now.weekday()) % 7)
    return None


def _time(text: str) -> tuple[Optional[str], Optional[str]]:
    t = text.lower()
    start = end = None
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        start = f"{h:02d}:{m.group(2) or '00'}"
    else:
        m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
        if m:
            start = f"{int(m.group(1)):02d}:{m.group(2)}"
    if start is None:
        for word, hhmm in (("brunch", "10:00"), ("morning", "10:00"), ("lunch", "12:00"), ("afternoon", "14:00"),
                           ("sunset", "17:00"), ("evening", "17:00"), ("dinner", "19:00"), ("night", "19:00"), ("tonight", "19:00")):
            if re.search(rf"\b{word}\b", t):
                start = hhmm
                break
    m = re.search(r"(?:till|until|to|-)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", t)
    if start and m and m.group(3):
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        end = f"{h:02d}:{m.group(2) or '00'}"
    m = re.search(r"\b(\d{1,2})\s*(?:hours|hrs|hr|h)\b", t)
    if start and not end and m:
        h0, m0 = (int(x) for x in start.split(":"))
        h1 = (h0 + int(m.group(1))) % 24
        end = f"{h1:02d}:{m0:02d}"
    return start, end


def _party(text: str) -> Optional[int]:
    t = text.lower()
    for pat in (r"\b(\d{1,3})\s*(?:pax|people|persons|guests|ppl|adults|of us|ladies|guys|friends|pp)\b",
                r"\b(?:group|party|table|booking) (?:of|for) (\d{1,3})\b", r"\bwe(?:'re| are) (\d{1,3})\b",
                r"\bfor (\d{1,3}) (?:people|pax|guests|of us|ppl)\b"):
        m = re.search(pat, t)
        if m:
            return int(m.group(1))
    m = re.search(r"\b(" + "|".join(WORDNUM) + r")\s+(?:of us|people|pax|guests|friends)\b", t)
    if m:
        return WORDNUM[m.group(1)]
    return None


def _bottles(text: str) -> Optional[int]:
    t = text.lower()
    if not re.search(r"\b(bring\w*|brought|own|byo|carry\w*|corkage)\b", t):
        return None
    m = re.search(r"\b(\d{1,2})\s*(?:bottles?|btls?)\b", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(" + "|".join(WORDNUM) + r")\s+bottles?\b", t)
    return WORDNUM[m.group(1)] if m else None


def understand(profile: BusinessProfile, text: str, now: datetime, state: Optional[dict] = None) -> Understanding:
    t = text.lower().strip()
    u = Understanding()
    d = _date(t, now)
    u.date = d.isoformat() if d else None
    u.start_time, u.end_time = _time(t)
    u.party_size = _party(t)
    for word, occ in OCCASIONS:
        if re.search(rf"\b{word}", t):
            u.occasion = occ
            break
    u.byo_bottles = _bottles(t)
    m = re.search(r"\bmy name is (\w+)|\bthis is (\w+)\b|\bi'?m (\w+)\b", text, re.I)
    if m:
        name = next(g for g in m.groups() if g)
        if name.lower() not in ("looking", "planning", "trying", "wondering", "interested", "keen", "not", "ok", "okay", "fine"):
            u.customer_first_name = name.capitalize()

    # Seating named explicitly ("3 beds and the bean bag section").
    for res in profile.inventory:
        if res.kind != "unit":
            continue
        for alias in res.aliases or [res.label.lower()]:
            a = re.escape(alias.lower())
            m = re.search(rf"\b(\d{{1,2}}|{'|'.join(WORDNUM)})\s+(?:\w+\s+)?{a}", t)
            if m and res.units > 1:
                n = WORDNUM.get(m.group(1)) or int(m.group(1))
                u.seating.append(SeatRequest(resource_id=res.id, units=n))
                break
            if re.search(rf"\b(?:the |a |and |\+ ?){a}", t) and res.units == 1:
                u.seating.append(SeatRequest(resource_id=res.id, units=1))
                break
    if u.seating and all(profile.resource(s.resource_id).id in {p.resource_id for p in profile.perks if p.type == "package"}
                         for s in u.seating):
        u.seating = []  # only a package was named (e.g. "VIP"): that is a question, not a seating choice

    # Basket: "20 cases of house red", "2 colours", "4 cut and blow-dry".
    for prod in profile.products:
        names = prod.aliases or [w for w in re.findall(r"[a-z]+", prod.label.lower())
                                 if len(w) > 3 and w not in ("case", "bottle", "pint", "house")][-1:]
        for alias in names:
            m = re.search(rf"\b(\d{{1,3}}|{'|'.join(WORDNUM)})\s+(?:[\w-]+\s+){{0,3}}?{re.escape(alias.lower())}", t)
            if m:
                q = WORDNUM.get(m.group(1)) or int(m.group(1))
                u.basket.append(BasketItem(product_id=prod.id, qty=q))
                break

    # Intent.
    if re.search(r"\b(any way to call|call me|call us|can (?:i|we) call|give me a call|phone|ring me|speak to someone)\b", t):
        u.intent = "request_call"
    elif re.search(r"\bhow (?:do|can) (?:i|we) (?:confirm|book|pay)\b", t):
        u.intent, u.blocker = "ask_question", "process"
    elif re.search(r"\b(no thanks|we'?ll pass|not interested|forget it|maybe next time|book elsewhere|going elsewhere)\b", t):
        u.intent = "decline"
    elif re.search(r"\bcancel\b", t):
        u.intent = "cancel"
    elif re.search(r"\b(let'?s (?:do|book|go)|book it|we'?ll take|it'?s a deal|deal!|confirm(?:ed)?\b|sounds good|ok(?:ay)?,? (?:let'?s|book|go|we'?ll)|"
                   r"yes,? please|go ahead|i'?ll take|we'?re in|happy to (?:go|book|confirm)|ready to (?:book|pay|confirm)|lock it in)", t):
        u.intent = "accept"
    elif re.search(r"\b(let me (?:check|ask|think)|will (?:check|revert|get back)|ask my|think about it|get back to you)\b", t):
        u.intent = "thinking"

    # Blocker (first match wins, most specific first).
    if u.blocker == "none":
        if re.search(r"\bcorkage\b|cancellation fee|\bdeposit\b|service charge|\bfee\b", t):
            u.blocker = "fee"
        elif re.search(r"\b(rain\w*|weather|wet|indoor|shelter|what if)\b", t):
            u.blocker = "risk"
        elif re.search(r"\b(seat|seating|comfortable|together|layout|photos?|pictures?|see the|space|split|grandparents|elderly|privacy|private)\b", t):
            u.blocker = "fit"
        elif re.search(r"\b(earlier|later|extend|extra hour|full day|whole day|longer)\b", t):
            u.blocker = "timing"
        elif re.search(r"minimum spend|min spend|\blower\b|cheaper|discount|% ?off|too expensive|budget|best price|any deals?|"
                       r"promo|reduce|do better|better (?:price|deal|rate)|good (?:price|deal)|less\b|special price|deal on|group (?:discount|rate)", t):
            u.blocker = "price"
        elif u.occasion and re.search(r"\b(special|something|surprise|cake|decor|celebrate)\b", t):
            u.blocker = "occasion"
    if u.intent == "enquire" and u.blocker in ("price", "fee"):
        u.intent = "negotiate"
    if u.intent == "accept":
        u.blocker = "none"  # "happy with the minimum spend, ready to book" is not a price objection

    # Explicit asks mapped to the catalogue.
    m = re.search(r"(\d{1,2})\s*%", t)
    if m:
        u.requested_discount_pct = float(m.group(1))
        discounts = [p for p in profile.perks if p.type == "price_discount" and p.discount_pct is not None]
        if discounts:
            best = min(discounts, key=lambda p: abs(float(p.discount_pct) * 100 - float(m.group(1))))
            u.requested.append(ConcessionAsk(perk_id=best.id, qty=1))
    asking = any(c in t for c in ASK_CUES)
    for perk in profile.perks:
        if perk.type == "price_discount" and u.requested_discount_pct is not None:
            continue
        if perk.type == "package":
            if any(k in t for k in perk.mention_keywords()):
                for k in perk.mention_keywords():
                    for topic in ("vip",):
                        if topic in k and topic not in u.asks_about:
                            u.asks_about.append(topic)
                if perk.floor_basis == "requested" and any(k in t for k in perk.mention_keywords() if k != "vip"):
                    u.requested.append(ConcessionAsk(perk_id=perk.id, qty=1))
            continue
        if asking and any(k in t for k in perk.mention_keywords() if not k.startswith("%")):
            qty = 1
            if perk.type == "fee_waiver":
                mq = re.search(r"\ball (\d{1,2})\b|\b(\d{1,2}) bottles?\b|\ball (?:of )?(?:the |our )?bottles\b", t)
                if mq:
                    qty = int(next((g for g in mq.groups() if g), 0)) or (u.byo_bottles or perk.max_per_booking)
            if perk.cost_per == "guest":
                qty = 0
            u.requested.append(ConcessionAsk(perk_id=perk.id, qty=qty))
    if "corkage" in t and "waive" in t and not any(r.perk_id for r in u.requested if profile.has_perk(r.perk_id)
                                                     and profile.perk(r.perk_id).type == "fee_waiver"):
        for perk in profile.perks:
            if perk.type == "fee_waiver" and perk.fee_id and "cork" in perk.fee_id:
                u.requested.append(ConcessionAsk(perk_id=perk.id, qty=1))
    if any(r.perk_id in [p.id for p in profile.perks if "occasion" in p.blockers] for r in u.requested) and u.blocker == "none":
        u.blocker = "occasion"
    if any(r.perk_id in [p.id for p in profile.perks if p.type == "price_discount"] for r in u.requested):
        u.blocker = "price"
        if u.intent == "enquire":
            u.intent = "negotiate"

    for topic, pat in (("weather", r"\b(rain\w*|weather|wet)\b"), ("layout", r"\b(layout|photos?|seating|together)\b"),
                       ("pool", r"\bpool\b"), ("cancellation", r"\bcancel"), ("payment", r"\b(pay|payment|card)\b"),
                       ("children", r"\b(kids?|children|child)\b")):
        if re.search(pat, t) and topic not in u.asks_about:
            u.asks_about.append(topic)
    return u


def first_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return name.strip().split()[0][:30]
