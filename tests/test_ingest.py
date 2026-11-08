import gzip
import io
import tarfile
from pathlib import Path

import pytest

from mathresearch import db, ingest, store
from mathresearch.cli import main
from mathresearch.ingest import arxiv as ax
from mathresearch.ingest import latex
from mathresearch.ingest.text import extract_units_from_text
from mathresearch.tools import registry

MAIN_TEX = r"""
\documentclass{article}
\usepackage{amsthm}
\newtheorem{thm}{Theorem}[section]
\newtheorem{lem}[thm]{Lemma}
\newtheorem{defn}[thm]{Definition}
\newtheorem{assump}{Hardness Assumption}
\newcommand{\F}{\mathbb{F}}
\newcommand{\abs}[1]{\left|#1\right|}  % takes an argument: left alone
\title{Linearization Equations for \textbf{C$^*$}}
\author{Jacques Patarin \and Someone Else\thanks{supported}}
\begin{document}
\maketitle
% A comment with \begin{thm} inside must be ignored
\section{Preliminaries}
\begin{defn}[Matsumoto--Imai map]\label{def:mi}
Let $q = 2$ and $K = \F_{q^n}$. The map $u \mapsto u^{1+q^\theta}$ is a bijection of $K$ when
$\gcd(1+q^\theta, q^n-1) = 1$. 100\% of the time.
\end{defn}
\input{sec2}
\begin{assump}\label{as:mq}
Solving random quadratic systems over $\F_q$ is hard.
\end{assump}
\begin{algorithm}
\caption{Find linearization equations}
\label{alg:lin}
\begin{algorithmic}
\State sample $O(n^2)$ pairs $(x, y)$
\State solve the linear system for $a_{ij}, b_i, c_j, d$
\end{algorithmic}
\end{algorithm}
\end{document}
"""

SEC2_TEX = r"""
\section{The attack}
\begin{thm}\label{thm:main}
There are at least $n$ linearly independent bilinear relations $\sum a_{ij} x_i y_j + \sum b_i x_i + \sum c_j y_j + d = 0$
satisfied by every pair $(x, y = P(x))$.
\end{thm}
\begin{proof}
Follows from $y = u^{1+q^\theta}$: multiply by $u$ and apply Frobenius.
\end{proof}
\begin{lem}
The relations give $y$-dependent linear equations in $x$; for random $y$ their rank is $n - \gcd(\theta, n)$.
\end{lem}
"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v2</id>
    <published>2023-01-01T00:00:00Z</published>
    <title>Linearization  Equations
      for C*</title>
    <summary>We break it.</summary>
    <author><name>Jacques Patarin</name></author>
    <author><name>Someone Else</name></author>
    <arxiv:doi>10.1000/xyz</arxiv:doi>
    <category term="cs.CR"/><category term="math.AG"/>
  </entry>
</feed>
"""


def _tar_gz(files: dict[str, str], extra_member: str | None = None) -> bytes:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if extra_member:
            data = b"evil"
            info = tarfile.TarInfo(extra_member)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


def fake_fetch(calls=None):
    calls = calls if calls is not None else []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if "export.arxiv.org/api/query" in url:
            return ATOM
        if "arxiv.org/e-print/" in url:
            return _tar_gz({"main.tex": MAIN_TEX, "sec2.tex": SEC2_TEX})
        if "eprint.iacr.org/2024/123.pdf" in url:
            return b"%PDF-1.4 fake"
        if "eprint.iacr.org/2024/123" in url:
            return (b'<html><head><title>Paper - Cryptology ePrint Archive</title>'
                    b'<meta name="citation_title" content="A &amp; B">'
                    b'<meta name="citation_author" content="Alice">'
                    b'<meta name="citation_author" content="Bob">'
                    b'<meta name="citation_publication_date" content="2024/02/01"></head></html>')
        raise AssertionError(f"unexpected URL {url}")

    fetch.calls = calls
    return fetch


# -- LaTeX splitting -------------------------------------------------------------

