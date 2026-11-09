"""The reading list: references the system fetches for itself, and textbooks the operator supplies.

``catalog/reading_list.yaml`` names surveys, foundational papers and lecture notes with open-access
arXiv or ePrint ids (fetched by ``library fetch``) and the standard textbooks of the charter's domains
(paywalled: the operator places a PDF, ``.tex`` file or LaTeX directory at ``<workspace>/library/<id>``
and the next ``library fetch`` ingests it). Every item ingested this way is an operator reference, so it
gets a seed read.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
import unicodedata
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml

from . import ingest, reading, store

LIBRARY_DIR = "library"
TITLE_WINDOW = 400           # the title must start within this many letters of the first page
DOWNLOAD_MAX_AGE_DAYS = 120   # Downloads PDFs older than this are not opened to look for a title
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


def _names(entry: dict[str, Any] | str) -> list[str]:
    """File names the operator may use for an entry: its id, and for ePrint papers 2015_939 / 2015-939."""
    if isinstance(entry, str):
        return [entry]
    names = [entry["id"]]
    eprint = entry.get("eprint") or (entry.get("ref") if entry.get("access") == "eprint" else None)
    if eprint:
        names += [eprint.replace("/", "_"), eprint.replace("/", "-")]
    return names


def local_file(workspace: Path, entry: dict[str, Any] | str) -> Path | None:
    """The operator's copy of an entry, if placed under <workspace>/library/ (a textbook, or the PDF of an ePrint
    paper that ePrint would not let us download)."""
    base = workspace / LIBRARY_DIR
    for name in _names(entry):
        if (base / name).is_dir():
            return base / name
        for suffix in FILE_SUFFIXES:
            p = base / f"{name}{suffix}"
            if p.is_file():
                return p
    return None


def find_ingested(conn: sqlite3.Connection, entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("access") in ("arxiv", "eprint"):
        kind = ingest.ax.classify(entry["ref"])
        found = ingest.find_source(conn, *kind) if kind else None
        if found is None and entry.get("eprint"):  # stored earlier under its ePrint id
            found = ingest.find_source(conn, "eprint", entry["eprint"])
        return found
    return ingest.find_source(conn, "library", entry["id"])


def _units(conn: sqlite3.Connection, source_id: str) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM units WHERE source_id = ?", (source_id,)).fetchone()["n"]


def _abstract_only(src: dict[str, Any] | None) -> bool:
    return bool(src) and src.get("format") == "html"


def status(conn: sqlite3.Connection, workspace: Path, topic: str | None = None) -> list[dict[str, Any]]:
    out = []
    for e in entries(topic):
        src = find_ingested(conn, e)
        if src and _abstract_only(src):
            state = "abstract only; PDF file present" if local_file(workspace, e) else "abstract only"
        elif src:
            state = "ingested"
        elif e.get("access") == "file":
            state = "file present" if local_file(workspace, e) else "waiting for file"
        else:
            state = "to fetch"
        out.append({**e, "state": state, "source_id": src["id"] if src else None})
    return out


def fetch(conn: sqlite3.Connection, workspace: Path, *, topic: str | None = None, seed: bool = True,
          delay: float = 3.0, fetch_fn: ingest.ax.Fetch = ingest.ax.http_fetch,
          report: Callable[[str], None] = print, downloads: Path | None = None) -> dict[str, list[str]]:
    """Ingest every open-access entry and every textbook the operator has placed; queue seed reads.

    Papers stored as abstract only (ePrint refuses automated PDF downloads) get their full text from, in
    order: a file under library/, a PDF the operator downloaded into ``downloads`` (matched by ePrint number
    and checked against the title), or the paper's arXiv version."""
    result: dict[str, list[str]] = {"ingested": [], "already": [], "waiting": [], "failed": [], "abstract_only": [],
                                    "completed": []}
    first = True
    rivals = [s["title"] for s in ingest.abstract_only(conn)]
    for e in entries(topic):
        src = find_ingested(conn, e)
        if src and _abstract_only(src):
            done = _complete(conn, workspace, e, src, fetch_fn, delay, report, result, downloads, rivals)
            if not done:
                result["abstract_only"].append(e["id"])
            continue
        if src:
            result["already"].append(e["id"])
            if src.get("kind") in ("book", "notes") and store.select(
                    conn, "questions", "kind = 'seed' AND origin_id = ?", (src["id"],), limit=1):
                reading.seed_reference(conn, src["id"])   # raises a textbook's waiting seed read to the front
            if src.get("kind") in ("book", "notes") and _units(conn, src["id"]) == ingest.MAX_UNITS_PER_SOURCE:
                try:  # cut at the old 400-unit cap: add the rest, keeping the ids already cited
                    added = ingest.complete_units(conn, src["id"])
                except Exception as exc:  # one failure must not stop the list
                    report(f"{e['id']}: could not add the remaining units: {type(exc).__name__}: {exc}")
                else:
                    if added:
                        report(f"{e['id']}: added the {added} units beyond the old 400-unit cap")
                        result["completed"].append(e["id"])
            continue
        domains = e.get("domains") or None
        try:
            if e.get("access") in ("arxiv", "eprint"):
                if not first:
                    ingest.ax.polite_sleep(delay)
                first = False
                res = ingest.ingest_reference(conn, e["ref"], workspace, domains=domains, fetch=fetch_fn, delay=delay)
            else:
                path = local_file(workspace, e)
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
    # papers the agents asked for by ePrint number that the knowledge base has no record of yet: take the record
    # (title, abstract) from the ePrint page so a downloaded PDF can be matched to it below
    result["requested"] = _record_requests(conn, workspace, fetch_fn, delay, report, result)
    # abstract-only papers that are not on the list (the agents found them or asked for them)
    listed = {(find_ingested(conn, e) or {}).get("id") for e in entries()}
    rivals = [s["title"] for s in ingest.abstract_only(conn)]
    for src in ingest.abstract_only(conn):
        if src["id"] in listed:
            continue
        name = src["title"][:40]
        if not _complete(conn, workspace, None, src, fetch_fn, delay, report, result, downloads, rivals):
            result["abstract_only"].append(name)
    result["sectioned"] = add_missing_sections(conn, report)
    result["unmatched"] = [str(p) for p in recent_unmatched(downloads, result.get("picked", []))]
    for path in result["unmatched"]:
        report(f"not matched to any paper: {path} (use 'ingest file <pdf> --eprint YYYY/NNNN' or --title)")
    return result


