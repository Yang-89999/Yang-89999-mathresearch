"""Papers the agents want: requests for full texts the system cannot fetch for itself.

IACR ePrint refuses automated PDF downloads, and textbooks and journal versions are paywalled, so an agent
that needs such a text asks for it (the ``request_paper`` tool) and keeps working from what it knows. The
operator sees the list (``library wanted``, the end of ``library fetch``), downloads what is worth it, and
the next ``library fetch`` or ``ingest file`` attaches it. A request is marked supplied once its source has
full text.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import ingest, store

MAX_WHYS = 5


def _link(reference: str | None) -> str | None:
    if not reference:
        return None
    kind = ingest.ax.classify(reference)
    if kind and kind[0] == "eprint":
        return ingest.eprint_pdf_url(kind[1])
    if kind and kind[0] == "arxiv":
        return ingest.ax.ARXIV_ABS.format(id=kind[1])
    ref = reference.strip()
    if ref.lower().startswith("10."):
        return f"https://doi.org/{ref}"
    if ref.lower().startswith(("http://", "https://")):
        return ref
    return None


def _full_text(src: dict[str, Any] | None) -> bool:
    return bool(src) and src.get("format") not in ("html", None)


def _by_title(conn: sqlite3.Connection, citation: str) -> dict[str, Any] | None:
    """The source whose title appears in a free-form citation (titles shorter than 16 characters never match)."""
    cited = ingest.ax.norm_title(citation)
    best = None
    for s in store.select(conn, "sources"):
        t = ingest.ax.norm_title(s["title"])
        if len(t) > 15 and t in cited and (best is None or _full_text(s) > _full_text(best)):
            best = s
    return best


def request(conn: sqlite3.Connection, citation: str, why: str, *, reference: str | None = None,
            question_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    """Record that an agent needs a paper. Returns {'id', 'times', 'status', 'source_id', 'new'}; status
    'available' means the knowledge base already holds its full text (nothing is recorded then)."""
    citation = " ".join(citation.split())[:400]
    reference = (reference or "").strip() or None
    source = None
    kind = ingest.ax.classify(reference) if reference else None
    if kind:
        source = ingest.find_source(conn, *kind)
        reference = kind[1] if kind[0] == "eprint" else f"arXiv:{kind[1]}"
    if source is None:
        source = _by_title(conn, citation)
    if _full_text(source):
        return {"id": None, "times": 0, "status": "available", "source_id": source["id"], "new": False}
    key = ingest.ax.norm_title(citation)
    existing = None
    for r in store.select(conn, "paper_requests", "status = 'wanted'"):
        if (source and r.get("source_id") == source["id"]) or (reference and r.get("reference") == reference) \
                or ingest.ax.norm_title(r["citation"]) == key:
            existing = r
            break
    why = " ".join((why or "").split())[:500]
    if existing:
        whys = (existing.get("whys") or [])
        if why and why not in whys:
            whys = (whys + [why])[-MAX_WHYS:]
        store.update(conn, "paper_requests", existing["id"], times=existing["times"] + 1, whys=whys,
                     reference=existing.get("reference") or reference,
                     source_id=existing.get("source_id") or (source["id"] if source else None))
        return {"id": existing["id"], "times": existing["times"] + 1, "status": "wanted",
                "source_id": existing.get("source_id") or (source["id"] if source else None), "new": False}
    rid = store.insert(conn, "paper_requests", citation=citation, reference=reference, whys=[why] if why else [],
                       source_id=source["id"] if source else None, question_id=question_id, run_id=run_id)
    return {"id": rid, "times": 1, "status": "wanted", "source_id": source["id"] if source else None, "new": True}


def refresh(conn: sqlite3.Connection) -> int:
    """Mark requests supplied once their source has full text. Returns how many were closed."""
    n = 0
    for r in store.select(conn, "paper_requests", "status = 'wanted'"):
        src = store.get(conn, "sources", r["source_id"]) if r.get("source_id") else None
        if src is None and r.get("reference"):
            kind = ingest.ax.classify(r["reference"])
            src = ingest.find_source(conn, *kind) if kind else None
        if src is None:
            src = _by_title(conn, r["citation"])
        if _full_text(src):
            store.update(conn, "paper_requests", r["id"], status="supplied", source_id=src["id"])
            n += 1
    return n


def open_requests(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    refresh(conn)
    rows = store.select(conn, "paper_requests", "status = 'wanted'", order="times DESC, created_at")
    for r in rows:
        r["link"] = _link(r.get("reference"))
    return rows


def dismiss(conn: sqlite3.Connection, request_id: str) -> bool:
    if store.get(conn, "paper_requests", request_id) is None:
        return False
    store.update(conn, "paper_requests", request_id, status="dismissed")
    return True


def render(r: dict[str, Any], workspace=None) -> str:
    asked = f"asked {r['times']}x" if r["times"] > 1 else "asked once"
    lines = [f"[{r['id']}] {r['citation']} ({asked})"]
    if r.get("link"):
        lines.append(f"  get it: {r['link']}")
    for w in (r.get("whys") or [])[-3:]:
        lines.append(f"  why: {w}")
    eprint = r.get("reference") if r.get("reference") and "/" in r["reference"] else None
    if eprint:
        lines.append(f"  then: save the PDF to Downloads or library/ and run 'library fetch' "
                     f"(or 'ingest file <pdf> --eprint {eprint}')")
    else:
        lines.append("  then: 'ingest file <pdf> --title \"...\"' (a textbook on the reading list: save it as "
                     "library/<id>.pdf)")
    return "\n".join(lines)
