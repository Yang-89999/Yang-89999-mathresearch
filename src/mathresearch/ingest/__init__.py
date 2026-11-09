"""Literature ingestion: turn papers into sources and knowledge units.

    ingest_reference(conn, "2301.00001" | "arXiv:..." | "2024/123" | url, workspace)
    ingest_file(conn, path, ...)          .tex, .txt/.md, or .pdf (pypdf)
    search(query, ...)                    arXiv search for 'learn' questions

Everything downloaded lands under ``workspace/sources/<service>/<id>/`` so a
unit's anchor (file, line) can always be opened again.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import store
from . import arxiv as ax
from .latex import LatexUnit, parse, parse_directory
from .text import extract_units_from_text, pdf_to_text

MAX_UNITS_PER_SOURCE = 400


@dataclass
class IngestResult:
    source_id: str
    title: str
    format: str
    units: int
    kinds: dict[str, int] = field(default_factory=dict)
    unit_ids: list[str] = field(default_factory=list)
    already_present: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "title": self.title, "format": self.format, "units": self.units,
                "kinds": dict(self.kinds), "already_present": self.already_present, "note": self.note,
                "sample_unit_ids": self.unit_ids[:8]}

    def summary(self) -> str:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(self.kinds.items()))
        state = "already ingested" if self.already_present else "ingested"
        extra = f" ({self.note})" if self.note else ""
        return f"{state} source {self.source_id} '{self.title}' [{self.format}]: {self.units} units ({kinds}){extra}"


# -- lookup ----------------------------------------------------------------------

def find_source(conn: sqlite3.Connection, service: str, identifier: str) -> dict[str, Any] | None:
    rows = store.select(conn, "sources", "identifiers LIKE ?", (f'%"{service}": "{identifier}"%',), limit=1)
    return rows[0] if rows else None


def _reset_source(conn: sqlite3.Connection, service: str, identifier: str) -> str | None:
    """For a forced re-ingest: drop the existing source's units and return its id to reuse."""
    existing = find_source(conn, service, identifier)
    if not existing:
        return None
    conn.execute("DELETE FROM units WHERE source_id = ?", (existing["id"],))
    return existing["id"]


def _upsert_source(conn: sqlite3.Connection, source_id: str | None, **row: Any) -> str:
    if source_id is None:
        return store.insert(conn, "sources", **row)
    store.update(conn, "sources", source_id, **row)
    return source_id


def _existing_result(conn: sqlite3.Connection, src: dict[str, Any]) -> IngestResult:
    units = store.select(conn, "units", "source_id = ?", (src["id"],), order="rowid")
    kinds = Counter(u["kind"] for u in units)
    return IngestResult(source_id=src["id"], title=src["title"], format=src.get("format") or "?",
                        units=len(units), kinds=dict(kinds), unit_ids=[u["id"] for u in units], already_present=True)


# -- storing ---------------------------------------------------------------------

def store_units(conn: sqlite3.Connection, source_id: str, units: Iterable[LatexUnit],
                domains: list[str] | None) -> list[str]:
    ids = []
    for u in list(units)[:MAX_UNITS_PER_SOURCE]:
        ids.append(store.insert(conn, "units", source_id=source_id, kind=u.kind, label=u.label, title=u.title,
                                statement=u.statement, anchor=u.anchor(), domains=domains or None))
    return ids


def _finish(conn: sqlite3.Connection, source_id: str, title: str, fmt: str, units: list[LatexUnit],
            domains: list[str] | None, note: str = "") -> IngestResult:
    ids = store_units(conn, source_id, units, domains)
    store.update(conn, "sources", source_id, ingested_at=store._now(conn))
    kinds = Counter(u.kind for u in units[:MAX_UNITS_PER_SOURCE])
    if len(units) > MAX_UNITS_PER_SOURCE:
        note = (note + "; " if note else "") + f"kept the first {MAX_UNITS_PER_SOURCE} of {len(units)} units"
    return IngestResult(source_id=source_id, title=title, format=fmt, units=len(ids), kinds=dict(kinds),
                        unit_ids=ids, note=note)


# -- local files ---------------------------------------------------------------

