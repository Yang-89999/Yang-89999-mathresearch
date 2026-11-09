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
    units = list(units)[:MAX_UNITS_PER_SOURCE]
    ids = []
    for u in units:
        ids.append(store.insert(conn, "units", source_id=source_id, kind=u.kind, label=u.label, title=u.title,
                                statement=u.statement, anchor=u.anchor(), domains=domains or None,
                                proof_sketch=getattr(u, "proof", None), cites=getattr(u, "cites", None) or None))
    link_units(conn, units, ids)
    return ids


def link_units(conn: sqlite3.Connection, units: list[LatexUnit], ids: list[str]) -> int:
    """Resolve each unit's \\ref labels to the units that carry them (depends_on). Returns links made."""
    by_label: dict[str, str] = {}
    for u, uid in zip(units, ids):
        for lab in (getattr(u, "labels", None) or ([u.label] if u.label else [])):
            by_label.setdefault(lab, uid)
    links = 0
    for u, uid in zip(units, ids):
        deps = list(dict.fromkeys(by_label[r] for r in (getattr(u, "refs", None) or [])
                                  if r in by_label and by_label[r] != uid))
        if deps:
            store.update(conn, "units", uid, depends_on=deps)
            links += len(deps)
    return links


def _finish(conn: sqlite3.Connection, source_id: str, title: str, fmt: str, units: list[LatexUnit],
            domains: list[str] | None, note: str = "", bib: dict | None = None) -> IngestResult:
    ids = store_units(conn, source_id, units, domains)
    if bib:
        store.update(conn, "sources", source_id, bibliography=bib)
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
    if doc.bibliography:
        note += f"; {len(doc.bibliography)} bibliography entries"
    return _finish(conn, source_id, title, "latex", doc.units, domains, note, bib=doc.bibliography)


def ingest_text(conn: sqlite3.Connection, text: str, *, title: str, path: str | None = None, fmt: str = "text",
                authors: list[str] | None = None, year: int | None = None, kind: str = "notes",
                identifiers: dict | None = None, url: str | None = None, domains: list[str] | None = None,
                source_id: str | None = None) -> IngestResult:
    bib = None
    if fmt == "latex":
        doc = parse(text, file=path)
        units, bib = doc.units, doc.bibliography
    else:
        units = extract_units_from_text(text, file=path)
    if source_id is None:
        source_id = store.insert(conn, "sources", kind=kind, title=title, authors=authors or None, year=year,
                                 identifiers=identifiers or None, url=url, path=path, format=fmt,
                                 domains=domains or None)
    return _finish(conn, source_id, title, fmt, units, domains, bib=bib)


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
        return _finish(conn, source_id, title or doc.title or path.stem, "latex", doc.units, domains,
                       bib=doc.bibliography)
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


def attach_file(conn: sqlite3.Connection, source_id: str, path: Path,
                identifiers: dict | None = None) -> IngestResult:
    """Give an existing source its full text from a local file or LaTeX directory: its units are replaced by
    the ones parsed from ``path``; its id, identifiers, card, notes and seed question stay. Used when a paper
    was stored as abstract only (ePrint refuses automated PDF downloads) and the operator supplies the file."""
    src = store.get(conn, "sources", source_id)
    if src is None:
        raise ValueError(f"no source {source_id}")
    path = Path(path)
    ids = dict(src.get("identifiers") or {})
    ids.update(identifiers or {})
    conn.execute("DELETE FROM units WHERE source_id = ?", (source_id,))
    domains = src.get("domains") or None
    if path.is_dir():
        store.update(conn, "sources", source_id, identifiers=ids or None, path=str(path), format="latex")
        res = ingest_latex_dir(conn, path, title=src["title"], domains=domains, source_id=source_id)
    elif path.suffix.lower() == ".tex":
        doc = parse(path.read_text(encoding="utf-8", errors="replace"), read=_reader(path.parent), file=path.name)
        store.update(conn, "sources", source_id, identifiers=ids or None, path=str(path), format="latex")
        res = _finish(conn, source_id, src["title"], "latex", doc.units, domains, bib=doc.bibliography)
    else:
        if path.suffix.lower() == ".pdf":
            try:
                text, fmt = pdf_to_text(path), "pdf"
            except ImportError as exc:
                raise RuntimeError("PDF ingestion needs pypdf: pip install 'mathresearch[pdf]'") from exc
        else:
            text, fmt = path.read_text(encoding="utf-8", errors="replace"), "text"
        store.update(conn, "sources", source_id, identifiers=ids or None, path=str(path), format=fmt)
        res = ingest_text(conn, text, title=src["title"], path=str(path), fmt=fmt, domains=domains,
                          source_id=source_id)
    res.note = ((res.note + "; ") if res.note else "") + "full text attached to the existing source"
    return res


ARXIV_RECHECK_DAYS = 30


