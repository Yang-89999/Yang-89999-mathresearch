"""Typed access to the research database.

Thin functions over the tables in ``schema.sql``. JSON-valued columns take and
return Python objects. Nothing here talks to a model.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .ids import new_id

_JSON_COLUMNS = {
    "sources": {"authors", "identifiers", "domains"},
    "units": {"hypotheses", "notation", "depends_on", "anchor", "domains"},
    "problems": {"domains", "structure", "reductions", "best_attacks", "refs"},
    "attacks": {"applies_to", "requires", "refs"},
    "questions": {"domains"},
    "experiments": {"params", "result", "tool_versions"},
    "findings": set(),
    "runs": {"context_ids", "result"},
}

_ID_KIND = {
    "sources": "source",
    "units": "unit",
    "problems": "problem",
    "attacks": "attack",
    "questions": "question",
    "experiments": "experiment",
    "findings": "finding",
    "runs": "run",
}


def _encode(table: str, row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if k in _JSON_COLUMNS[table] and v is not None and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, sort_keys=True)
        out[k] = v
    return out


def _decode(table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for k in _JSON_COLUMNS[table]:
        if out.get(k) is not None:
            try:
                out[k] = json.loads(out[k])
            except (TypeError, ValueError):
                pass
    return out


def insert(conn: sqlite3.Connection, table: str, **row: Any) -> str:
    """Insert a row, minting an id when none is given. Returns the id."""
    row.setdefault("id", new_id(_ID_KIND[table]))
    row = _encode(table, row)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    return row["id"]


def update(conn: sqlite3.Connection, table: str, id_: str, **changes: Any) -> None:
    if not changes:
        return
    changes = _encode(table, changes)
    if table in ("problems", "questions"):
        changes.setdefault("updated_at", _now(conn))
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), id_))


def get(conn: sqlite3.Connection, table: str, id_: str) -> dict[str, Any] | None:
    return _decode(table, conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone())


def select(conn: sqlite3.Connection, table: str, where: str = "1=1", params: Iterable[Any] = (),
           order: str = "created_at", limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [_decode(table, r) for r in conn.execute(sql, tuple(params)).fetchall()]


def search_units(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """BM25 full-text search over knowledge units."""
    rows = conn.execute(
        "SELECT u.*, bm25(units_fts) AS score FROM units_fts "
        "JOIN units u ON u.rowid = units_fts.rowid "
        "WHERE units_fts MATCH ? ORDER BY score LIMIT ?",
        (query, limit),
    ).fetchall()
    return [_decode("units", r) for r in rows]


# -- agenda helpers -----------------------------------------------------------

def open_questions(conn: sqlite3.Connection, lane: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where = "status = 'open'"
    params: list[Any] = []
    if lane:
        where += " AND lane = ?"
        params.append(lane)
    return select(conn, "questions", where, params, order="priority DESC, created_at", limit=limit)


def block_on(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    """Mark ``parent_id`` blocked until ``child_id`` closes."""
    update(conn, "questions", parent_id, status="blocked")
    update(conn, "questions", child_id, parent_id=parent_id)


def close_question(conn: sqlite3.Connection, id_: str, status: str = "closed") -> None:
    """Close a question and unblock its parent if no other open children remain."""
    update(conn, "questions", id_, status=status, closed_at=_now(conn))
    q = get(conn, "questions", id_)
    parent = q and q.get("parent_id")
    if parent:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM questions WHERE parent_id = ? AND status IN ('open','blocked','running')",
            (parent,),
        ).fetchone()["n"]
        if remaining == 0:
            p = get(conn, "questions", parent)
            if p and p["status"] == "blocked":
                update(conn, "questions", parent, status="open")


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS t").fetchone()["t"]
