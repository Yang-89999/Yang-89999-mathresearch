"""Heuristic unit extraction from plain text (including text pulled out of PDFs).

Far weaker than the LaTeX splitter: statements are paragraphs that open with
"Theorem 3.1." or similar. Used for ePrint PDFs, lecture notes and text files.
"""

from __future__ import annotations

import re
from pathlib import Path

from .latex import LatexUnit

_HEAD = re.compile(
    r"^\s*(Definition|Theorem|Lemma|Proposition|Corollary|Conjecture|Algorithm|Example|Remark|Claim|Fact|"
    r"Assumption|Problem)\s*([0-9]+(?:\.[0-9]+)*)?\s*(?:\(([^)]{0,120})\))?\s*[.:]\s*(.*)$",
    re.IGNORECASE | re.DOTALL)

_KIND = {"claim": "proposition", "fact": "proposition", "assumption": "conjecture", "problem": "definition"}

_SECTION = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([A-Z][^\n]{2,80})$")


def extract_units_from_text(text: str, file: str | None = None, max_len: int = 4000) -> list[LatexUnit]:
    paragraphs = re.split(r"\n\s*\n", text)
    units: list[LatexUnit] = []
    line = 1
    section = None
    for para in paragraphs:
        para_lines = para.count("\n") + 1
        sm = _SECTION.match(para.strip()) if len(para.strip()) < 100 else None
        if sm:
            section = f"{sm.group(1)} {sm.group(2).strip()}"
        m = _HEAD.match(para)
        if m and m.group(4).strip():
            word = m.group(1).lower()
            kind = _KIND.get(word, word)
            number = m.group(2)
            label = f"{m.group(1).capitalize()} {number}" if number else None
            statement = re.sub(r"[ \t]+", " ", m.group(4).strip())
            # A proof that follows in the same paragraph is not part of the statement.
            statement = re.split(r"\bProof[.:]", statement, maxsplit=1)[0].strip()
            if 10 <= len(statement) <= max_len:
                units.append(LatexUnit(kind=kind, env=word, statement=statement, label=label,
                                       title=(m.group(3) or "").strip() or None, section=section,
                                       line=line, file=file))
        line += para_lines + 1
    return units


def pdf_to_text(path: Path) -> str:
    """Extract text from a PDF with pypdf (optional dependency). Raises ImportError when absent."""
    from pypdf import PdfReader  # noqa: PLC0415

    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # pragma: no cover - pypdf quirks on odd fonts
            pages.append("")
    return "\n\n".join(_dehyphenate(p) for p in pages)


def _dehyphenate(s: str) -> str:
    s = re.sub(r"-\n(?=[a-z])", "", s)
    return re.sub(r"(?<![.\n])\n(?!\n)", " ", s)
