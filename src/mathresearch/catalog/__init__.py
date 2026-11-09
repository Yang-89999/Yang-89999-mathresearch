"""Seed catalogs of known problems, attacks and weakness patterns, loaded into the database."""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from typing import Any

import yaml

from .. import store


def _load_yaml(name: str) -> list[dict[str, Any]]:
    text = resources.files("mathresearch.catalog").joinpath(name).read_text(encoding="utf-8")
    return yaml.safe_load(text) or []


_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def entry_year(row: dict[str, Any]) -> int | None:
    """Earliest year named in an entry's references, or None when it has none."""
    years = [int(y) for ref in (row.get("refs") or []) for y in _YEAR.findall(str(ref))]
    return min(years) if years else None


def _trim_refs(row: dict[str, Any], max_year: int | None) -> None:
    if max_year is None or not row.get("refs"):
        return
    row["refs"] = [r for r in row["refs"] if not any(int(y) > max_year for y in _YEAR.findall(str(r)))]


def load(conn: sqlite3.Connection, max_year: int | None = None,
         hidden_attacks: set[str] | frozenset[str] | None = None,
         always_keep: set[str] | None = None) -> dict[str, int]:
    """Upsert the seed catalogs. Returns counts of inserted rows per table.

    ``max_year`` drops entries whose earliest reference is later (retrodiction tests rebuild the
    knowledge of an earlier date); ``hidden_attacks`` drops attacks by implementation name and
    removes them from every problem's best_attacks list.
    """
    inserted = {"problems": 0, "attacks": 0, "weakness_patterns": 0}
    hidden = set(hidden_attacks or ())
    skipped_attack_ids = set()
    attack_rows = []
    for row in _load_yaml("attacks.yaml"):
        row = dict(row)
        year = entry_year(row)
        too_new = max_year is not None and year is not None and year > max_year and row["id"] not in (always_keep or ())
        if too_new or row.get("implementation") in hidden:
            skipped_attack_ids.add(row["id"])
            continue
        _trim_refs(row, max_year)
        attack_rows.append(row)
    for row in _load_yaml("problems.yaml"):
        row = dict(row)
        row.setdefault("status", "known")
        year = entry_year(row)
        if max_year is not None and year is not None and year > max_year and row["id"] not in (always_keep or ()):
            continue
        _trim_refs(row, max_year)
        if skipped_attack_ids and row.get("best_attacks"):
            row["best_attacks"] = [b for b in row["best_attacks"] if b.get("attack_id") not in skipped_attack_ids]
        if store.get(conn, "problems", row["id"]) is None:
            store.insert(conn, "problems", **row)
            inserted["problems"] += 1
        else:
            store.update(conn, "problems", row["id"], **{k: v for k, v in row.items() if k != "id"})
    for row in attack_rows:
        if store.get(conn, "attacks", row["id"]) is None:
            store.insert(conn, "attacks", **row)
            inserted["attacks"] += 1
        else:
            store.update(conn, "attacks", row["id"], **{k: v for k, v in row.items() if k != "id"})
    kept_attacks = {r["id"] for r in attack_rows}
    for row in _load_yaml("weakness_patterns.yaml"):
        row = dict(row)
        year = entry_year(row)
        if max_year is not None and year is not None and year > max_year and row["id"] not in (always_keep or ()):
            continue
        _trim_refs(row, max_year)
        row["exploited_by"] = [a for a in row.get("exploited_by") or [] if a in kept_attacks]
        if store.get(conn, "weakness_patterns", row["id"]) is None:
            store.insert(conn, "weakness_patterns", **row)
            inserted["weakness_patterns"] += 1
        else:
            store.update(conn, "weakness_patterns", row["id"], **{k: v for k, v in row.items() if k != "id"})
    return inserted