def ingest_latex_dir(conn: sqlite3.Connection, root: Path, *, title: str | None = None,
                     authors: list[str] | None = None, year: int | None = None, kind: str = "paper",
                     identifiers: dict | None = None, url: str | None = None,
                     domains: list[str] | None = None, source_id: str | None = None) -> IngestResult:
    doc = parse_directory(root)
    if doc is None:
        raise ValueError(f"no .tex files under {root}")
    title = title or doc.title or root.name
    authors = authors or doc.authors
    if source_id is None:
        source_id = store.insert(conn, "sources", kind=kind, title=title, authors=authors or None, year=year,
                                 identifiers=identifiers or None, url=url, path=str(root), format="latex",
                                 domains=domains or None)
    note = f"{len(doc.environments)} theorem-like environments recognised"
    return _finish(conn, source_id, title, "latex", doc.units, domains, note)


def ingest_text(conn: sqlite3.Connection, text: str, *, title: str, path: str | None = None, fmt: str = "text",
                authors: list[str] | None = None, year: int | None = None, kind: str = "notes",
                identifiers: dict | None = None, url: str | None = None, domains: list[str] | None = None,
                source_id: str | None = None) -> IngestResult:
    if fmt == "latex":
        units = parse(text, file=path).units
    else:
        units = extract_units_from_text(text, file=path)
    if source_id is None:
        source_id = store.insert(conn, "sources", kind=kind, title=title, authors=authors or None, year=year,
                                 identifiers=identifiers or None, url=url, path=path, format=fmt,
                                 domains=domains or None)
    return _finish(conn, source_id, title, fmt, units, domains)


def ingest_file(conn: sqlite3.Connection, path: Path, *, title: str | None = None, kind: str = "paper",
                authors: list[str] | None = None, year: int | None = None,
                domains: list[str] | None = None) -> IngestResult:
    path = Path(path)
    if path.is_dir():
        return ingest_latex_dir(conn, path, title=title, authors=authors, year=year, kind=kind, domains=domains)
    suffix = path.suffix.lower()
    if suffix == ".tex":
        doc = parse(path.read_text(encoding="utf-8", errors="replace"),
                    read=_reader(path.parent), file=path.name)
        source_id = store.insert(conn, "sources", kind=kind, title=title or doc.title or path.stem,
                                 authors=authors or doc.authors or None, year=year, path=str(path),
                                 format="latex", domains=domains or None)
        return _finish(conn, source_id, title or doc.title or path.stem, "latex", doc.units, domains)
    if suffix == ".pdf":
        try:
            text = pdf_to_text(path)
        except ImportError as exc:
            raise RuntimeError("PDF ingestion needs pypdf: pip install 'mathresearch[pdf]'") from exc
        return ingest_text(conn, text, title=title or path.stem, path=str(path), fmt="pdf", authors=authors,
                           year=year, kind=kind, domains=domains)
    text = path.read_text(encoding="utf-8", errors="replace")
    return ingest_text(conn, text, title=title or path.stem, path=str(path), fmt="text", authors=authors,
                       year=year, kind=kind, domains=domains)


def _reader(root: Path):
    from .latex import file_reader
    return file_reader(root)


# -- remote ----------------------------------------------------------------------

def ingest_arxiv(conn: sqlite3.Connection, ref: str, workspace: Path, *, domains: list[str] | None = None,
                 fetch: ax.Fetch = ax.http_fetch, delay: float = 3.0, force: bool = False) -> IngestResult:
    arxiv_id = ax.normalize_arxiv_id(ref)
    if not arxiv_id:
        raise ValueError(f"not an arXiv identifier: {ref!r}")
    reuse_id = None
    if force:
        reuse_id = _reset_source(conn, "arxiv", arxiv_id)
    else:
        existing = find_source(conn, "arxiv", arxiv_id)
        if existing:
            return _existing_result(conn, existing)
    meta = ax.arxiv_metadata(arxiv_id, fetch=fetch)
    ax.polite_sleep(delay)
    dest = workspace / "sources" / "arxiv" / arxiv_id.replace("/", "_")
    fmt = ax.download_arxiv_source(arxiv_id, dest, fetch=fetch)
    title = meta.title if meta else f"arXiv:{arxiv_id}"
    authors = meta.authors if meta else []
    year = meta.year if meta else None
    identifiers = meta.identifiers() if meta else {"arxiv": arxiv_id}
    url = ax.ARXIV_ABS.format(id=arxiv_id)
    source_id = _upsert_source(conn, reuse_id, kind="paper", title=title, authors=authors or None, year=year,
                               identifiers=identifiers, url=url, path=str(dest), format=fmt,
                               venue=", ".join(meta.categories[:3]) if meta else None, domains=domains or None,
                               abstract=(meta.summary or None) if meta else None)
    if fmt == "latex":
        return ingest_latex_dir(conn, dest, title=title, domains=domains, source_id=source_id)
    if fmt == "pdf":
        pdfs = sorted(dest.rglob("*.pdf"))
        try:
            text = pdf_to_text(pdfs[0])
        except ImportError:
            text = meta.summary if meta else ""
            return ingest_text(conn, text, title=title, path=str(pdfs[0]), fmt="pdf", domains=domains,
                               source_id=source_id)
        return ingest_text(conn, text, title=title, path=str(pdfs[0]), fmt="pdf", domains=domains,
                           source_id=source_id)
    # no usable payload: at least keep the abstract as a remark unit
    text = meta.summary if meta else ""
    units = [LatexUnit(kind="remark", env="abstract", statement=text, title="Abstract", file=None)] if text else []
    return _finish(conn, source_id, title, fmt, units, domains, note="no LaTeX or PDF payload; abstract only")


