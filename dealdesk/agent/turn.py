"""The agent: six steps every turn, plus the staff loop.

    0 Intake      code     dedupe, load the conversation record, start the reply clock
    1 Understand  model    enquiry fields, intent, blocker (rules fallback)
    2 Ground      code     calendar, holds, signals, prices, perk stock, P&L posture: re-read every turn
    3 Propose     code     placement first, then catalogue concessions aimed at the blocker
    4 Score       engine   revenue, contribution, opportunity cost, E, break-even, authority
    5 Decide      engine   keep / offer / alternative / escalate; side effects only when sendable
    6 Write       model    words from the brief; the checker compares every number; template fallback
    7 Gate        staff    review card in the staff chat until a decision class earns auto-send

Nothing is promised to a customer that the engine has not returned as sendable.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from ..engine.calendar_math import is_high_demand, overlapping_occurrences, session_type_at
from ..engine.evaluate import evaluate_offers
from ..engine.types import Candidate, ConcessionLine, DealContext, Figures, TurnDecision
from ..finance.posture import derive_posture
from ..ledger.store import Store
from ..money import D, fmt_money, whole
from ..policy.rules import callback_deadline, escalation_deadline, hold_expiry, in_hours, reply_sla_minutes
from ..profile.schema import DAYS, BusinessProfile
from ..staff.cards import callback_card, escalation_card, review_card
from . import rules_brain, writer
from .brief import OfferLine, ReplyBrief, escalation_packet, figures_table, offer_line, staff_rationale
from .candidates import Enquiry, build_candidates, repriced_standard, requested_placement
from .checker import check
from .models import Inbound, Outbound, TurnResult, Understanding
from .serialize import cand_from_dict, cand_to_dict

log = logging.getLogger(__name__)

DECISION_CLASS = {"keep_quote": "keep_quote", "offer_approved_concession": "offer_approved_concession",
                  "suggest_alternative": "suggest_alternative", "escalate": "escalate"}


class Agent:
    def __init__(self, profile: BusinessProfile, store: Store, brain=None, channel: str = "telegram"):
        self.profile = profile
        self.store = store
        self.brain = brain  # ClaudeBrain, or None for rules-only
        self.channel = channel
        self.posture = derive_posture(profile)

    # ================================================================== helpers
    @property
    def b(self):
        return self.profile.business

    def _ctx(self, enq: Enquiry, blocker: str, now: datetime, cid: Optional[str], tier: str = "front_line") -> DealContext:
        cal = self.store.calendar_view(self.b.id, now, exclude_conversation=cid)
        calibrated = self.store.calibrated(self.b.id)
        p_over = {k.split(".", 1)[1]: v for k, v in calibrated.items() if k.startswith("p_book.")}
        return DealContext(now=now, party_size=int(enq.party_size or 0), occasion=enq.occasion, byo_bottles=int(enq.byo_bottles or 0),
                           blocker=blocker, sender_tier=tier, calendar=cal, posture=self.posture, p_overrides=p_over)

    def _granted(self, cid: str) -> list[tuple[str, int]]:
        agg: dict[str, int] = {}
        for c in self.store.concessions(cid, ("offered", "accepted")):
            agg[c["perk_id"]] = max(agg.get(c["perk_id"], 0), int(c["qty"]))
        return list(agg.items())

    def _understand(self, text: str, now: datetime, state: dict, transcript: list[dict]) -> Understanding:
        if self.brain is not None:
            try:
                return self.brain.understand(self.profile, text, now, state, transcript)
            except Exception as e:  # model down, refusal, bad output: never block the customer
                log.warning("understand fell back to rules: %s", e)
        return rules_brain.understand(self.profile, text, now, state)

    def _write(self, brief: ReplyBrief, transcript: list[dict]) -> tuple[str, list[str]]:
        """Model draft -> checker -> one rewrite -> template. Returns (text, notes)."""
        notes: list[str] = []
        if self.brain is not None:
            feedback = None
            for attempt in range(2):
                try:
                    text = self.brain.write(self.profile, brief, self.channel, feedback, transcript)
                except Exception as e:
                    notes.append(f"model unavailable for wording ({e}); used template")
                    break
                res = check(self.profile, brief, text)
                if res.ok:
                    return text, notes
                feedback = res.feedback()
                notes.append(f"checker rejected draft {attempt + 1}: {feedback}")
        text = writer.write(brief)
        res = check(self.profile, brief, text)
        if not res.ok:  # should never happen; surfaced to staff rather than hidden
            notes.append(f"TEMPLATE FAILED CHECK: {res.feedback()}")
        return text, notes

    def _brief(self, kind: str, now: datetime, name: Optional[str], enq: Enquiry, **kw) -> ReplyBrief:
        return ReplyBrief(kind=kind, business=self.b.name, symbol=self.b.currency_symbol, tone=self.b.tone, now=now,
                          name=name, occasion=enq.occasion, party=enq.party_size, **kw)

    def _merge(self, state: dict, u: Understanding) -> Enquiry:
        enq = Enquiry.from_state(state.get("enquiry", {}))
        for f in ("date", "start_time", "end_time", "session_id", "party_size", "occasion", "offered_price", "deadline"):
            v = getattr(u, f)
            if v not in (None, ""):
                setattr(enq, f, v)
        if u.byo_bottles is not None:
            enq.byo_bottles = u.byo_bottles
        if u.seating:
            enq.seating = [s.model_dump() for s in u.seating if self._valid_resource(s.resource_id)]
        if u.basket:
            enq.basket = [x.model_dump() for x in u.basket if any(p.id == x.product_id for p in self.profile.products)]
        state["enquiry"] = asdict(enq)
        return enq

    def _valid_resource(self, rid: str) -> bool:
        return any(r.id == rid for r in self.profile.inventory)

    def _placement_high_demand(self, enq: Enquiry, cal) -> bool:
        pl = requested_placement(self.profile, enq)
        if pl is None:
            return False
        for part in pl.parts:
            res = self.profile.resource(part.resource_id)
            if res.kind == "unit":
                for occ in overlapping_occurrences(self.profile, res, pl.start, pl.end):
                    if is_high_demand(self.profile, cal, res, occ):
                        return True
        return False

    def _next_desk_open(self, now: datetime) -> Optional[datetime]:
        for d in range(0, 8):
            day = now + timedelta(days=d)
            span = self.b.hours_for(DAYS[day.weekday()], desk=True)
            if not span:
                continue
            h, m = (int(x) for x in span[0].split(":"))
            t = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                return t
        return None

    def _autosend(self, cls: str, out_of_hours: bool) -> bool:
        if out_of_hours and cls == "keep_quote" and self.profile.rollout.out_of_hours_autosend_keep_quote:
            return True
        override = self.store.get_setting(self.b.id, f"autosend.{cls}")
        if override is not None:
            return override == "1" and cls != "escalate"
        return bool(self.profile.rollout.autosend.get(cls)) and cls != "escalate"

    def _summary(self, enq: Enquiry) -> str:
        bits = []
        if enq.party_size:
            bits.append(f"{enq.party_size} pax")
        if enq.occasion:
            bits.append(enq.occasion)
        if enq.date:
            bits.append(f"{enq.date} {enq.start_time or enq.session_id or ''}".strip())
        if enq.byo_bottles:
            bits.append(f"{enq.byo_bottles} BYO bottles")
        if enq.basket:
            bits.append(", ".join(f"{b['qty']}x {b['product_id']}" for b in enq.basket))
        return " | ".join(bits) or "details not given yet"

    # ================================================================== customer turn
    def handle_customer(self, msg: Inbound) -> TurnResult:
        now = msg.at
        conv = self.store.conversation(self.b.id, msg.channel, msg.chat_ref, rules_brain.first_name(msg.sender_name), now)
        key = f"{conv.id}:in:{msg.message_id}"
        if not self.store.claim(key, now):  # 0. duplicate inbound: no new side effects
            return TurnResult(conv.id, "duplicate", duplicate=True, notes=["duplicate inbound ignored"])
        state = conv.state
        state["turn"] = turn = int(state.get("turn", 0)) + 1
        state.setdefault("first_inbound_at", now.isoformat())
        state["last_inbound_at"] = now.isoformat()
        self.store.add_transcript(conv.id, "customer", msg.text, now)
        transcript = self.store.transcript(conv.id)
        self.store.log(self.b.id, conv.id, "inbound", {"turn": turn}, now)

        u = self._understand(msg.text, now, state, transcript)  # 1
        enq = self._merge(state, u)
        name = u.customer_first_name or conv.customer_first_name
        if u.customer_first_name and not conv.customer_first_name:
            self.store.x("UPDATE conversations SET customer_first_name=? WHERE id=?", (u.customer_first_name, conv.id))
        blocker = u.blocker
        state.setdefault("blockers", []).append(blocker)
        # A customer who pushes back on price after hearing a package price has declined that package.
        last = self.store.last_sent_offer(conv.id)
        if last and blocker == "price":
            for alt in last["offer"].get("alternative_packages", []):
                state.setdefault("declined", []).append(alt)
        if last and last["offer"].get("candidate", {}).get("package_id") and blocker == "price":
            state.setdefault("declined", []).append(last["offer"]["candidate"]["package_id"])

        ctx_args = dict(msg=msg, conv=conv, state=state, turn=turn, enq=enq, u=u, name=name, blocker=blocker,
                        transcript=transcript, now=now)
        if u.intent in ("decline", "cancel"):
            result = self._close(**ctx_args)
        elif u.intent == "accept" and last is not None:
            result = self._accept(last=last, **ctx_args)
        elif u.intent == "thinking" and last is not None:
            result = self._thinking(last=last, **ctx_args)
        else:
            result = self._offer(**ctx_args)
        result.understanding = u
        self.store.save_state(conv.id, state, now)
        self.store.finish(key, {"decision": result.decision, "review_id": result.review_id})
        return result

    # ------------------------------------------------------------------ 3-6: the offer path
    def _offer(self, *, msg, conv, state, turn, enq, u, name, blocker, transcript, now) -> TurnResult:
        out_of_hours = not in_hours(self.profile, now)
        unit_based = any(r.kind == "unit" and r.pricing != "none" for r in self.profile.inventory)
        hours_based = any(r.kind == "hours" for r in self.profile.inventory)
        missing = []
        if unit_based:
            if not enq.date:
                missing.append("which date")
            if not (enq.start_time or enq.session_id):
                missing.append("what time")
            if not enq.party_size:
                missing.append("how many guests")
        elif hours_based:
            if not enq.date or not enq.start_time:
                missing.append("which day and time")
            if not enq.basket:
                missing.append("which services")
        elif not enq.basket:
            missing.append("what you'd like to order and how much")
        if missing:
            q = "Could you tell me " + (", ".join(missing[:-1]) + " and " + missing[-1] if len(missing) > 1 else missing[0]) + "?"
            brief = self._brief("need_details", now, name, enq, question=q)
            if out_of_hours and enq.party_size and self.b.online_cap_guests:
                brief.online_cap = self.b.online_cap_guests
            return self._finish(conv, state, turn, "need_details", brief, None, transcript, now, msg, hold_ids=[])
        if u.clarifying_question and blocker == "none" and u.intent in ("negotiate", "ask_question"):
            brief = self._brief("question", now, name, enq, question=u.clarifying_question)
            return self._finish(conv, state, turn, "question", brief, None, transcript, now, msg, hold_ids=[])

        ctx = self._ctx(enq, blocker, now, conv.id)  # 2. fresh facts, every turn
        hd = self._placement_high_demand(enq, ctx.calendar)
        requested = [(r.perk_id, r.qty) for r in u.requested if self.profile.has_perk(r.perk_id)]
        plan = build_candidates(self.profile, enq, blocker=blocker, requested=requested, asks_about=list(u.asks_about),
                                granted=self._granted(conv.id), declined=state.get("declined", []), cal=ctx.calendar,
                                now=now, high_demand_hint=hd)  # 3
        if plan.placement is None and (unit_based or hours_based):
            brief = self._brief("unavailable", now, name, enq,
                                unavailable_note="we're fully booked around that time.",
                                next_step="Is there another date that works for you?")
            return self._finish(conv, state, turn, "unavailable", brief, None, transcript, now, msg, hold_ids=[])

        td = evaluate_offers(self.profile, ctx, plan.candidates)  # 4 + 5
        std = td.standard
        if std is not None and std.decision == "not_offerable" and std.min_price is not None:
            # High-demand or below-minimum slot: the quote rises to the slot's minimum price.
            repriced = repriced_standard(std.candidate, std.min_price)
            cands = [repriced] + [c for c in plan.candidates if c is not std.candidate]
            for c in cands[1:]:
                if c.placement is std.candidate.placement and c.floor is None and c.pricing == repriced.pricing:
                    c.floor, c.price = repriced.floor, repriced.price if repriced.price is not None else c.price
            plan.candidates = cands
            td = evaluate_offers(self.profile, ctx, plan.candidates)
        state["last_candidates"] = {c.offer_id: cand_to_dict(c) for c in plan.candidates}

        granted_ids = {p for p, _ in self._granted(conv.id)}
        requested_ids = {p for p, _ in requested}
        if granted_ids:
            td = self._with_commitments(td, requested_ids)
        if td.decision == "escalate":
            return self._escalate(conv, state, turn, enq, name, blocker, td, transcript, now, msg)

        main_fig, alts = self._present(td)
        if main_fig is None:
            brief = self._brief("unavailable", now, name, enq, unavailable_note="that slot isn't available.",
                                next_step="Would another date or time work?")
            return self._finish(conv, state, turn, "unavailable", brief, td, transcript, now, msg, hold_ids=[])

        hold_ids, expiry, hold_problem = self._hold(conv.id, main_fig, now)
        if hold_problem:  # someone took it between the check and the hold: re-plan honestly
            return self._unavailable(conv, state, turn, enq, name, blocker, transcript, now, msg, hold_problem)

        declined_texts, declined_ids = self._declined(plan, td, requested)
        main = offer_line(self.profile, main_fig, enq.party_size, enq.byo_bottles or 0)
        brief = self._brief(
            "out_of_hours" if out_of_hours else ("alternative" if td.decision == "suggest_alternative" else
                                                 "offer" if main_fig.kind == "concession" else "keep"),
            now, name, enq, main=main, alternatives=[offer_line(self.profile, a, enq.party_size, enq.byo_bottles or 0) for a in alts],
            facts=list(main_fig.candidate.facts) if main_fig.kind == "clarify" else
            ([f for f in plan.facts] if blocker in ("fit", "risk", "process") else []),
            declined=declined_texts, declined_perk_ids=declined_ids, hold_expiry=expiry,
            scarcity_ok=False)
        if declined_texts and main.per_head and main_fig.candidate.pricing == "minimum_spend":
            brief.reframe = (f"The {fmt_money(main.price, self.b.currency_symbol)} minimum goes entirely on food and drinks, "
                             f"about {self.b.currency_symbol}{main.per_head} a head, so it isn't an extra charge.")
        elif declined_texts:
            brief.reframe = f"Here's the best I can do on {main.label.split(',')[0]}."
        if out_of_hours:
            brief.online_cap = self.b.online_cap_guests
            brief.price_list = self._price_list(td, enq)
            brief.desk_opens = self._next_desk_open(now)
            brief.next_step = "Reply here with your name to keep it, and we'll take it from there."
        elif u.intent == "request_call":
            pass
        else:
            brief.next_step = ("Reply 'confirm' and I'll send the link to lock it in." if main_fig.kind != "standard" or alts
                               else "Reply 'confirm' and I'll send you the link to book.")
        if u.intent == "accept" and not out_of_hours and not declined_texts and td.decision in ("keep_quote", "offer_approved_concession"):
            # Ready on the first message: skip straight to the confirmation step for the quote they accepted.
            ref = hold_ids[0] if hold_ids else f"{conv.id}:{turn}"
            brief.kind = "confirm_link"
            brief.confirm_link = f"{self.b.paylink_url}?ref={ref}" if self.b.paylink_url else (self.b.booking_url or "")
            brief.house_rules_before_commit = [self.b.house_rules[k] for k in ("card_on_file", "cancellation")
                                               if k in self.b.house_rules]
            brief.alternatives = []
            state["status"] = "awaiting_confirmation"
            return self._finish(conv, state, turn, "keep_quote", brief, td, transcript, now, msg, hold_ids=hold_ids,
                                chosen=main_fig, review_kind="confirm")
        if u.intent == "request_call":
            cb_name, cb_by, cb_out = self._callback(conv, enq, now)
            brief.kind, brief.callback_name, brief.callback_by = "callback", cb_name, cb_by
            res = self._finish(conv, state, turn, "callback", brief, td, transcript, now, msg, hold_ids=hold_ids,
                               chosen=main_fig, alt_packages=[a.candidate.package_id for a in alts if a.candidate.package_id])
            res.outbound.append(cb_out)
            return res
        return self._finish(conv, state, turn, td.decision if td.decision in DECISION_CLASS else "keep_quote", brief, td,
                            transcript, now, msg, hold_ids=hold_ids, chosen=main_fig,
                            alt_packages=[a.candidate.package_id for a in alts if a.candidate.package_id],
                            out_of_hours=out_of_hours)

    def _with_commitments(self, td: TurnDecision, requested_ids: set[str]) -> TurnDecision:
        """Concessions already offered are commitments: they stay on the table. A new ask is
        evaluated as 'what was given + the new ask' against the ORIGINAL standard quote; if it
        clears policy it is offered, otherwise the current offer is restated (never withdrawn)."""
        cur = next((f for f in td.ranked if f.offer_id == "CUR"), None)
        if cur is None:
            return td
        cur.sendable, cur.decision = True, "offer_approved_concession"
        e = lambda f: f.expected_contribution
        with_req = [f for f in td.ranked if set(f.candidate.new_concessions) & requested_ids]
        ok = [f for f in with_req if f.sendable]
        esc = [f for f in with_req if f.escalatable]
        if ok:
            best = max(ok, key=e)
            return TurnDecision("offer_approved_concession", best, td.standard, cur, td.ranked, f"{best.label}: {best.rule}")
        if esc:
            return TurnDecision("escalate", esc[0], td.standard, cur, td.ranked, f"{esc[0].label}: {esc[0].rule}; fallback current offer")
        if not requested_ids:
            better = [f for f in td.ranked if f.sendable and f.candidate.new_concessions and f.offer_id != "CUR" and e(f) > e(cur)]
            if better:
                return TurnDecision("offer_approved_concession", better[0], td.standard, cur, td.ranked, f"{better[0].label}: {better[0].rule}")
        reason = "; ".join(f"{f.label}: {f.rule}" for f in with_req) or "nothing new clears policy"
        return TurnDecision("keep_quote", cur, td.standard, cur, td.ranked, f"current offer stands ({reason})")

    def _present(self, td: TurnDecision) -> tuple[Optional[Figures], list[Figures]]:
        """Give the customer a choice: the best sendable offer on the requested placement, plus a
        sendable alternative placement or package when it is worth more."""
        sendable = [f for f in td.ranked if f.sendable]
        if not sendable:
            return None, []
        on_requested = [f for f in sendable if f.kind != "alternative"]
        alts = [f for f in sendable if f.kind == "alternative"]
        main = on_requested[0] if on_requested else alts.pop(0)
        if td.chosen is not None and td.chosen.kind != "alternative" and td.chosen.sendable:
            main = td.chosen
        if td.chosen is not None and td.chosen.offer_id == "CUR" or (main.kind == "standard" and
                                                                      any(f.offer_id == "CUR" for f in sendable)):
            main = next(f for f in td.ranked if f.offer_id == "CUR")
        better = [a for a in alts if a.expected_contribution > (td.standard.expected_contribution if td.standard else 0)]
        if td.decision == "suggest_alternative" or (main.kind == "standard" and better):
            return main, better[:1]
        return main, [a for a in better if a.candidate.package_id][:1] if td.decision != "offer_approved_concession" else []

    def _declined(self, plan, td: TurnDecision, requested: list[tuple[str, int]]) -> tuple[list[str], list[str]]:
        texts, ids = [], []
        by_new = {}
        for f in td.ranked:
            for pid in f.candidate.new_concessions:
                by_new.setdefault(pid, []).append(f)
        for pid, _ in requested:
            figs = by_new.get(pid, [])
            if pid in plan.declined_requests or (figs and not any(f.sendable for f in figs)) or not figs:
                if self.profile.has_perk(pid):
                    p = self.profile.perk(pid)
                    if p.type == "package":
                        continue
                    texts.append(p.customer_text or p.label)
                    ids.append(pid)
        return texts, ids

    def _price_list(self, td: TurnDecision, enq: Enquiry) -> list[str]:
        sym = self.b.currency_symbol
        seen, out = set(), []
        for f in td.ranked:
            c = f.candidate
            if c.placement is None or f.kind not in ("standard", "alternative") or c.concessions:
                continue
            if any(self.profile.resource(p.resource_id).kind != "unit" for p in c.placement.parts):
                continue
            what = c.placement.label.split(",")[0]
            if what in seen:
                continue
            seen.add(what)
            price = f"{fmt_money(f.price_quoted, sym)} minimum spend" if c.pricing == "minimum_spend" else fmt_money(f.price_quoted, sym)
            out.append(f"{what}: {price}")
        return out[:3]

    # ------------------------------------------------------------------ holds
    def _hold(self, cid: str, fig: Figures, now: datetime) -> tuple[list[str], Optional[datetime], str]:
        cand = fig.candidate
        if cand.placement is None or not any(self.profile.resource(p.resource_id).kind == "unit" for p in cand.placement.parts):
            return [], None, ""
        expiry = hold_expiry(self.profile, now, cand.placement.start, fig.high_demand)
        if expiry is None:
            return [], None, ""
        cal = self.store.calendar_view(self.b.id, now, exclude_conversation=cid)
        from ..engine.calendar_math import pick_units
        ids: list[str] = []
        key = f"{cid}:hold:{cand.placement.key()}"
        existing = [h for h in self.store.active_holds(cid, now) if h["idem_key"] == key]
        if existing:  # same placement already held: never extend silently, keep the original expiry
            return [h["id"] for h in existing], datetime.fromisoformat(existing[0]["expires_at"]), ""
        self.store.release_holds(cid, "superseded")  # one active hold per customer per session
        for part in cand.placement.parts:
            res = self.profile.resource(part.resource_id)
            if res.kind != "unit":
                continue
            units, why = pick_units(cal, res, cand.placement.start, cand.placement.end, part.units, part.unit_ids)
            if units is None:
                self.store.release_hold_ids(ids, "failed")
                return [], None, why
            for unit in units:
                hid, why = self.store.place_hold(cid, self.b.id, res.id, unit, cand.placement.start, cand.placement.end,
                                                 expiry, key, now)
                if hid is None:
                    self.store.release_hold_ids(ids, "failed")
                    return [], None, why
                ids.append(hid)
        self.store.log(self.b.id, cid, "hold_placed", {"holds": ids, "expires_at": expiry}, now)
        return ids, expiry, ""

    # ------------------------------------------------------------------ escalation
    def _escalate(self, conv, state, turn, enq, name, blocker, td: TurnDecision, transcript, now, msg) -> TurnResult:
        start = td.chosen.candidate.placement.start if td.chosen.candidate.placement else None
        deadline = escalation_deadline(self.profile, now, start)
        packet = escalation_packet(self.profile, td, blocker, deadline)
        packet["candidates"] = {f.offer_id: cand_to_dict(f.candidate) for f in td.ranked}
        packet["enquiry"] = state.get("enquiry")
        eid = self.store.escalate(conv.id, packet, deadline, f"{conv.id}:esc:{turn}", now)
        state["escalation_id"] = eid
        hold_ids, _, _ = self._hold(conv.id, td.fallback or td.chosen, now) if (td.fallback or td.chosen) else ([], None, "")
        self.store.log(self.b.id, conv.id, "escalated", {"escalation_id": eid, "deadline": deadline,
                                                         "chosen": td.chosen.offer_id, "rule": td.chosen.rule}, now)
        brief = self._brief("escalate_holding", now, name, enq, escalation_back_by=deadline)
        res = self._finish(conv, state, turn, "escalate", brief, td, transcript, now, msg, hold_ids=hold_ids, chosen=None)
        res.escalation_id = eid
        text, buttons = escalation_card(eid, self.b.name, name or conv.chat_ref, packet, self.b.currency_symbol, deadline)
        res.outbound.append(Outbound("staff", None, text, buttons))
        return res

    # ------------------------------------------------------------------ callbacks
    def _callback(self, conv, enq: Enquiry, now: datetime) -> tuple[str, datetime, Outbound]:
        staff = [s for s in self.profile.staff if s.on_duty] or self.profile.staff
        person = next((s for s in staff if s.tier == "front_line"), staff[0] if staff else None)
        name = person.name if person else "Our team"
        pl = requested_placement(self.profile, enq)
        deadline = callback_deadline(self.profile, now, pl.start if pl else None)
        cb = self.store.assign_callback(conv.id, name, deadline, f"{conv.id}:cb:{now:%Y%m%d%H%M}", now)
        self.store.log(self.b.id, conv.id, "callback_assigned", {"callback_id": cb, "staff": name, "deadline": deadline}, now)
        card = callback_card(cb, self.b.name, conv.customer_first_name or conv.chat_ref, name, deadline, conv.chat_ref,
                             self._summary(enq))
        return name, deadline, Outbound("staff", None, card)

    # ------------------------------------------------------------------ accept: re-check, hold, link
    def _accept(self, *, last, msg, conv, state, turn, enq, u, name, blocker, transcript, now) -> TurnResult:
        cand = cand_from_dict(last["offer"]["candidate"])
        ctx = self._ctx(enq, "none", now, conv.id)
        std = Candidate("STD", "Standard quote", "standard", cand.placement, pricing=cand.pricing, price=cand.price,
                        basket=list(cand.basket), floor=cand.floor if cand.kind == "standard" else None)
        td = evaluate_offers(self.profile, ctx, [std, cand] if cand.kind != "standard" else [cand])
        fig = next((f for f in td.ranked if f.offer_id == cand.offer_id), None)
        if fig is None or not fig.available:
            return self._unavailable(conv, state, turn, enq, name, blocker, transcript, now, msg,
                                     fig.unavailable_reason if fig else "it's no longer available")
        if not (fig.sendable or fig.decision in ("keep_quote", "offer_approved_concession", "suggest_alternative")
                or last["decision"] == "approved_by_manager"):
            return self._offer(msg=msg, conv=conv, state=state, turn=turn, enq=enq, u=u, name=name, blocker=blocker,
                               transcript=transcript, now=now)
        hold_ids, expiry, problem = self._hold(conv.id, fig, now)
        if problem:
            return self._unavailable(conv, state, turn, enq, name, blocker, transcript, now, msg, problem)
        ref = hold_ids[0] if hold_ids else f"{conv.id}:{turn}"
        link = f"{self.b.paylink_url}?ref={ref}" if self.b.paylink_url else (self.b.booking_url or "")
        rules = [self.b.house_rules[k] for k in ("card_on_file", "cancellation") if k in self.b.house_rules]
        main = offer_line(self.profile, fig, enq.party_size, enq.byo_bottles or 0)
        brief = self._brief("confirm_link", now, name, enq, main=main, hold_expiry=expiry, confirm_link=link,
                            house_rules_before_commit=rules)
        self.store.mark_offer(last["id"], "accepted")
        for c in self.store.concessions(conv.id, ("offered",)):
            self.store.x("UPDATE concessions SET status='accepted' WHERE id=?", (c["id"],))
        state["status"] = "awaiting_confirmation"
        self.store.log(self.b.id, conv.id, "accepted", {"offer_id": cand.offer_id, "hold_ids": hold_ids}, now)
        return self._finish(conv, state, turn, "keep_quote", brief, td, transcript, now, msg, hold_ids=hold_ids, chosen=fig,
                            review_kind="confirm")

    def _unavailable(self, conv, state, turn, enq, name, blocker, transcript, now, msg, why: str) -> TurnResult:
        ctx = self._ctx(enq, blocker, now, conv.id)
        plan = build_candidates(self.profile, enq, blocker=blocker, requested=[], asks_about=[], granted=self._granted(conv.id),
                                declined=state.get("declined", []), cal=ctx.calendar, now=now)
        main_fig = None
        td = None
        hold_ids, expiry = [], None
        if plan.candidates:
            td = evaluate_offers(self.profile, ctx, plan.candidates)
            main_fig, _ = self._present(td)
            if main_fig is not None:
                hold_ids, expiry, problem = self._hold(conv.id, main_fig, now)
                if problem:
                    main_fig = None
        note = "that time was just taken while we were talking."  # honest, without other guests' details
        brief = self._brief("unavailable", now, name, enq, unavailable_note=note,
                            main=offer_line(self.profile, main_fig, enq.party_size, enq.byo_bottles or 0) if main_fig else None,
                            hold_expiry=expiry if main_fig else None,
                            next_step="Reply 'confirm' if that works and I'll send the link." if main_fig else
                            "Is there another date or time that works?")
        return self._finish(conv, state, turn, "keep_quote" if main_fig else "unavailable", brief, td, transcript, now, msg,
                            hold_ids=hold_ids, chosen=main_fig, review_kind="unavailable")

    def _thinking(self, *, last, msg, conv, state, turn, enq, u, name, blocker, transcript, now) -> TurnResult:
        holds = self.store.active_holds(conv.id, now)
        expiry = datetime.fromisoformat(holds[0]["expires_at"]) if holds else None
        fig_main = None
        cand = cand_from_dict(last["offer"]["candidate"])
        line = OfferLine(cand.placement.label if cand.placement else cand.label, "", D(0)) if cand.placement else None
        brief = self._brief("thinking", now, name, enq, main=line, hold_expiry=expiry,
                            next_step="Just reply here when you're ready.")
        return self._finish(conv, state, turn, "thinking", brief, None, transcript, now, msg, hold_ids=[h["id"] for h in holds])

    def _close(self, *, msg, conv, state, turn, enq, u, name, blocker, transcript, now) -> TurnResult:
        self.store.release_holds(conv.id, "released")
        state["status"] = "closed"
        self.store.record_outcome(conv.id, self.b.id, False, now, blocker=blocker)
        self.store.log(self.b.id, conv.id, "lost", {"blocker": blocker}, now)
        brief = self._brief("decline_close", now, name, enq, next_step="If your plans change, just message us here.")
        return self._finish(conv, state, turn, "close", brief, None, transcript, now, msg, hold_ids=[])

    # ------------------------------------------------------------------ 6 + 7: write, record, gate
    def _finish(self, conv, state, turn, decision: str, brief: ReplyBrief, td: Optional[TurnDecision], transcript, now,
                msg: Inbound, hold_ids: list[str], chosen: Optional[Figures] = None, alt_packages: Optional[list] = None,
                out_of_hours: bool = False, review_kind: Optional[str] = None, urgent: bool = False) -> TurnResult:
        text, notes = self._write(brief, transcript)
        figures = [f.to_schema() for f in td.ranked] if td else []
        # Record the offer BEFORE anything is sent (append-only ledger).
        if chosen is not None:
            offer = {"figures": chosen.to_schema(), "candidate": cand_to_dict(chosen.candidate),
                     "alternative_packages": alt_packages or [], "price_label": brief.main.price_label if brief.main else None}
            self.store.record_offer(conv.id, turn, chosen.offer_id, decision, chosen.label, offer, chosen.conditions,
                                    brief.hold_expiry, "drafted", f"{conv.id}:offer:{turn}", now)
            for pid in chosen.candidate.new_concessions:
                qty = next((c.qty for c in chosen.candidate.concessions if c.perk_id == pid), 1)
                self.store.add_concession(conv.id, pid, qty, chosen.concession_cost, turn, "drafted",
                                          f"{conv.id}:conc:{turn}:{pid}", now)
        state["offers_summary"] = [f"{o['label']} ({o['status']})" for o in self.store.offers(conv.id)][-5:]
        rationale = staff_rationale(self.profile, td, brief_blocker(state)) if td else f"1. Blocker: {brief_blocker(state)}"
        cls = DECISION_CLASS.get(decision, "keep_quote" if decision in ("thinking", "need_details", "question", "close",
                                                                         "callback", "unavailable") else decision)
        inbound_at = datetime.fromisoformat(state.get("last_inbound_at", now.isoformat()))
        auto = self._autosend(cls, out_of_hours) and not notes_has_failure(notes)
        rid = self.store.add_review(conv.id, turn, review_kind or decision, text, {"ranked": figures}, rationale, hold_ids,
                                    f"{conv.id}:review:{turn}", now, inbound_at, status="auto_sent" if auto else "pending")
        self.store.log(self.b.id, conv.id, "drafted", {"review_id": rid, "decision": decision, "class": cls, "notes": notes,
                                                       "chosen": chosen.offer_id if chosen else None,
                                                       "p_class": chosen.assumptions.get("p_class") if chosen else None,
                                                       "concession_cost": str(chosen.concession_cost) if chosen else "0"}, now)
        res = TurnResult(conv.id, decision, draft=text, review_id=rid, hold_ids=hold_ids, figures=figures, rationale=rationale,
                         notes=notes)
        if auto:
            res.autosent = True
            res.outbound.append(Outbound("customer", conv.chat_ref, text))
            self._mark_sent(conv.id, turn, text, now, "auto")
        else:
            sla = reply_sla_minutes(self.profile, inbound_at)
            hold_note = ""
            if hold_ids:
                hold_note = f"Hold: {', '.join(hold_ids)} until {brief.hold_expiry:%a %H:%M}" if brief.hold_expiry else f"Hold: {', '.join(hold_ids)}"
            card, buttons = review_card(rid, self.b.name, brief.name or conv.chat_ref, self._summary(Enquiry.from_state(state.get("enquiry"))),
                                        review_kind or decision, text, rationale,
                                        figures_table(self.profile, td) if td else "", hold_note,
                                        f"Reply due by {(inbound_at + timedelta(minutes=sla)):%H:%M} ({sla} min SLA)"
                                        + ("" if not notes else " | " + " ; ".join(notes)), urgent)
            res.outbound.append(Outbound("staff", None, card, buttons, rid))
        return res

    def _mark_sent(self, cid: str, turn: int, text: str, now: datetime, by: str) -> None:
        for o in self.store.offers(cid):
            if o["turn"] == turn and o["status"] == "drafted":
                self.store.mark_offer(o["id"], "sent")
        self.store.x("UPDATE concessions SET status='offered' WHERE conversation_id=? AND turn=? AND status='drafted'", (cid, turn))
        self.store.add_transcript(cid, "agent", text, now)
        conv = self.store.get_conversation(cid)
        if conv and not conv.state.get("first_reply_at"):
            conv.state["first_reply_at"] = now.isoformat()
            self.store.save_state(cid, conv.state, now)
        self.store.log(self.b.id, cid, "sent", {"turn": turn, "by": by}, now)

    # ================================================================== staff actions
    def staff_send(self, rid: int, staff: str, now: datetime) -> list[Outbound]:
        r = self.store.review(rid)
        if r is None or not self.store.decide_review(rid, "sent", staff, now, final_text=r["draft"]):
            return [Outbound("staff", None, f"Review #{rid} was already handled.")]
        self._mark_sent(r["conversation_id"], r["turn"], r["draft"], now, staff)
        conv = self.store.get_conversation(r["conversation_id"])
        self.store.log(self.b.id, conv.id, "review_decided", {"review_id": rid, "action": "sent", "staff": staff,
                                                             "class": r["decision_class"]}, now)
        return [Outbound("customer", conv.chat_ref, r["draft"]),
                Outbound("staff", None, f"Sent #{rid} by {staff}.")]

    def staff_edit(self, rid: int, staff: str, new_text: str, now: datetime) -> list[Outbound]:
        r = self.store.review(rid)
        if r is None or not self.store.decide_review(rid, "edited", staff, now, final_text=new_text):
            return [Outbound("staff", None, f"Review #{rid} was already handled.")]
        self._mark_sent(r["conversation_id"], r["turn"], new_text, now, staff)
        conv = self.store.get_conversation(r["conversation_id"])
        self.store.log(self.b.id, conv.id, "review_decided", {"review_id": rid, "action": "edited", "staff": staff,
                                                             "class": r["decision_class"]}, now)
        return [Outbound("customer", conv.chat_ref, new_text), Outbound("staff", None, f"Edited and sent #{rid} by {staff}.")]

    def staff_reject(self, rid: int, staff: str, reason: str, now: datetime) -> list[Outbound]:
        r = self.store.review(rid)
        if r is None or not self.store.decide_review(rid, "rejected", staff, now, reason=reason):
            return [Outbound("staff", None, f"Review #{rid} was already handled.")]
        self.store.release_hold_ids(r["hold_ids"], "rejected")
        for o in self.store.offers(r["conversation_id"]):
            if o["turn"] == r["turn"] and o["status"] == "drafted":
                self.store.mark_offer(o["id"], "rejected")
        self.store.set_concession_status(r["conversation_id"], r["turn"], "withdrawn")
        self.store.log(self.b.id, r["conversation_id"], "review_decided", {"review_id": rid, "action": "rejected",
                                                                          "staff": staff, "reason": reason,
                                                                          "class": r["decision_class"]}, now)
        return [Outbound("staff", None, f"Rejected #{rid} ({reason}). Nothing was sent; holds released. "
                                        "Reply to the customer yourself or wait for their next message.")]

    def decide_escalation(self, eid: int, approve: bool, staff: str, staff_tier: str, now: datetime,
                          timed_out: bool = False) -> list[Outbound]:
        esc = self.store.escalation(eid)
        if esc is None:
            return [Outbound("staff", None, f"Escalation #{eid} not found.")]
        packet = esc["packet"]
        need = packet.get("authority_required", "manager")
        from ..profile.schema import TIER_ORDER
        if approve and TIER_ORDER.get(staff_tier, 0) < TIER_ORDER.get(need, 1):
            return [Outbound("staff", None, f"{staff} cannot approve #{eid}: needs {need}.")]
        status = "timed_out" if timed_out else ("approved" if approve else "declined")
        if not self.store.decide_escalation(eid, status, staff, now):
            return [Outbound("staff", None, f"Escalation #{eid} was already decided.")]
        conv = self.store.get_conversation(esc["conversation_id"])
        state = conv.state
        enq = Enquiry.from_state(state.get("enquiry"))
        cands = {k: cand_from_dict(v) for k, v in packet["candidates"].items()}
        pick = packet["chosen_offer_id"] if approve else packet.get("fallback_offer_id")
        tier = staff_tier if approve else "front_line"
        ctx = self._ctx(enq, packet.get("blocker", "none"), now, conv.id, tier=tier)
        std = next((c for c in cands.values() if c.kind == "standard"), None)
        lineup = [c for c in [std, cands.get(pick)] if c is not None]
        td = evaluate_offers(self.profile, ctx, lineup)
        fig = next((f for f in td.ranked if pick and f.offer_id == pick and (f.sendable or approve)), None) or td.standard
        if fig is None or not fig.available:
            return [Outbound("staff", None, f"Escalation #{eid}: the offer is no longer available; ask the customer for another slot.")]
        turn = int(state.get("turn", 0)) + 1
        state["turn"] = turn
        hold_ids, expiry, problem = self._hold(conv.id, fig, now)
        main = offer_line(self.profile, fig, enq.party_size, enq.byo_bottles or 0)
        brief = self._brief("offer" if fig.kind != "standard" else "keep", now, conv.customer_first_name, enq, main=main,
                            hold_expiry=expiry, next_step="Reply 'confirm' and I'll send the link to lock it in.")
        self.store.log(self.b.id, conv.id, "escalation_decided", {"escalation_id": eid, "status": status, "staff": staff,
                                                                 "offer": fig.offer_id}, now)
        transcript = self.store.transcript(conv.id)
        if approve or not timed_out:
            # A manager has seen this decision: it goes straight to the customer.
            text, notes = self._write(brief, transcript)
            offer = {"figures": fig.to_schema(), "candidate": cand_to_dict(fig.candidate), "alternative_packages": [],
                     "price_label": main.price_label}
            self.store.record_offer(conv.id, turn, fig.offer_id, "approved_by_manager" if approve else fig.decision, fig.label,
                                    offer, fig.conditions, expiry, "drafted", f"{conv.id}:offer:{turn}", now)
            for pid in fig.candidate.new_concessions:
                qty = next((c.qty for c in fig.candidate.concessions if c.perk_id == pid), 1)
                self.store.add_concession(conv.id, pid, qty, fig.concession_cost, turn, "drafted", f"{conv.id}:conc:{turn}:{pid}", now)
            self.store.add_review(conv.id, turn, "escalation_" + status, text, {"ranked": [fig.to_schema()]}, "", hold_ids,
                                  f"{conv.id}:review:{turn}", now, now, status="sent")
            self._mark_sent(conv.id, turn, text, now, staff)
            self.store.save_state(conv.id, state, now)
            return [Outbound("customer", conv.chat_ref, text),
                    Outbound("staff", None, f"Escalation #{eid} {status} by {staff}; customer told: {fig.label}.")]
        # Timed out: fall back to the best approved offer rather than silence (still reviewed unless auto-send is on).
        res = self._finish(conv, state, turn, fig.decision if fig.decision in DECISION_CLASS else "keep_quote", brief, td,
                           transcript, now, Inbound(self.channel, conv.chat_ref, f"timeout-{eid}", "", now),
                           hold_ids=hold_ids, chosen=fig, review_kind="fallback", urgent=True)
        self.store.save_state(conv.id, state, now)
        return res.outbound


def brief_blocker(state: dict) -> str:
    bl = [b for b in state.get("blockers", []) if b != "none"]
    return bl[-1] if bl else "none"


def notes_has_failure(notes: list[str]) -> bool:
    return any(n.startswith("TEMPLATE FAILED") for n in notes)
