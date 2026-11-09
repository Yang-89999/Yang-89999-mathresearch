"""arXiv and IACR ePrint access: metadata, search, and source download.

All network calls go through one injectable ``fetch(url) -> bytes`` so tests
run offline. arXiv asks for at most one request every three seconds; callers
that issue several requests pass ``delay``.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

USER_AGENT = "mathresearch/0.1 (autonomous research system; literature ingestion)"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_EPRINT = "https://arxiv.org/e-print/{id}"
ARXIV_ABS = "https://arxiv.org/abs/{id}"
EPRINT_PDF = "https://eprint.iacr.org/{id}.pdf"
EPRINT_PAGE = "https://eprint.iacr.org/{id}"

Fetch = Callable[[str], bytes]

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


RETRIES = 3            # attempts for a transient network failure (a cut-off download, a reset, a timeout)
RETRY_WAIT = 5.0       # seconds before the first retry; doubles after each


def _transient(exc: BaseException) -> bool:
    import http.client
    import socket
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):  # a server answer: only 429 and 5xx are worth retrying
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionError,
                            TimeoutError, socket.timeout, urllib.error.URLError))


def http_fetch(url: str, timeout: float = 60.0, retries: int = RETRIES, wait: float = RETRY_WAIT,
               sleep: Callable[[float], None] = time.sleep) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https/http to fixed hosts)
                return resp.read()
        except Exception as exc:
            if attempt == retries - 1 or not _transient(exc):
                raise
            sleep(wait * 2 ** attempt)
    raise RuntimeError("unreachable")


@dataclass
class PaperMeta:
    identifier: str                  # arXiv id without version, or ePrint "YYYY/NNN"
    service: str                     # 'arxiv' | 'eprint'
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    summary: str = ""
    categories: list[str] = field(default_factory=list)
    url: str = ""
    doi: str | None = None

    def identifiers(self) -> dict:
        out = {self.service: self.identifier}
        if self.doi:
            out["doi"] = self.doi
        return out


# -- identifiers ---------------------------------------------------------------

_NEW_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_ID = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?")
_EPRINT_ID = re.compile(r"(\d{4})/(\d{1,4})")


def normalize_arxiv_id(s: str) -> str | None:
    """'arXiv:2301.00001v2', 'https://arxiv.org/abs/2301.00001', 'math/0601001' -> canonical id."""
    s = s.strip()
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.IGNORECASE)
    if "arxiv.org" in s:
        s = s.split("arxiv.org/", 1)[1]
        s = re.sub(r"^(abs|pdf|e-print|format)/", "", s)
        s = re.sub(r"\.pdf$", "", s)
    m = _NEW_ID.fullmatch(s) or _OLD_ID.fullmatch(s)
    if m:
        return m.group(1)
    return None


def normalize_eprint_id(s: str) -> str | None:
    s = s.strip()
    if "eprint.iacr.org" in s:
        s = s.split("eprint.iacr.org/", 1)[1]
        s = re.sub(r"\.pdf$", "", s)
    m = _EPRINT_ID.fullmatch(s)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):03d}"
    return None


def classify(ref: str) -> tuple[str, str] | None:
    """('arxiv', id) or ('eprint', id) for a user-supplied reference, else None."""
    if "eprint.iacr.org" in ref or re.fullmatch(r"\s*\d{4}/\d{1,4}\s*", ref):
        e = normalize_eprint_id(ref)
        return ("eprint", e) if e else None
    a = normalize_arxiv_id(ref)
    return ("arxiv", a) if a else None


# -- arXiv metadata and search ---------------------------------------------------

def _parse_atom(xml_bytes: bytes) -> list[PaperMeta]:
    root = ET.fromstring(xml_bytes)
    out = []
    for entry in root.findall(_ATOM + "entry"):
        raw_id = (entry.findtext(_ATOM + "id") or "").strip()
        arxiv_id = normalize_arxiv_id(raw_id) or raw_id
        title = re.sub(r"\s+", " ", entry.findtext(_ATOM + "title") or "").strip()
        if not title or title.lower() == "error":
            continue
        authors = [re.sub(r"\s+", " ", a.findtext(_ATOM + "name") or "").strip()
                   for a in entry.findall(_ATOM + "author")]
        published = entry.findtext(_ATOM + "published") or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        summary = re.sub(r"\s+", " ", entry.findtext(_ATOM + "summary") or "").strip()
        cats = [c.get("term", "") for c in entry.findall(_ATOM + "category")]
        doi = entry.findtext(_ARXIV_NS + "doi")
        out.append(PaperMeta(identifier=arxiv_id, service="arxiv", title=title, authors=[a for a in authors if a],
                             year=year, summary=summary, categories=[c for c in cats if c],
                             url=ARXIV_ABS.format(id=arxiv_id), doi=doi.strip() if doi else None))
    return out


def arxiv_metadata(arxiv_id: str, fetch: Fetch = http_fetch) -> PaperMeta | None:
    url = f"{ARXIV_API}?id_list={urllib.parse.quote(arxiv_id)}&max_results=1"
    entries = _parse_atom(fetch(url))
    return entries[0] if entries else None


def arxiv_search(query: str, max_results: int = 10, fetch: Fetch = http_fetch,
                 sort_by: str = "relevance") -> list[PaperMeta]:
    """Search arXiv. ``query`` uses the API syntax (all:, ti:, abs:, au:, cat:); a bare
    phrase is searched in all fields."""
    q = query.strip()
    if not re.search(r"\b(all|ti|abs|au|cat|co|jr|rn|id):", q):
        # Bare phrase: require every word (arXiv ORs loose words after the first field).
        # Quoted phrases and hyphenated names stay whole.
        terms = re.findall(r'"[^"]+"|\S+', q)
        q = " AND ".join("all:" + t for t in terms if t.upper() not in ("AND", "OR", "ANDNOT"))
    params = urllib.parse.urlencode({"search_query": q, "max_results": max(1, min(int(max_results), 50)),
                                     "sortBy": sort_by, "sortOrder": "descending"})
    return _parse_atom(fetch(f"{ARXIV_API}?{params}"))


def norm_title(title: str) -> str:
    """Lower-case words only: drops TeX math, accents' markup and punctuation, for comparing titles."""
    t = re.sub(r"\$[^$]*\$", " ", title.lower())
    t = re.sub(r"\\[a-z]+", " ", t)
    return re.sub(r"[^0-9a-z]+", " ", t).strip()


