"""The reading list: references the system fetches for itself, and textbooks the operator supplies.

``catalog/reading_list.yaml`` names surveys, foundational papers and lecture notes with open-access
arXiv or ePrint ids (fetched by ``library fetch``) and the standard textbooks of the charter's domains
(paywalled: the operator places a PDF, ``.tex`` file or LaTeX directory at ``<workspace>/library/<id>``
and the next ``library fetch`` ingests it). Every item ingested this way is an operator reference, so it
gets a seed read.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml

from . import ingest, reading, store

LIBRARY_DIR = "library"
FILE_SUFFIXES = (".pdf", ".tex", ".txt", ".md")


def entries(topic: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    text = resources.files("mathresearch.catalog").joinpath("reading_list.yaml").read_text(encoding="utf-8")
    rows = yaml.safe_load(text) or []
    if topic:
        rows = [r for r in rows if topic in (r.get("topics") or [])]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows


def topics() -> list[str]:
    seen: dict[str, None] = {}
    for r in entries():
        for t in r.get("topics") or []:
            seen.setdefault(t, None)
    return list(seen)


def local_file(workspace: Path, entry_id: str) -> Path | None:
    """The operator's copy of a textbook, if placed under <workspace>/library/."""
    base = workspace / LIBRARY_DIR
    if (base / entry_id).is_dir():
        return base / entry_id
    for suffix in FILE_SUFFIXES:
        p = base / f"{entry_id}{suffix}"
        if p.is_file():
            return p
    return None


def find_ingested(conn: sqlite3.Connection, entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("access") in ("arxiv", "eprint"):
        kind = ingest.ax.classify(entry["ref"])
        return ingest.find_source(conn, *kind) if kind else None
    return ingest.find_source(conn, "library", entry["id"])


def status(conn: sqlite3.Connection, workspace: Path, topic: str | None = None) -> list[dict[str, Any]]:
    out = []
    for e in entries(topic):
        src = find_ingested(conn, e)
        if src:
            state = "ingested"
        elif e.get("access") == "file":
            state = "file present" if local_file(workspace, e["id"]) else "waiting for file"
        else:
            state = "to fetch"
        out.append({**e, "state": state, "source_id": src["id"] if src else None})
    return out


def fetch(conn: sqlite3.Connection, workspace: Path, *, topic: str | None = None, seed: bool = True,
          delay: float = 3.0, fetch_fn: ingest.ax.Fetch = ingest.ax.http_fetch,
          report: Callable[[str], None] = print) -> dict[str, list[str]]:
    """Ingest every open-access entry and every textbook the operator has placed; queue seed reads."""
    result: dict[str, list[str]] = {"ingested": [], "already": [], "waiting": [], "failed": []}
    first = True
    for e in entries(topic):
        if find_ingested(conn, e):
            result["already"].append(e["id"])
            continue
        domains = e.get("domains") or None
        try:
            if e.get("access") in ("arxiv", "eprint"):
                if not first:
                    ingest.ax.polite_sleep(delay)
                first = False
                res = ingest.ingest_reference(conn, e["ref"], workspace, domains=domains, fetch=fetch_fn, delay=delay)
            else:
                path = local_file(workspace, e["id"])
                if path is None:
                    result["waiting"].append(e["id"])
                    continue
                res = ingest.ingest_file(conn, path, title=e["title"], kind="book" if e.get("kind") == "book" else "notes",
                                         authors=e.get("authors"), year=e.get("year"), domains=domains)
                src = store.get(conn, "sources", res.source_id)
                ids = dict(src.get("identifiers") or {})
                ids["library"] = e["id"]
                store.update(conn, "sources", res.source_id, identifiers=ids, url=e.get("url"))
        except Exception as exc:  # one failure must not stop the list
            result["failed"].append(f"{e['id']}: {type(exc).__name__}: {exc}")
            report(f"{e['id']}: {type(exc).__name__}: {exc}")
            continue
        store.update(conn, "sources", res.source_id, provided_by="operator")
        line = res.summary()
        if seed:
            qid = reading.seed_reference(conn, res.source_id)
            line += f"\n  seed read queued: {qid}"
        report(line)
        result["ingested"].append(e["id"])
    return result


def render_entry(e: dict[str, Any], state: str | None = None, workspace: Path | None = None) -> str:
    authors = ", ".join(a.split()[-1] for a in e.get("authors") or [])
    head = f"[{e['id']}] {e['title']} ({authors}, {e.get('year')}) {e.get('kind')}"
    if state:
        head += f" -- {state}"
    lines = [head, f"  topics: {', '.join(e.get('topics') or [])}", f"  why: {e.get('why', '').strip()}"]
    if e.get("access") in ("arxiv", "eprint"):
        lines.append(f"  fetch: {e['ref']}")
    else:
        where = f"{workspace / LIBRARY_DIR}/{e['id']}.pdf" if workspace else f"<workspace>/{LIBRARY_DIR}/{e['id']}.pdf"
        lines.append(f"  file: place it at {where}" + (f" (free: {e['url']})" if e.get("url") else ""))
    return "\n".join(lines)