def add_missing_sections(conn: sqlite3.Connection, report: Callable[[str], None]) -> list[str]:
    """PDF sources ingested before their prose was kept get it now (their units and ids stay as they are)."""
    done = []
    for src in store.select(conn, "sources", "format = 'pdf' AND path IS NOT NULL"):
        path = Path(src["path"])
        if not path.is_file() or path.suffix.lower() != ".pdf" or ingest.section_count(conn, src["id"]):
            continue
        n = ingest.index_sections(conn, src["id"], path)
        if n:
            report(f"{src['title'][:60]}: text kept in {n} sections")
            done.append(src["id"])
    return done


def _record_requests(conn: sqlite3.Connection, workspace: Path, fetch_fn: ingest.ax.Fetch, delay: float,
                     report: Callable[[str], None], result: dict[str, list[str]]) -> list[str]:
    from . import wanted  # noqa: PLC0415  (wanted imports ingest; keep the import graph flat)
    done = []
    for r in wanted.open_requests(conn):
        kind = ingest.ax.classify(r.get("reference") or "")
        if not kind or kind[0] != "eprint" or r.get("source_id") or ingest.find_source(conn, *kind):
            continue
        try:
            ingest.ax.polite_sleep(delay)
            res = ingest.ingest_reference(conn, kind[1], workspace, fetch=fetch_fn, delay=delay)
        except Exception as exc:  # one failure must not stop the list
            result["failed"].append(f"{kind[1]}: {type(exc).__name__}: {exc}")
            report(f"requested {kind[1]}: {type(exc).__name__}: {exc}")
            continue
        store.update(conn, "paper_requests", r["id"], source_id=res.source_id)
        report(f"requested by the agents: {res.summary()}")
        done.append(kind[1])
    return done


