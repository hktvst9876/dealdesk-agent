"""Load a business profile from YAML (or the Excel template, see loader_xlsx)."""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
from typing import Union

import yaml

from ..money import D
from .schema import BusinessProfile

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


def _read_matrix(path: Path) -> list[dict]:
    """Deal Policy matrix: lead time x forecast utilisation -> P(resell).

    Long format, one row per cell:
        lead_min_h,lead_max_h,util_min,util_max,p_resell
    Upper bounds are exclusive; 'inf' is allowed.
    """
    rows = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            row = {k: D(v.strip()) if v.strip().lower() != "inf" else Decimal("Infinity") for k, v in r.items() if k}
            if not (0 <= row["p_resell"] <= 1):
                raise ValueError(f"{path.name}: p_resell {row['p_resell']} is not a probability")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: empty P(resell) matrix")
    return rows


def resolve_profile_path(name_or_path: Union[str, Path]) -> Path:
    p = Path(name_or_path)
    if p.exists():
        return p
    for cand in (PROFILES_DIR / f"{name_or_path}.yaml", PROFILES_DIR / f"{name_or_path}.xlsx", PROFILES_DIR / str(name_or_path)):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"no profile '{name_or_path}' (looked in {PROFILES_DIR})")


def load_profile_dict(data: dict, base_dir: Path | None = None) -> BusinessProfile:
    profile = BusinessProfile.model_validate(data)
    matrix = profile.policy.p_resell_matrix
    if matrix:
        mpath = Path(matrix)
        if not mpath.is_absolute() and base_dir is not None:
            mpath = base_dir / mpath
        profile.resell_matrix = _read_matrix(mpath)
    return profile


def load_profile(name_or_path: Union[str, Path]) -> BusinessProfile:
    path = resolve_profile_path(name_or_path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from .loader_xlsx import load_profile_xlsx

        return load_profile_xlsx(path)
    data = yaml.safe_load(path.read_text())
    return load_profile_dict(data, base_dir=path.parent)
