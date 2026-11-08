"""Heuristic unit extraction from plain text (including text pulled out of PDFs).

Far weaker than the LaTeX splitter: a statement is the text that follows a
header such as "Theorem 3.1.", "Lemma 2.1 [2]" or "Definition 4 (MQ)." up to
the next header, a blank line, a proof, or a length cap. Used for ePrint
PDFs, lecture notes and text files, where line breaks are unreliable.
"""

from __future__ import annotations

import logging
import re
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


def pdf_to_text(path: Path) -> str:
    """Extract text from a PDF with pypdf (optional dependency). Raises ImportError when absent."""
    from pypdf import PdfReader  # noqa: PLC0415

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font-encoding warnings are noise here
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # pragma: no cover - pypdf quirks on odd fonts
            pages.append("")
    return "\n\n".join(_dehyphenate(p) for p in pages)


def _dehyphenate(s: str) -> str:
    s = re.sub(r"-\n(?=[a-z])", "", s)
    return re.sub(r"(?<![.\n])\n(?!\n)", " ", s)