def ingest_eprint(conn: sqlite3.Connection, ref: str, workspace: Path, *, domains: list[str] | None = None,
                  fetch: ax.Fetch = ax.http_fetch, delay: float = 1.0, force: bool = False) -> IngestResult:
    eprint_id = ax.normalize_eprint_id(ref)
    if not eprint_id:
        raise ValueError(f"not an IACR ePrint identifier: {ref!r}")
    reuse_id = None
    if force:
        reuse_id = _reset_source(conn, "eprint", eprint_id)
    else:
        existing = find_source(conn, "eprint", eprint_id)
        if existing:
            return _existing_result(conn, existing)
    meta = ax.eprint_metadata(eprint_id, fetch=fetch)
    ax.polite_sleep(delay)
    dest = workspace / "sources" / "eprint" / eprint_id.replace("/", "_")
    try:
        pdf: Path | None = ax.download_eprint_pdf(eprint_id, dest, fetch=fetch)
    except ax.BlockedError as exc:
        pdf, blocked = None, str(exc)
    else:
        blocked = ""
    source_id = _upsert_source(conn, reuse_id, kind="paper", title=meta.title, authors=meta.authors or None,
                               year=meta.year, identifiers=meta.identifiers(), url=meta.url,
                               path=str(pdf) if pdf else None, format="pdf" if pdf else "html",
                               venue="IACR ePrint", domains=domains or None, abstract=meta.summary or None)
    if pdf is None:
        units = [LatexUnit(kind="remark", env="abstract", statement=meta.summary, title="Abstract")] \
            if meta.summary else []
        note = (f"{blocked}; stored title, authors and abstract only. To index the full text, download the PDF "
                f"in a browser and run: mathresearch ingest file <paper.pdf> --title \"{meta.title[:60]}\"")
        return _finish(conn, source_id, meta.title, "html", units, domains, note=note)
    try:
        text = pdf_to_text(pdf)
    except ImportError:
        return _finish(conn, source_id, meta.title, "pdf", [], domains,
                       note="pypdf not installed; PDF stored but no units extracted")
    return ingest_text(conn, text, title=meta.title, path=str(pdf), fmt="pdf", domains=domains, source_id=source_id)


def ingest_reference(conn: sqlite3.Connection, ref: str, workspace: Path, *, domains: list[str] | None = None,
                     fetch: ax.Fetch = ax.http_fetch, delay: float = 3.0, force: bool = False) -> IngestResult:
    kind = ax.classify(ref)
    if kind is None:
        raise ValueError(f"cannot recognise {ref!r} as an arXiv id/URL or an IACR ePrint id/URL")
    service, identifier = kind
    if service == "arxiv":
        return ingest_arxiv(conn, identifier, workspace, domains=domains, fetch=fetch, delay=delay, force=force)
    return ingest_eprint(conn, identifier, workspace, domains=domains, fetch=fetch, delay=min(delay, 1.0),
                         force=force)


def search(conn: sqlite3.Connection, query: str, max_results: int = 10,
           fetch: ax.Fetch = ax.http_fetch) -> list[dict[str, Any]]:
    """arXiv search annotated with whether each hit is already in the knowledge base."""
    out = []
    for m in ax.arxiv_search(query, max_results=max_results, fetch=fetch):
        existing = find_source(conn, "arxiv", m.identifier)
        out.append({"arxiv_id": m.identifier, "title": m.title, "authors": m.authors[:6], "year": m.year,
                    "categories": m.categories[:3], "summary": m.summary[:400],
                    "ingested_source_id": existing["id"] if existing else None})
    return out


__all__ = ["IngestResult", "find_source", "ingest_arxiv", "ingest_eprint", "ingest_file", "ingest_latex_dir",
           "ingest_reference", "ingest_text", "search", "store_units"]