def _surnames(authors: list[str]) -> set[str]:
    return {norm_title(a).split()[-1] for a in authors if norm_title(a)}


def find_arxiv_copy(title: str, authors: list[str] | None = None, fetch: Fetch = http_fetch,
                    delay: float = 3.0, min_ratio: float = 0.9) -> PaperMeta | None:
    """An arXiv version of a paper known by title (e.g. an ePrint paper whose PDF we may not download).

    Searches arXiv titles for the exact phrase, then for all title words; a hit counts only when its title
    matches closely (``min_ratio``) and, when both author lists are known, they share a surname."""
    from difflib import SequenceMatcher

    want = norm_title(title)
    words = want.split()
    if len(words) < 3:
        return None
    names = _surnames(authors or [])
    queries = [f'ti:"{want}"', " AND ".join(f"ti:{w}" for w in words if len(w) > 2)]
    for i, q in enumerate(queries):
        if i:
            polite_sleep(delay)
        for hit in arxiv_search(q, max_results=5, fetch=fetch):
            if SequenceMatcher(None, norm_title(hit.title), want).ratio() < min_ratio:
                continue
            got = _surnames(hit.authors)
            if names and got and not names & got:
                continue
            return hit
    return None


# -- source download -------------------------------------------------------------

def _is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> list[Path]:
    written = []
    for member in tf.getmembers():
        if not (member.isfile() or member.isdir()):
            continue  # no symlinks, devices
        target = dest / member.name
        if not _is_within(dest, target):
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        src = tf.extractfile(member)
        if src is None:
            continue
        with open(target, "wb") as out:
            out.write(src.read())
        written.append(target)
    return written


