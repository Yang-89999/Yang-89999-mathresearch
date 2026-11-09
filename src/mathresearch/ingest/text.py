"""Heuristic unit extraction from plain text (including text pulled out of PDFs).

Far weaker than the LaTeX splitter: a statement is the text that follows a
header such as "Theorem 3.1.", "Lemma 2.1 [2]" or "Definition 4 (MQ)." up to
the next header, a blank line, a proof, or a length cap. Used for ePrint
PDFs, lecture notes and text files, where line breaks are unreliable.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from .latex import LatexUnit

_WORDS = r"Definition|Theorem|Lemma|Proposition|Corollary|Conjecture|Algorithm|Example|Remark|Claim|Fact|Assumption|Problem"

# A header: at a line start or after sentence punctuation / wide spacing, not after a
# prose word such as "by Lemma 2.1", with a number or a parenthesised name, then a
# statement that starts with a capital, a digit, maths or a quote.
_HEAD = re.compile(
    r"(?:(?<=^)|(?<=\n)|(?<=[.:;]\s)|(?<=\s\s))"
    rf"(?P<word>{_WORDS})\s*"
    r"(?P<num>[0-9]+(?:\.[0-9]+)*[a-z]?)?\s*"
    r"(?:\[(?P<ref>[^\]]{0,40})\])?\s*"
    r"(?:\((?P<title>[^)]{0,120})\))?\s*"
    r"(?P<punct>[.:])?\s+"
    r"(?=[A-Z0-9$\\\"'(“])")  # case-sensitive on purpose: "by lemma 2.1" in prose must not match

_KIND = {"claim": "proposition", "fact": "proposition", "assumption": "conjecture", "problem": "definition"}
_SECTION = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([A-Z][^\n]{2,80})$", re.MULTILINE)
_STOP = re.compile(r"\n\s*\n|\bProof[.:]\s|\bProof\b\s*\n")
MAX_STATEMENT = 1500
MIN_STATEMENT = 10


def extract_units_from_text(text: str, file: str | None = None, max_len: int = MAX_STATEMENT) -> list[LatexUnit]:
    sections = [(m.start(), f"{m.group(1)} {m.group(2).strip()}") for m in _SECTION.finditer(text)]
    heads = [m for m in _HEAD.finditer(text) if m.group("num") or m.group("title") or m.group("punct")]
    units: list[LatexUnit] = []
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[start:min(end, start + max_len * 2)]
        stop = _STOP.search(body)
        if stop:
            body = body[:stop.start()]
        statement = re.sub(r"\s+", " ", body).strip()
        if len(statement) > max_len:
            statement = statement[:max_len].rsplit(" ", 1)[0] + " ..."
        if len(statement) < MIN_STATEMENT:
            continue
        word = m.group("word").lower()
        number = m.group("num")
        section = None
        for pos, name in sections:
            if pos <= m.start():
                section = name
        units.append(LatexUnit(
            kind=_KIND.get(word, word), env=word, statement=statement,
            label=f"{word.capitalize()} {number}" if number else None,
            title=(m.group("title") or "").strip() or None, section=section,
            line=text.count("\n", 0, m.start()) + 1, file=file))
    return units


_PAGES_CACHE: dict[tuple[str, float, int], list[str]] = {}


def pdf_pages(path: Path) -> list[str]:
    """The raw text of each page (line breaks kept), with pypdf (optional dependency; ImportError when absent).
    Cached per file version, since ingestion reads a PDF twice (units, then sections)."""
    from pypdf import PdfReader  # noqa: PLC0415

    st = Path(path).stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key in _PAGES_CACHE:
        return _PAGES_CACHE[key]
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding warnings are noise here
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            # NFKC turns ligatures such as "ﬁ" back into letters, so "definition" finds "deﬁnition"
            pages.append(unicodedata.normalize("NFKC", page.extract_text() or ""))
        except Exception:  # pragma: no cover - pypdf quirks on odd fonts
            pages.append("")
    _PAGES_CACHE.clear()          # keep one file: books are large
    _PAGES_CACHE[key] = pages
    return pages


def pdf_to_text(path: Path) -> str:
    """Extract text from a PDF with pypdf (optional dependency). Raises ImportError when absent."""
    return "\n\n".join(_dehyphenate(p) for p in pdf_pages(path))


# -- sections: the document's own structure, for reading the prose ---------------------------------------------

_NUMBERED = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^\n]{1,90}?)\s*$")
_NAMED = re.compile(r"^\s*(Abstract|Introduction|Preliminaries|Background|Notation|Conclusions?|Discussion|"
                    r"References|Bibliography|Acknowledge?ments?|Appendix(?:\s+[A-Z0-9]+)?(?:[.:]?\s+[A-Z][^\n]{0,80})?)"
                    r"\s*$")
_CHAPTER = re.compile(r"^\s*((?:Chapter|CHAPTER)\s+\d+|§\s*\d+(?:\.\d+)?)\.?\s*([^\n]{0,90}?)\s*$")
MIN_SECTIONS = 3


def _successor(prev: tuple[int, ...] | None, cur: tuple[int, ...]) -> bool:
    """A numbered heading must continue the numbering: 1 -> 1.1 -> 1.2 -> 2 -> 2.1, never back or sideways."""
    if prev is None:
        return cur[0] <= 2 and all(c <= 2 for c in cur[1:])
    if len(cur) == len(prev) + 1:
        return cur[:-1] == prev and cur[-1] == 1
    if len(cur) <= len(prev):
        return cur[:-1] == prev[:len(cur) - 1] and cur[-1] == prev[len(cur) - 1] + 1
    return False


def _looks_like_heading(title: str) -> bool:
    words = title.split()
    return 0 < len(words) <= 12 and not title.rstrip().endswith((",", ";")) and \
        sum(ch.isdigit() for ch in title) <= len(title) // 3


def split_sections(pages: list[str]) -> list[tuple[str, str]]:
    """(heading, prose) per section of a document, cut at the document's own headings (numbered sections,
    named ones such as Introduction or References, chapters and §); one section per page when too few headings
    are found. Never cut at a fixed size: the structure is the author's."""
    lines: list[str] = []
    for page in pages:
        lines += page.split("\n") + [""]
    heads: list[tuple[int, str]] = []
    prev: tuple[int, ...] | None = None
    seen_named: set[str] = set()
    wrapped: set[int] = set()          # second lines of headings that wrapped
    for i, line in enumerate(lines):
        if len(line) > 110 or i in wrapped:
            continue
        m = _NUMBERED.match(line)
        if m and _looks_like_heading(m.group(2)):
            num = tuple(int(x) for x in m.group(1).split("."))
            if _successor(prev, num):
                title = m.group(2).strip()
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt and len(nxt.split()) <= 5 and (title.endswith("-") or nxt[:1].islower()):
                    title = (title[:-1] if title.endswith("-") else title + " ") + nxt
                    wrapped.add(i + 1)
                heads.append((i, f"{m.group(1)} {title}"))
                seen_named.add(title.split()[0].lower())     # "1 Introduction" then a running "Introduction"
                prev = num
            continue
        m = _CHAPTER.match(line)
        if m and (not m.group(2) or _looks_like_heading(m.group(2))):
            heads.append((i, f"{m.group(1)} {m.group(2).strip()}".strip()))
            prev = None if m.group(1).lower().startswith("chapter") else prev
            continue
        m = _NAMED.match(line)
        if m:
            name = m.group(1).split()[0].lower()
            if name not in seen_named:          # running headers repeat a name on every page
                seen_named.add(name)
                heads.append((i, m.group(1).strip()))
    if len(heads) < MIN_SECTIONS:
        return [(f"page {k + 1}", _dehyphenate(p).strip()) for k, p in enumerate(pages) if p.strip()]
    out = []
    if heads[0][0] > 0:
        front = "\n".join(lines[:heads[0][0]]).strip()
        if front:
            out.append(("Front matter", _dehyphenate(front).strip()))
    for k, (start, title) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        first = start + 2 if start + 1 in wrapped else start + 1
        body = _dehyphenate("\n".join(lines[first:end])).strip()
        out.append((title, body))
    return out


def _dehyphenate(s: str) -> str:
    s = re.sub(r"-\n(?=[a-z])", "", s)
    return re.sub(r"(?<![.\n])\n(?!\n)", " ", s)