def recent_unmatched(downloads: Path | None, picked: list[str], days: float = 2) -> list[Path]:
    """PDFs that arrived in Downloads in the last ``days`` days and were not matched to any paper."""
    if downloads is None or not Path(downloads).expanduser().is_dir():
        return []
    cutoff = time.time() - days * 86400
    taken = {str(Path(p).resolve()) for p in picked}
    return sorted(p for p in Path(downloads).expanduser().iterdir()
                  if p.is_file() and p.suffix.lower() == ".pdf" and p.stat().st_mtime >= cutoff
                  and str(p.resolve()) not in taken)


def _complete(conn: sqlite3.Connection, workspace: Path, e: dict[str, Any] | None, src: dict[str, Any],
              fetch_fn: ingest.ax.Fetch, delay: float, report: Callable[[str], None],
              result: dict[str, list[str]], downloads: Path | None = None, rivals: Any = ()) -> bool:
    """Give an abstract-only source its full text: from the arXiv LaTeX when the entry points at arXiv,
    otherwise from a file under library/, a download in ``downloads``, or an arXiv version found by title.
    Returns True when the full text was attached (or an error was recorded)."""
    name = e["id"] if e else src["title"][:40]
    eprint_id = (src.get("identifiers") or {}).get("eprint")
    try:
        if e and e.get("access") == "arxiv":
            arxiv_id = ingest.ax.normalize_arxiv_id(e["ref"])
            dest = workspace / "sources" / "arxiv" / arxiv_id.replace("/", "_")
            fmt = ingest.ax.download_arxiv_source(arxiv_id, dest, fetch=fetch_fn)
            path = dest if fmt == "latex" else next(iter(sorted(dest.rglob("*.pdf"))), None)
            if path is None:
                return False
            res = ingest.attach_file(conn, src["id"], path, identifiers={"arxiv": arxiv_id})
        else:
            path = local_file(workspace, e if e else (eprint_id or "").replace("/", "_"))
            if path is None and downloads is not None and eprint_id:
                found = find_download(downloads, eprint_id, src["title"], rivals)
                if found is not None:
                    path = workspace / LIBRARY_DIR / f"{e['id'] if e else eprint_id.replace('/', '_')}.pdf"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(found, path)
                    result.setdefault("picked", []).append(str(found))
                    report(f"{name}: picked up {found}")
            if path is not None:
                res = ingest.attach_file(conn, src["id"], path)
            else:
                res = ingest.attach_arxiv_copy(conn, src["id"], workspace, fetch=fetch_fn, delay=delay)
                if res is None:
                    return False
    except Exception as exc:  # one failure must not stop the list
        result["failed"].append(f"{name}: {type(exc).__name__}: {exc}")
        report(f"{name}: {type(exc).__name__}: {exc}")
        return True
    report(res.summary())
    result["completed"].append(name)
    if store.select(conn, "questions", "kind = 'seed' AND origin_id = ?", (src["id"],), limit=1):
        reading.seed_reference(conn, src["id"])   # a waiting seed read moves up now that the full text is here
    return True


_SMALL_WORDS = {"the", "and", "for", "from", "with", "over", "on", "of", "in", "an", "a", "to", "its"}


def _title_on_page(title: str, text: str, share: float = 0.7) -> bool:
    words = [w for w in ingest.ax.norm_title(title).split() if w not in _SMALL_WORDS and len(w) > 2]
    page = set(ingest.ax.norm_title(text).split())
    return bool(words) and sum(w in page for w in words) >= share * len(words)


def _title_pos(title: str, text: str) -> int | None:
    """Where the whole title, in order, starts near the top of the page (spaces ignored: PDF text often loses
    or adds them); None when it is not there. Stricter than _title_on_page, for files whose name says nothing
    about the paper."""
    t = ingest.ax.norm_title(unicodedata.normalize("NFKC", title)).replace(" ", "")
    page = ingest.ax.norm_title(unicodedata.normalize("NFKC", text)).replace(" ", "")
    if len(t) < 16:
        return None
    i = page.find(t, 0, TITLE_WINDOW + len(t))
    return None if i < 0 else i


def _title_at_top(title: str, text: str, rivals: Any = ()) -> bool:
    """The title is at the top of the page, before any rival title found there: a paper's abstract may quote
    the title of a related paper that is also wanted."""
    pos = _title_pos(title, text)
    if pos is None:
        return False
    return not any(r != title and (rp := _title_pos(r, text)) is not None and rp < pos for r in rivals)


_PAGES: dict[tuple[str, float, int], str | None] = {}


