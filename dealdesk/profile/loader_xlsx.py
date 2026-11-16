"""Business profile <-> Excel workbook.

Owners fill in a workbook instead of YAML. Layout:
  * key/value sheets (Business, Economics, Policy, Callbacks, Finance, Rollout, Channels, Adapters):
    columns Key | Value | Notes, keys dotted for nesting (e.g. reply_sla.staffed_minutes)
  * table sheets (Sessions, Inventory, Products, Fees, Perks, Staff, Placeholders):
    one row per item, headers dotted for nesting (e.g. floors.weekend_eve)
  * DealPolicy: the P(resell) matrix, columns lead_min_h, lead_max_h, util_min, util_max, p_resell
Lists and maps inside a cell use YAML flow syntax: [sat, sun] or {front_line: 7, manager: 10}.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from ..money import D
from .schema import BusinessProfile

KV_SHEETS = ["business", "economics", "policy", "callbacks", "finance", "rollout", "channels", "adapters"]
TABLE_SHEETS = ["sessions", "inventory", "products", "fees", "perks", "staff", "placeholders"]

README = [
    "DealDesk business profile workbook",
    "",
    "Fill in every sheet for your business. Each value is used by the deal engine:",
    "  Business: name, currency, hours, reply times, house rules customers are told before they commit.",
    "  Sessions: your time slots (days, start, end). 'late' means closing time.",
    "  Inventory: what you sell by the slot (beds, tables, rooms) or by the hour (stylists, crews).",
    "  Products: stocked items with price, unit cost, stock on hand and expected daily sales.",
    "  Economics: cost ratios and spend per guest (base and conservative cases).",
    "  Perks: the ONLY concessions the agent may offer, with true cost, eligibility and who may approve.",
    "  Policy: minimum contribution per deal, concession cap, authority limits, holds, close rates.",
    "  Finance: this month's P&L snapshot (optional) so the agent adapts to pace, costs and cash.",
    "  DealPolicy: chance unused capacity resells, by lead time and how full the slot is.",
    "",
    "Lists and maps in a cell use [a, b] and {key: value}. Leave a cell empty to use the default.",
    "Run:  dealdesk validate path/to/this.xlsx   to check the workbook before going live.",
]


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(_flatten(v, key))
            elif isinstance(v, (list, dict)):
                out[key] = yaml.safe_dump(v, default_flow_style=True, width=10_000).strip()
            else:
                out[key] = v
    return out


def _cell(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s.startswith(("[", "{")):
            return yaml.safe_load(s)
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        if s.lower() == "null":
            return None
        return v
    return v


def _unflatten(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for key, value in flat.items():
        if value is None or value == "":
            continue
        parts = str(key).split(".")
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = _cell(value)
    return out


def profile_dict_to_xlsx(data: dict, path: Path, matrix_rows: list[dict] | None = None) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    for line in README:
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=14)
    head = Font(bold=True)
    fill = PatternFill("solid", fgColor="DDEBF7")
    for name in KV_SHEETS:
        sec = data.get(name)
        if sec is None:
            continue
        s = wb.create_sheet(name.capitalize())
        s.append(["Key", "Value", "Notes"])
        for k, v in _flatten(sec).items():
            s.append([k, v, ""])
        for c in s[1]:
            c.font, c.fill = head, fill
        s.column_dimensions["A"].width = 38
        s.column_dimensions["B"].width = 60
    for name in TABLE_SHEETS:
        sec = data.get(name)
        if sec is None:
            continue
        rows = [{"id": k, **v} for k, v in sec.items()] if isinstance(sec, dict) else \
            ([{"note": x} for x in sec] if name == "placeholders" else list(sec))
        flat_rows = [_flatten(r) for r in rows]
        headers: list[str] = []
        for r in flat_rows:
            for k in r:
                if k not in headers:
                    headers.append(k)
        s = wb.create_sheet(name.capitalize())
        s.append(headers)
        for r in flat_rows:
            s.append([r.get(h) for h in headers])
        for c in s[1]:
            c.font, c.fill = head, fill
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width = 18
    if matrix_rows:
        s = wb.create_sheet("DealPolicy")
        cols = ["lead_min_h", "lead_max_h", "util_min", "util_max", "p_resell"]
        s.append(cols)
        for r in matrix_rows:
            s.append([("inf" if r[c] == Decimal("Infinity") else float(r[c])) for c in cols])
        for c in s[1]:
            c.font, c.fill = head, fill
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def xlsx_to_profile_dict(path: Path) -> tuple[dict, list[dict]]:
    wb = load_workbook(path, data_only=True)
    sheets = {s.title.lower(): s for s in wb.worksheets}
    data: dict = {}
    for name in KV_SHEETS:
        s = sheets.get(name)
        if s is None:
            continue
        flat = {}
        for row in s.iter_rows(min_row=2, values_only=True):
            if row and row[0] not in (None, ""):
                flat[str(row[0])] = row[1] if len(row) > 1 else None
        data[name] = _unflatten(flat)
    for name in TABLE_SHEETS:
        s = sheets.get(name)
        if s is None:
            continue
        rows = list(s.iter_rows(values_only=True))
        if not rows:
            continue
        headers = [str(h) if h is not None else "" for h in rows[0]]
        items = []
        for r in rows[1:]:
            if not any(v not in (None, "") for v in r):
                continue
            items.append(_unflatten({h: v for h, v in zip(headers, r) if h}))
        if name == "fees":
            data[name] = {it.pop("id"): it for it in items}
        elif name == "placeholders":
            data[name] = [it.get("note") for it in items if it.get("note")]
        else:
            data[name] = items
    matrix: list[dict] = []
    s = sheets.get("dealpolicy")
    if s is not None:
        rows = list(s.iter_rows(values_only=True))
        cols = [str(c) for c in rows[0]]
        for r in rows[1:]:
            if r and r[0] is not None:
                matrix.append({c: (Decimal("Infinity") if str(v).lower() == "inf" else D(str(v))) for c, v in zip(cols, r)})
    return data, matrix


def load_profile_xlsx(path: Path) -> BusinessProfile:
    data, matrix = xlsx_to_profile_dict(Path(path))
    if matrix:
        data.setdefault("policy", {}).pop("p_resell_matrix", None)
    profile = BusinessProfile.model_validate(data)
    if matrix:
        profile.resell_matrix = matrix
    return profile
