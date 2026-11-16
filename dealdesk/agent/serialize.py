"""Candidate <-> JSON, so the ledger can store exactly what was offered and replay it."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from ..engine.types import Candidate, ConcessionLine, Line, Placement, PlacementPart, Terms


def cand_to_dict(c: Candidate) -> dict:
    pl = None
    if c.placement is not None:
        pl = {"start": c.placement.start.isoformat(), "end": c.placement.end.isoformat(), "label": c.placement.label,
              "session_type": c.placement.session_type,
              "parts": [{"resource_id": p.resource_id, "units": p.units, "unit_ids": p.unit_ids,
                         "hours": str(p.hours) if p.hours is not None else None} for p in c.placement.parts]}
    return {
        "offer_id": c.offer_id, "label": c.label, "kind": c.kind, "placement": pl, "pricing": c.pricing,
        "floor": str(c.floor) if c.floor is not None else None, "price": str(c.price) if c.price is not None else None,
        "basket": [{"product_id": l.product_id, "qty": l.qty, "unit_price": str(l.unit_price) if l.unit_price is not None else None}
                   for l in c.basket],
        "concessions": [{"perk_id": x.perk_id, "qty": x.qty} for x in c.concessions],
        "new_concessions": list(c.new_concessions),
        "terms": {"deposit": c.terms.deposit, "prepay": c.terms.prepay, "credit_days": c.terms.credit_days},
        "conditions": list(c.conditions), "p_class": c.p_class, "package_id": c.package_id, "facts": list(c.facts),
    }


def _dec(x: Any) -> Optional[Decimal]:
    return None if x in (None, "", "None") else Decimal(str(x))


def cand_from_dict(d: dict) -> Candidate:
    pl = None
    if d.get("placement"):
        p = d["placement"]
        pl = Placement([PlacementPart(x["resource_id"], int(x["units"]), x.get("unit_ids"), _dec(x.get("hours")))
                        for x in p["parts"]], datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]),
                       p.get("label", ""), p.get("session_type"))
    t = d.get("terms") or {}
    return Candidate(
        d["offer_id"], d["label"], d.get("kind", "standard"), pl, d.get("pricing", "minimum_spend"), _dec(d.get("floor")),
        _dec(d.get("price")), [Line(l["product_id"], int(l["qty"]), _dec(l.get("unit_price"))) for l in d.get("basket", [])],
        [ConcessionLine(x["perk_id"], int(x["qty"])) for x in d.get("concessions", [])], list(d.get("new_concessions", [])),
        Terms(bool(t.get("deposit")), bool(t.get("prepay")), int(t.get("credit_days", 0))), list(d.get("conditions", [])),
        d.get("p_class"), d.get("package_id"), list(d.get("facts", [])))