def _first_page_cached(path: Path) -> str | None:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _PAGES:
        _PAGES[key] = _first_page(path)
    return _PAGES[key]


def _first_page(path: Path) -> str | None:
    try:
        from pypdf import PdfReader  # noqa: PLC0415
    except ImportError:
        return None
    import logging  # noqa: PLC0415
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # font-encoding warnings are noise here
    try:
        reader = PdfReader(str(path))
        return "\n".join((reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages))))
    except Exception:
        return ""


def find_download(folder: Path, eprint_id: str, title: str, rivals: Any = ()) -> Path | None:
    """A PDF of ePrint paper ``eprint_id`` that a browser saved into ``folder``.

    Browsers name the file after the URL (``939.pdf`` for 2015/939, ``939 (1).pdf`` for a second copy) or the
    operator saves it as ``2015-939.pdf``. The bare number is ambiguous across years, so such a file is taken
    only when its first page carries the paper's title (needs pypdf); year-qualified names are taken as is.
    A file with any other name (a browser that names downloads after the page title, or a renamed file) is
    taken when the paper's full title stands at the top of its first page."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return None
    by_number = _find_numbered(folder, eprint_id, title)
    if by_number is not None:
        return by_number
    cutoff = time.time() - DOWNLOAD_MAX_AGE_DAYS * 86400
    for p in sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        if p.stat().st_mtime < cutoff:
            break
        text = _first_page_cached(p)
        if text and _title_at_top(title, text, rivals):
            return p
    return None


def _find_numbered(folder: Path, eprint_id: str, title: str) -> Path | None:
    year, num = eprint_id.split("/")
    nums = {num, str(int(num))}
    qualified = re.compile(rf"{year}[-_](?:{'|'.join(sorted(nums))})(?:\s*\(\d+\)|-\d+)?\.pdf", re.IGNORECASE)
    bare = re.compile(rf"(?:{'|'.join(sorted(nums))})(?:\s*\(\d+\)|-\d+)?\.pdf", re.IGNORECASE)
    found = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in found:
        if qualified.fullmatch(p.name):
            text = _first_page_cached(p)
            if text is None or not text or _title_on_page(title, text):
                return p
    for p in found:
        if bare.fullmatch(p.name):
            text = _first_page_cached(p)
            if text and _title_on_page(title, text):
                return p
    return None


def missing_full_text(conn: sqlite3.Connection, workspace: Path) -> list[tuple[str, str, Path]]:
    """(title, PDF URL to open in a browser, where to save it) for every abstract-only ePrint source."""
    by_eprint = {e["ref"]: e for e in entries() if e.get("access") == "eprint"}
    out = []
    for src in ingest.abstract_only(conn):
        eid = (src.get("identifiers") or {}).get("eprint")
        if not eid:
            continue
        name = by_eprint[eid]["id"] if eid in by_eprint else eid.replace("/", "_")
        out.append((src["title"], ingest.eprint_pdf_url(eid), workspace / LIBRARY_DIR / f"{name}.pdf"))
    return out


def render_entry(e: dict[str, Any], state: str | None = None, workspace: Path | None = None) -> str:
    authors = ", ".join(a.split()[-1] for a in e.get("authors") or [])
    head = f"[{e['id']}] {e['title']} ({authors}, {e.get('year')}) {e.get('kind')}"
    if state:
        head += f" -- {state}"
    lines = [head, f"  topics: {', '.join(e.get('topics') or [])}", f"  why: {e.get('why', '').strip()}"]
    if e.get("access") == "eprint" and workspace and state and state.startswith("abstract only"):
        lines.append(f"  full text: open {ingest.eprint_pdf_url(e['ref'])} in a browser and save it as "
                     f"{workspace / LIBRARY_DIR}/{e['id']}.pdf, then run library fetch")
    elif e.get("access") in ("arxiv", "eprint"):
        lines.append(f"  fetch: {e['ref']}")
    else:
        where = f"{workspace / LIBRARY_DIR}/{e['id']}.pdf" if workspace else f"<workspace>/{LIBRARY_DIR}/{e['id']}.pdf"
        lines.append(f"  file: place it at {where}" + (f" (free: {e['url']})" if e.get("url") else ""))
    return "\n".join(lines)
