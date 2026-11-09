"""Split LaTeX sources into knowledge units.

A unit is one theorem-like environment (definition, theorem, lemma,
proposition, corollary, conjecture, example, remark) or one algorithm float,
with its exact statement, label and position. Nothing here calls a model: the
split is purely syntactic so that every unit is traceable to a line in a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

UNIT_KINDS = ("definition", "theorem", "lemma", "proposition", "corollary", "algorithm",
              "example", "remark", "conjecture")

# Display name of a theorem-like environment (from \newtheorem) -> unit kind.
_TITLE_TO_KIND = {
    "theorem": "theorem", "thm": "theorem", "main theorem": "theorem",
    "lemma": "lemma", "sublemma": "lemma",
    "proposition": "proposition", "claim": "proposition", "fact": "proposition", "property": "proposition",
    "corollary": "corollary",
    "definition": "definition", "notation": "definition", "problem": "definition", "construction": "definition",
    "convention": "definition",
    "conjecture": "conjecture", "assumption": "conjecture", "hypothesis": "conjecture", "question": "conjecture",
    "open problem": "conjecture",
    "example": "example", "examples": "example",
    "remark": "remark", "remarks": "remark", "observation": "remark", "note": "remark",
    "algorithm": "algorithm", "protocol": "algorithm", "scheme": "algorithm",
}

# Environments that exist without \newtheorem (amsthm defaults and common packages).
_DEFAULT_ENVS = {
    "theorem": "theorem", "thm": "theorem", "lemma": "lemma", "lem": "lemma", "proposition": "proposition",
    "prop": "proposition", "corollary": "corollary", "cor": "corollary", "definition": "definition",
    "defn": "definition", "dfn": "definition", "conjecture": "conjecture", "conj": "conjecture",
    "example": "example", "remark": "remark", "rem": "remark", "claim": "proposition", "fact": "proposition",
    "assumption": "conjecture", "problem": "definition",
}

_ALGORITHM_FLOATS = ("algorithm", "algorithm*", "algorithm2e", "algorithmic", "algo")


@dataclass
class LatexUnit:
    kind: str
    env: str
    statement: str
    label: str | None = None
    title: str | None = None
    section: str | None = None
    line: int = 0
    file: str | None = None
    labels: list[str] = field(default_factory=list)   # every \label inside (the unit's own, its equations')
    refs: list[str] = field(default_factory=list)     # labels it points to with \ref, \eqref, \cref, ...
    cites: list[str] = field(default_factory=list)    # bibliography keys it \cites
    proof: str | None = None                          # the proof environment right after it, if any

    def anchor(self) -> dict:
        a: dict = {"line": self.line}
        if self.file:
            a["file"] = self.file
        if self.section:
            a["section"] = self.section
        if self.label:
            a["label"] = self.label
        if len(self.labels) > 1:
            a["labels"] = self.labels
        return a


@dataclass
class LatexDocument:
    title: str | None
    authors: list[str]
    units: list[LatexUnit]
    environments: dict[str, str] = field(default_factory=dict)  # env -> kind
    macros: dict[str, str] = field(default_factory=dict)
    bibliography: dict[str, dict] = field(default_factory=dict)  # cite key -> {"text", "arxiv", "eprint", "doi"}


# -- preprocessing -------------------------------------------------------------

_COMMENT = re.compile(r"(?<!\\)%.*?$", re.MULTILINE)


def strip_comments(text: str) -> str:
    """Remove % comments (but not \\%)."""
    return _COMMENT.sub("", text)


_INPUT = re.compile(r"\\(?:input|include|subfile)\{([^}]+)\}")


def resolve_inputs(text: str, read: Callable[[str], str | None], depth: int = 0, max_depth: int = 6) -> str:
    """Inline \\input{} / \\include{} recursively. ``read(name)`` returns the file's
    text or None when it cannot be found (the command is then left in place)."""
    if depth >= max_depth:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1).strip()
        body = read(name)
        if body is None and not name.endswith(".tex"):
            body = read(name + ".tex")
        if body is None:
            return m.group(0)
        return "\n" + resolve_inputs(strip_comments(body), read, depth + 1, max_depth) + "\n"

    return _INPUT.sub(repl, text)


def file_reader(root: Path) -> Callable[[str], str | None]:
    def read(name: str) -> str | None:
        p = (root / name)
        try:
            p.resolve().relative_to(root.resolve())
        except ValueError:
            return None
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        return None
    return read


_NEWTHEOREM = re.compile(
    r"\\newtheorem\*?\s*\{([^}]+)\}\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", re.MULTILINE)
_NEWCOMMAND = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{?\\([A-Za-z]+)\}?\s*(?:\[(\d)\])?\s*\{", re.MULTILINE)
_DEF = re.compile(r"\\def\s*\\([A-Za-z]+)\s*\{")


def theorem_environments(text: str) -> dict[str, str]:
    """Map environment names to unit kinds from \\newtheorem declarations plus defaults."""
    envs = dict(_DEFAULT_ENVS)
    for m in _NEWTHEOREM.finditer(text):
        env, title = m.group(1).strip(), m.group(2).strip().lower()
        kind = _TITLE_TO_KIND.get(title)
        if kind is None:
            # "Hardness Assumption", "Main Theorem", "Key Lemma": match the longest known word.
            for key in sorted(_TITLE_TO_KIND, key=len, reverse=True):
                if re.search(r"\b" + re.escape(key) + r"\b", title):
                    kind = _TITLE_TO_KIND[key]
                    break
        if kind:
            envs[env] = kind
    return envs


def _balanced(text: str, start: int) -> tuple[str, int] | None:
    """Return the content of the brace group starting at text[start] == '{' and the index after it."""
    if start >= len(text) or text[start] != "{":
        return None
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return None


def macros(text: str) -> dict[str, str]:
    """Zero-argument macros defined with \\newcommand or \\def."""
    out: dict[str, str] = {}
    for rx in (_NEWCOMMAND, _DEF):
        for m in rx.finditer(text):
            if rx is _NEWCOMMAND and m.group(2):
                continue  # takes arguments; leave alone
            grp = _balanced(text, m.end() - 1)
            if grp is None:
                continue
            body = grp[0].strip()
            if 0 < len(body) <= 200:
                out[m.group(1)] = body
    return out


def expand_macros(text: str, defs: dict[str, str], passes: int = 3) -> str:
    if not defs:
        return text
    names = sorted(defs, key=len, reverse=True)
    rx = re.compile(r"\\(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z])")
    for _ in range(passes):
        new = rx.sub(lambda m: defs[m.group(1)], text)
        if new == text:
            break
        text = new
    return text


# -- extraction ----------------------------------------------------------------

_SECTION = re.compile(r"\\(section|subsection|chapter)\*?\s*(?:\[[^\]]*\])?\s*\{", re.MULTILINE)
_BEGIN = re.compile(r"\\begin\{([A-Za-z*0-9]+)\}")
_LABEL = re.compile(r"\\label\{([^}]+)\}")
_CAPTION = re.compile(r"\\caption\{")
_REF = re.compile(r"\\(?:ref|eqref|cref|Cref|autoref|nameref|vref|Vref|cpageref|labelcref)\*?\{([^}]+)\}")
_CITE = re.compile(r"\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|parencite|textcite|autocite|"
                   r"footcite|Cite|Citep|Citet)\*?(?:\[[^\]]*\]){0,2}\{([^}]+)\}")
_PROOF_START = re.compile(r"\s*\\begin\{proof\}")
_BIBITEM = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_ARXIV_ID = re.compile(r"(?:arXiv[:\s]*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})",
                       re.IGNORECASE)
_EPRINT_ID = re.compile(r"(?:eprint\.iacr\.org/|Cryptology\s+e?Print\s+Archive[^0-9]{0,40}(?:Report\s+)?)"
                        r"(\d{4})/(\d{3,5})", re.IGNORECASE)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s{},]+)")
MAX_PROOF_CHARS = 6000


def _keys(pattern: re.Pattern, text: str) -> list[str]:
    out: dict[str, None] = {}
    for m in pattern.finditer(text):
        for k in m.group(1).split(","):
            k = k.strip()
            if k:
                out.setdefault(k, None)
    return list(out)


def bibliography(text: str) -> dict[str, dict]:
    """``\\bibitem`` entries (from thebibliography or a .bbl file): key -> text and any arXiv/ePrint/DOI id."""
    items = list(_BIBITEM.finditer(text))
    out: dict[str, dict] = {}
    for i, m in enumerate(items):
        end = items[i + 1].start() if i + 1 < len(items) else len(text)
        raw = text[m.end():end]
        stop = raw.find("\\end{thebibliography}")
        if stop >= 0:
            raw = raw[:stop]
        entry: dict = {"text": " ".join(_strip_simple_commands(raw.replace("\\newblock", " ")).split())[:600]}
        if a := _ARXIV_ID.search(raw):
            entry["arxiv"] = a.group(1)
        if e := _EPRINT_ID.search(raw):
            entry["eprint"] = f"{e.group(1)}/{e.group(2)}"
        if d := _DOI.search(raw):
            entry["doi"] = d.group(1).rstrip(".")
        out[m.group(1).strip()] = entry
    return out
_TITLE_CMD = re.compile(r"\\title\s*(?:\[[^\]]*\])?\s*\{")
_AUTHOR_CMD = re.compile(r"\\author\s*(?:\[[^\]]*\])?\s*\{")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n\s*\n+")


def _sections(text: str) -> list[tuple[int, str]]:
    out = []
    for m in _SECTION.finditer(text):
        grp = _balanced(text, m.end() - 1)
        if grp:
            out.append((m.start(), clean_text(grp[0])))
    return out


def _section_at(sections: list[tuple[int, str]], pos: int) -> str | None:
    current = None
    for start, name in sections:
        if start <= pos:
            current = name
        else:
            break
    return current


def clean_text(s: str) -> str:
    s = _LABEL.sub("", s)
    s = s.replace("\\\\", "\n").replace("~", " ")
    s = _WS.sub(" ", s)
    s = _BLANKS.sub("\n\n", s)
    return s.strip()


def _strip_simple_commands(s: str) -> str:
    """Make a title readable: \\textbf{x} -> x, \\emph{x} -> x, \\thanks{..} removed."""
    s = re.sub(r"\\thanks\{[^}]*\}", "", s)
    s = re.sub(r"\\(?:textbf|textit|emph|textsc|text|mathrm|bf|it)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:and|\\)", ",", s)
    s = re.sub(r"\\[A-Za-z]+\s*", " ", s)
    return clean_text(s).strip(" ,")


def _find_end(text: str, env: str, start: int) -> int | None:
    """Index of the matching \\end{env} for a \\begin{env} that ends at ``start``, allowing nesting."""
    begin = re.compile(r"\\begin\{" + re.escape(env) + r"\}")
    end = re.compile(r"\\end\{" + re.escape(env) + r"\}")
    depth, pos = 1, start
    while True:
        e = end.search(text, pos)
        if e is None:
            return None
        b = begin.search(text, pos, e.start())
        if b is not None:
            depth += 1
            pos = b.end()
            continue
        depth -= 1
        if depth == 0:
            return e.start()
        pos = e.end()


def _optional_arg(text: str, pos: int) -> tuple[str | None, int]:
    """Parse an optional [..] argument (with nested braces) at ``pos``."""
    i = pos
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if i >= len(text) or text[i] != "[":
        return None, pos
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return None, pos


def extract_units(text: str, envs: dict[str, str] | None = None, file: str | None = None,
                  expand: dict[str, str] | None = None) -> list[LatexUnit]:
    envs = envs if envs is not None else theorem_environments(text)
    sections = _sections(text)
    units: list[LatexUnit] = []
    pos = 0
    while True:
        m = _BEGIN.search(text, pos)
        if m is None:
            break
        env = m.group(1)
        kind = envs.get(env)
        is_algo = env in _ALGORITHM_FLOATS
        if kind is None and not is_algo:
            pos = m.end()
            continue
        end = _find_end(text, env, m.end())
        if end is None:
            pos = m.end()
            continue
        body_start = m.end()
        title, body_start = _optional_arg(text, body_start)
        body = text[body_start:end]
        label_m = _LABEL.search(body)
        label = label_m.group(1).strip() if label_m else None
        if is_algo:
            kind = "algorithm"
            cap = _CAPTION.search(body)
            if cap:
                grp = _balanced(body, cap.end() - 1)
                if grp:
                    title = title or _strip_simple_commands(grp[0])
                    body = body[:cap.start()] + body[grp[1]:]
        statement = clean_text(expand_macros(body, expand or {}))
        if title:
            title = _strip_simple_commands(expand_macros(title, expand or {}))
        after = end + len(f"\\end{{{env}}}")
        proof_raw, proof = "", None
        pm = _PROOF_START.match(text, after)
        if pm:
            pend = _find_end(text, "proof", pm.end())
            if pend is not None:
                proof_raw = text[pm.end():pend]
                proof = clean_text(expand_macros(proof_raw, expand or {}))[:MAX_PROOF_CHARS] or None
        if statement:
            own = [lab.strip() for lab in _LABEL.findall(body)]
            refs = [r for r in _keys(_REF, body + "\n" + proof_raw) if r not in own]
            units.append(LatexUnit(kind=kind, env=env, statement=statement, label=label, title=title or None,
                                   section=_section_at(sections, m.start()),
                                   line=text.count("\n", 0, m.start()) + 1, file=file, labels=own, refs=refs,
                                   cites=_keys(_CITE, body + "\n" + proof_raw), proof=proof))
        pos = end
    return units


def document_metadata(text: str) -> tuple[str | None, list[str]]:
    title = None
    m = _TITLE_CMD.search(text)
    if m:
        grp = _balanced(text, m.end() - 1)
        if grp:
            title = _strip_simple_commands(grp[0]) or None
    authors: list[str] = []
    m = _AUTHOR_CMD.search(text)
    if m:
        grp = _balanced(text, m.end() - 1)
        if grp:
            raw = re.sub(r"\\(?:inst|thanks|footnote|affil|email)\{[^}]*\}", "", grp[0])
            raw = re.sub(r"\\(?:and|\\)", "\n", raw)
            for part in re.split(r"\n|,", raw):
                name = _strip_simple_commands(part)
                if name and len(name) < 80:
                    authors.append(name)
    return title, authors


def parse(text: str, read: Callable[[str], str | None] | None = None, file: str | None = None) -> LatexDocument:
    """Full pipeline on one main file: comments, inputs, macros, environments, units."""
    text = strip_comments(text)
    if read is not None:
        text = resolve_inputs(text, read)
    envs = theorem_environments(text)
    defs = macros(text)
    title, authors = document_metadata(text)
    units = extract_units(text, envs, file=file, expand=defs)
    bib_text = text
    if "\\bibitem" not in text and read is not None and file:
        bbl = read(Path(file).with_suffix(".bbl").name)  # bibtex output shipped with arXiv sources
        bib_text = bbl or ""
    return LatexDocument(title=title, authors=authors, units=units, environments=envs, macros=defs,
                         bibliography=bibliography(bib_text))


def find_main_file(root: Path) -> Path | None:
    """The .tex file containing \\documentclass (or \\begin{document}); largest wins on ties."""
    candidates = []
    for p in sorted(root.rglob("*.tex")):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\\documentclass" in head or "\\begin{document}" in head:
            candidates.append((len(head), p))
    if not candidates:
        texs = sorted(root.rglob("*.tex"), key=lambda p: p.stat().st_size, reverse=True)
        return texs[0] if texs else None
    candidates.sort(reverse=True)
    return candidates[0][1]


def parse_directory(root: Path) -> LatexDocument | None:
    main = find_main_file(root)
    if main is None:
        return None
    text = main.read_text(encoding="utf-8", errors="replace")
    return parse(text, read=file_reader(main.parent), file=str(main.relative_to(root)))