def attach_arxiv_copy(conn: sqlite3.Connection, source_id: str, workspace: Path, *,
                      fetch: ax.Fetch = ax.http_fetch, delay: float = 3.0) -> IngestResult | None:
    """Give an abstract-only source the full text of its arXiv version, when arXiv has one (matched by title
    and authors). Returns None when there is no arXiv copy or arXiv cannot be reached."""
    import datetime as _dt

    src = store.get(conn, "sources", source_id)
    if src is None:
        return None
    ids = dict(src.get("identifiers") or {})
    today = _dt.date.today()
    checked = ids.get("arxiv_checked")
    if checked and (today - _dt.date.fromisoformat(checked)).days < ARXIV_RECHECK_DAYS:
        return None   # looked recently and found none; arXiv postings of old ePrint papers are rare
    try:
        ax.polite_sleep(delay)
        hit = ax.find_arxiv_copy(src["title"], src.get("authors") or [], fetch=fetch, delay=delay)
        if hit is None:
            ids["arxiv_checked"] = today.isoformat()
            store.update(conn, "sources", source_id, identifiers=ids)
            return None
        ax.polite_sleep(delay)
        dest = workspace / "sources" / "arxiv" / hit.identifier.replace("/", "_")
        fmt = ax.download_arxiv_source(hit.identifier, dest, fetch=fetch)
    except Exception:  # an arXiv hiccup leaves the abstract in place; library fetch tries again later
        return None
    path = dest if fmt == "latex" else next(iter(sorted(dest.rglob("*.pdf"))), None)
    if path is None:
        return None
    res = attach_file(conn, source_id, path, identifiers={"arxiv": hit.identifier})
    res.note = f"full text from the arXiv version {hit.identifier}"
    return res


def abstract_only(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Sources stored with title and abstract only (ePrint refused the PDF) whose full text is not in the
    knowledge base under another source (such as the arXiv version): the operator can supply the file."""
    rows = store.select(conn, "sources", "format = 'html' AND identifiers LIKE '%\"eprint\"%'", order="created_at")
    return [r for r in rows if full_text_twin(conn, r) is None]


def full_text_twin(conn: sqlite3.Connection, src: dict[str, Any]) -> dict[str, Any] | None:
    """Another source holding the full text of the same paper (same title), e.g. the arXiv version an agent
    ingested while the ePrint record kept only the abstract."""
    want = ax.norm_title(src.get("title") or "")
    if len(want) < 16:
        return None
    for other in store.select(conn, "sources", "id != ? AND format IS NOT NULL AND format != 'html'", (src["id"],)):
        if ax.norm_title(other.get("title") or "") == want:
            return other
    return None


def eprint_pdf_url(eprint_id: str) -> str:
    return f"https://eprint.iacr.org/{eprint_id}.pdf"


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
        note = (f"{blocked}; stored title, authors and abstract only. To index the full text, open "
                f"{eprint_pdf_url(eprint_id)} in a browser, save the PDF, and run: "
                f"mathresearch ingest file <paper.pdf> --eprint {eprint_id}")
        res = _finish(conn, source_id, meta.title, "html", units, domains, note=note)
        return attach_arxiv_copy(conn, source_id, workspace, fetch=fetch, delay=delay) or res
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


# -- the logical structure of sources ingested before it was recorded -------------------

def relink(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, Any]:
    """Re-read the LaTeX of already-ingested sources and fill in what older ingestion did not record:
    depends_on (resolved \\ref links), cites, proofs, the bibliography. Units keep their ids (matched by
    file and line); nothing is deleted. Sources whose files are gone are skipped."""
    where, params = ("id = ?", (source_id,)) if source_id else ("format = 'latex'", ())
    out: dict[str, Any] = {"sources": 0, "units": 0, "links": 0, "skipped": []}
    for src in store.select(conn, "sources", where, params):
        path = Path(src["path"]) if src.get("path") else None
        if src.get("format") != "latex" or path is None or not path.exists():
            out["skipped"].append(src["id"])
            continue
        if path.is_dir():
            doc = parse_directory(path)
        else:
            doc = parse(path.read_text(encoding="utf-8", errors="replace"), read=_reader(path.parent),
                        file=path.name)
        if doc is None:
            out["skipped"].append(src["id"])
            continue
        existing = {((u.get("anchor") or {}).get("file"), (u.get("anchor") or {}).get("line")): u["id"]
                    for u in store.select(conn, "units", "source_id = ?", (src["id"],))}
        units, ids = [], []
        for u in doc.units:
            uid = existing.get((u.file, u.line))
            if uid is None:
                continue
            store.update(conn, "units", uid, anchor=u.anchor(), cites=u.cites or None,
                         proof_sketch=u.proof)
            units.append(u)
            ids.append(uid)
        out["links"] += link_units(conn, units, ids)
        if doc.bibliography:
            store.update(conn, "sources", src["id"], bibliography=doc.bibliography)
        out["sources"] += 1
        out["units"] += len(ids)
    return out