def test_parse_latex_document_extracts_units_with_anchors(tmp_path):
    (tmp_path / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (tmp_path / "sec2.tex").write_text(SEC2_TEX, encoding="utf-8")
    doc = latex.parse_directory(tmp_path)

    assert doc.title == "Linearization Equations for C$^*$"
    assert doc.authors == ["Jacques Patarin", "Someone Else"]
    assert doc.environments["assump"] == "conjecture" and doc.environments["defn"] == "definition"
    assert doc.macros == {"F": r"\mathbb{F}"}

    kinds = [u.kind for u in doc.units]
    assert kinds == ["definition", "theorem", "lemma", "conjecture", "algorithm"]

    d = doc.units[0]
    assert d.label == "def:mi" and d.title == "Matsumoto--Imai map" and d.section == "Preliminaries"
    assert r"\mathbb{F}_{q^n}" in d.statement and r"\label" not in d.statement
    assert r"100\% of the time" in d.statement  # escaped percent is not a comment
    assert d.anchor() == {"line": d.line, "file": "main.tex", "section": "Preliminaries", "label": "def:mi"}

    t = doc.units[1]
    assert t.label == "thm:main" and t.section == "The attack" and "bilinear relations" in t.statement
    assert "Follows from" not in t.statement  # proofs are not statements

    alg = doc.units[4]
    assert alg.title == "Find linearization equations" and alg.label == "alg:lin"
    assert r"\caption" not in alg.statement and "sample $O(n^2)$ pairs" in alg.statement


def test_resolve_inputs_refuses_paths_outside_root(tmp_path):
    (tmp_path / "paper").mkdir()
    (tmp_path / "secret.tex").write_text(r"\begin{thm}leak\end{thm}", encoding="utf-8")
    main_tex = r"\documentclass{article}\begin{document}\input{../secret}\end{document}"
    (tmp_path / "paper" / "main.tex").write_text(main_tex, encoding="utf-8")
    doc = latex.parse_directory(tmp_path / "paper")
    assert doc.units == []


def test_nested_environments_of_the_same_name_are_balanced():
    text = r"\begin{theorem}outer \begin{theorem}inner\end{theorem} tail\end{theorem}"
    units = latex.extract_units(text)
    assert len(units) == 1 and units[0].statement.startswith("outer") and units[0].statement.endswith("tail")


# -- plain text ----------------------------------------------------------------

def test_text_heuristic_units():
    text = ("2 Preliminaries\n\nTheorem 2.1 (Patarin). For every C* public key there exist n bilinear relations. "
            "Proof. Omitted.\n\nSome prose.\n\nDefinition 3. The MQ problem asks for a common zero.\n\n"
            "Lemma 4. x\n")
    units = extract_units_from_text(text)
    assert [(u.kind, u.label) for u in units] == [("theorem", "Theorem 2.1"), ("definition", "Definition 3")]
    assert units[0].title == "Patarin" and units[0].statement.endswith("bilinear relations.")
    assert units[0].section == "2 Preliminaries"


# -- arXiv helpers -------------------------------------------------------------

def test_identifier_normalisation():
    assert ax.normalize_arxiv_id("arXiv:2301.00001v2") == "2301.00001"
    assert ax.normalize_arxiv_id("https://arxiv.org/abs/2301.00001") == "2301.00001"
    assert ax.normalize_arxiv_id("https://arxiv.org/pdf/math/0601001v1.pdf") == "math/0601001"
    assert ax.normalize_arxiv_id("not an id") is None
    assert ax.normalize_eprint_id("https://eprint.iacr.org/2024/123.pdf") == "2024/123"
    assert ax.normalize_eprint_id("2024/7") == "2024/007"
    assert ax.classify("2024/123") == ("eprint", "2024/123")
    assert ax.classify("2301.00001") == ("arxiv", "2301.00001")
    assert ax.classify("hello") is None


def test_atom_parsing_and_search_query():
    fetch = fake_fetch()
    hits = ax.arxiv_search("linearization equations", max_results=5, fetch=fetch)
    assert "search_query=all%3Alinearization+AND+all%3Aequations" in fetch.calls[0]
    assert len(hits) == 1
    m = hits[0]
    assert m.identifier == "2301.00001" and m.title == "Linearization Equations for C*" and m.year == 2023
    assert m.authors == ["Jacques Patarin", "Someone Else"] and m.doi == "10.1000/xyz"
    assert m.identifiers() == {"arxiv": "2301.00001", "doi": "10.1000/xyz"}


def test_unpack_eprint_handles_tar_gz_gz_and_pdf(tmp_path):
    fmt = ax.unpack_eprint(_tar_gz({"a/main.tex": MAIN_TEX}, extra_member="../escape.tex"), tmp_path / "t")
    assert fmt == "latex" and (tmp_path / "t" / "a" / "main.tex").exists()
    assert not (tmp_path / "escape.tex").exists()

    fmt = ax.unpack_eprint(gzip.compress(MAIN_TEX.encode()), tmp_path / "g")
    assert fmt == "latex" and (tmp_path / "g" / "main.tex").exists()

    fmt = ax.unpack_eprint(b"%PDF-1.5 ...", tmp_path / "p")
    assert fmt == "pdf" and (tmp_path / "p" / "paper.pdf").exists()


# -- pipeline ------------------------------------------------------------------

def test_ingest_arxiv_end_to_end_and_dedup(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    fetch = fake_fetch()
    res = ingest.ingest_arxiv(conn, "arXiv:2301.00001v2", tmp_path, domains=["multivariate"], fetch=fetch, delay=0)
    assert not res.already_present and res.format == "latex" and res.units == 5
    assert res.kinds == {"definition": 1, "theorem": 1, "lemma": 1, "conjecture": 1, "algorithm": 1}

    src = store.get(conn, "sources", res.source_id)
    assert src["identifiers"] == {"arxiv": "2301.00001", "doi": "10.1000/xyz"}
    assert src["title"] == "Linearization Equations for C*" and src["year"] == 2023 and src["ingested_at"]
    assert Path(src["path"]).is_dir() and (Path(src["path"]) / "sec2.tex").exists()

    hits = store.search_units(conn, "bilinear relations")
    assert len(hits) == 1 and hits[0]["source_id"] == res.source_id and hits[0]["domains"] == ["multivariate"]
    assert hits[0]["anchor"]["file"] == "main.tex" and hits[0]["anchor"]["label"] == "thm:main"

    again = ingest.ingest_arxiv(conn, "2301.00001", tmp_path, fetch=fetch, delay=0)
    assert again.already_present and again.units == 5 and again.source_id == res.source_id
    assert len(fetch.calls) == 2  # metadata + e-print, no second download
    assert db.table_counts(conn)["units"] == 5


def test_ingest_eprint_uses_page_metadata(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    monkeypatch.setattr(ingest, "pdf_to_text", lambda p: "Theorem 1. The scheme is broken in polynomial time.")
    res = ingest.ingest_reference(conn, "https://eprint.iacr.org/2024/123", tmp_path, fetch=fake_fetch(), delay=0)
    src = store.get(conn, "sources", res.source_id)
    assert src["title"] == "A & B" and src["authors"] == ["Alice", "Bob"] and src["year"] == 2024
    assert src["identifiers"] == {"eprint": "2024/123"} and src["format"] == "pdf"
    assert res.units == 1 and res.kinds == {"theorem": 1}


def test_ingest_file_tex_and_text(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    (tmp_path / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (tmp_path / "sec2.tex").write_text(SEC2_TEX, encoding="utf-8")
    res = ingest.ingest_file(conn, tmp_path / "main.tex", domains=["mq"])
    assert res.units == 5 and res.title == "Linearization Equations for C$^*$"

    notes = tmp_path / "notes.txt"
    notes.write_text("Definition 1. A problem is easy if it is solvable in polynomial time.\n", encoding="utf-8")
    res = ingest.ingest_file(conn, notes, kind="notes")
    assert res.units == 1 and store.get(conn, "sources", res.source_id)["kind"] == "notes"
    with pytest.raises(ValueError):
        ingest.ingest_reference(conn, "nonsense", tmp_path)


# -- worker tools ----------------------------------------------------------------

def _ctx(conn, tmp_path, fetch):
    qid = store.insert(conn, "questions", kind="learn", lane="harvest", status="open",
                       title="Learn Patarin's attack", domains=["multivariate"])
    return registry.ToolContext(conn=conn, workspace=tmp_path, question=store.get(conn, "questions", qid),
                                run_id="run_test", fetch=fetch, fetch_delay=0)


def test_tools_search_ingest_and_list(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    ctx = _ctx(conn, tmp_path, fake_fetch())

    text, err = registry.execute("search_literature", {"query": "ti:linearization"}, ctx)
    assert not err and "arXiv:2301.00001" in text and "not ingested" in text

    text, err = registry.execute("ingest_source", {"reference": "2301.00001"}, ctx)
    assert not err and "ingested source src_" in text and "5 units" in text and "[unit_" in text
    units = store.select(conn, "units")
    assert all(u["domains"] == ["multivariate"] for u in units)  # inherited from the question

    text, _ = registry.execute("search_literature", {"query": "linearization"}, ctx)
    assert "already ingested as src_" in text

    text, err = registry.execute("list_sources", {}, ctx)
    assert not err and "5 units" in text and "arXiv:2301.00001" in text

    text, err = registry.execute("ingest_source", {"reference": "garbage"}, ctx)
    assert not err and "cannot recognise" in text

    names = {s.name for s in registry.specs()}
    assert {"search_literature", "ingest_source", "list_sources"} <= names


def test_learn_questions_get_kind_instructions(tmp_path):
    from mathresearch.context import user_prompt

    conn = db.connect(tmp_path / "r.db")
    qid = store.insert(conn, "questions", kind="learn", lane="harvest", status="open", title="Learn X")
    prompt = user_prompt(conn, store.get(conn, "questions", qid))
    assert "This is a learn question" in prompt and "ingest_source" in prompt
    qid2 = store.insert(conn, "questions", kind="survey", lane="harvest", status="open", title="Survey Y")
    assert "This is a learn question" not in user_prompt(conn, store.get(conn, "questions", qid2))


# -- CLI ---------------------------------------------------------------------------

def test_cli_ingest_file_and_units_search(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    (tmp_path / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (tmp_path / "sec2.tex").write_text(SEC2_TEX, encoding="utf-8")
    assert main(["--dir", str(tmp_path), "ingest", "file", str(tmp_path / "main.tex"), "--domains", "mq"]) == 0
    out = capsys.readouterr().out
    assert "5 units" in out
    assert main(["--dir", str(tmp_path), "units", "search", "Frobenius OR bilinear"]) == 0
    out = capsys.readouterr().out
    assert "[unit_" in out and "bilinear" in out
    assert main(["--dir", str(tmp_path), "status"]) == 0
    assert "units        5" in capsys.readouterr().out


# -- blocked downloads and forced re-ingest -----------------------------------------

def test_eprint_pdf_blocked_falls_back_to_metadata_and_abstract(tmp_path):
    import urllib.error

    base = fake_fetch()

    def fetch(url):
        if url.endswith("2024/123.pdf"):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if url.endswith("2024/123"):
            return (b'<html><head><meta name="citation_title" content="Memory Checking"></head><body>'
                    b'<h5 class="mt-3">Abstract</h5><p style="x">We prove a <b>tight</b> lower bound.\r\n\r\nMore.</p>'
                    b'</body></html>')
        return base(url)

    conn = db.connect(tmp_path / "r.db")
    res = ingest.ingest_reference(conn, "2024/123", tmp_path, fetch=fetch, delay=0)
    assert res.units == 1 and res.kinds == {"remark": 1} and res.format == "html"
    assert "refused the PDF download" in res.note and "ingest file" in res.note
    src = store.get(conn, "sources", res.source_id)
    assert src["format"] == "html" and src["path"] is None and src["title"] == "Memory Checking"
    unit = store.select(conn, "units")[0]
    assert unit["statement"] == "We prove a tight lower bound. More." and unit["title"] == "Abstract"


def test_force_reingest_reuses_the_source_row(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    first = ingest.ingest_arxiv(conn, "2301.00001", tmp_path, fetch=fake_fetch(), delay=0)
    second = ingest.ingest_arxiv(conn, "2301.00001", tmp_path, fetch=fake_fetch(), delay=0, force=True)
    assert second.source_id == first.source_id and not second.already_present
    assert db.table_counts(conn)["sources"] == 1 and db.table_counts(conn)["units"] == 5
    assert set(second.unit_ids).isdisjoint(first.unit_ids)  # units were rebuilt


def test_text_heuristic_ignores_prose_references_and_handles_pdf_layout():
    text = ("By Lemma 2.1 the claim follows for all n.  Lemma 2.2 The polynomial f is a permutation of F if and "
            "only if gcd(n, q-1) = 1.  Theorem 3 [12] Every C* public key admits n linearization equations. "
            "Proof. See [12].  Remark 4 lowercase start is not a statement")
    units = extract_units_from_text(text)
    assert [(u.kind, u.label) for u in units] == [("lemma", "Lemma 2.2"), ("theorem", "Theorem 3")]
    assert units[0].statement.startswith("The polynomial f") and units[0].statement.endswith("= 1.")
    assert units[1].statement == "Every C* public key admits n linearization equations."