def unpack_eprint(data: bytes, dest: Path) -> str:
    """Write an arXiv e-print payload into ``dest`` and return its format:
    'latex' (one or more .tex files), 'pdf', or 'other'."""
    dest.mkdir(parents=True, exist_ok=True)
    if data[:4] == b"%PDF":
        (dest / "paper.pdf").write_bytes(data)
        return "pdf"
    payload = data
    if data[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(data)
        except OSError:
            (dest / "payload.bin").write_bytes(data)
            return "other"
    bio = io.BytesIO(payload)
    try:
        with tarfile.open(fileobj=bio, mode="r:*") as tf:
            _safe_extract_tar(tf, dest)
    except tarfile.TarError:
        # single gzipped file: a .tex (or, rarely, a PDF)
        if payload[:4] == b"%PDF":
            (dest / "paper.pdf").write_bytes(payload)
            return "pdf"
        (dest / "main.tex").write_bytes(payload)
    texs = list(dest.rglob("*.tex"))
    if texs:
        return "latex"
    if list(dest.rglob("*.pdf")):
        return "pdf"
    return "other"


def download_arxiv_source(arxiv_id: str, dest: Path, fetch: Fetch = http_fetch) -> str:
    data = fetch(ARXIV_EPRINT.format(id=arxiv_id))
    return unpack_eprint(data, dest)


# -- IACR ePrint ---------------------------------------------------------------

_META_TAG = re.compile(r'<meta\s+name="([^"]+)"\s+content="([^"]*)"', re.IGNORECASE)
_HTML_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ABSTRACT = re.compile(r"<h\d[^>]*>\s*Abstract\s*</h\d>\s*<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


class BlockedError(RuntimeError):
    """The site refused an automated download (bot check or access reservation)."""


def eprint_metadata(eprint_id: str, fetch: Fetch = http_fetch) -> PaperMeta:
    import html as html_lib

    page = fetch(EPRINT_PAGE.format(id=eprint_id)).decode("utf-8", errors="replace")
    metas: dict[str, list[str]] = {}
    for name, content in _META_TAG.findall(page):
        metas.setdefault(name.lower(), []).append(html_lib.unescape(content).strip())
    title = (metas.get("citation_title") or [""])[0]
    if not title:
        m = _HTML_TITLE.search(page)
        title = html_lib.unescape(m.group(1)).strip() if m else f"ePrint {eprint_id}"
        title = re.sub(r"\s*-\s*Cryptology ePrint Archive\s*$", "", title)
    authors = metas.get("citation_author") or []
    date = (metas.get("citation_publication_date") or metas.get("citation_online_date") or [""])[0]
    year = int(date[:4]) if date[:4].isdigit() else int(eprint_id[:4])
    m = _ABSTRACT.search(page)
    summary = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip() if m else ""
    return PaperMeta(identifier=eprint_id, service="eprint", title=title, authors=authors, year=year,
                     summary=summary, url=EPRINT_PAGE.format(id=eprint_id))


def download_eprint_pdf(eprint_id: str, dest: Path, fetch: Fetch = http_fetch) -> Path:
    """Download the PDF. ePrint serves PDFs behind a browser check that an automated client is
    not meant to pass; that surfaces as ``BlockedError`` so callers can fall back to metadata."""
    import urllib.error

    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "paper.pdf"
    try:
        data = fetch(EPRINT_PDF.format(id=eprint_id))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            raise BlockedError(f"eprint.iacr.org refused the PDF download (HTTP {exc.code})") from exc
        raise
    if not data.startswith(b"%PDF"):
        raise BlockedError("eprint.iacr.org returned a page instead of the PDF (browser check)")
    path.write_bytes(data)
    return path


def polite_sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)
