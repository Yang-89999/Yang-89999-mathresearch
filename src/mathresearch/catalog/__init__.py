"""Seed catalogs of known problems and attacks, loaded into the database."""

from __future__ import annotations

import sqlite3
from importlib import resources
from typing import Any

import yaml

from .. import store


def _load_yaml(name: str) -> list[dict[str, Any]]:
    text = resources.files("mathresearch.catalog").joinpath(name).read_text(encoding="utf-8")
    return yaml.safe_load(text) or []


def load(conn: sqlite3.Connection) -> dict[str, int]:
    """Upsert the seed catalogs. Returns counts of inserted rows per table."""
    inserted = {"problems": 0, "attacks": 0}
    for row in _load_yaml("problems.yaml"):
        row = dict(row)
        row.setdefault("status", "known")
        if store.get(conn, "problems", row["id"]) is None:
            store.insert(conn, "problems", **row)
            inserted["problems"] += 1
        else:
            store.update(conn, "problems", row["id"], **{k: v for k, v in row.items() if k != "id"})
    for row in _load_yaml("attacks.yaml"):
        row = dict(row)
        if store.get(conn, "attacks", row["id"]) is None:
            store.insert(conn, "attacks", **row)
            inserted["attacks"] += 1
        else:
            store.update(conn, "attacks", row["id"], **{k: v for k, v in row.items() if k != "id"})
    return inserted
