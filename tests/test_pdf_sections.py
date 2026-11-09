"""PDF sources keep their prose section by section, at the document's own headings."""

from mathresearch import db, ingest, library, store
from mathresearch.ingest.text import split_sections
from mathresearch.tools import registry

PAGES = [
    "Graph-Theoretic Algorithms for ATFE\nWard Beullens\nAbstract\nWe give an algorithm for ATFE.\n"
    "1 Introduction\nAlternating trilinear forms are used in ALTEQ.\n",
    "Introduction\n"                                     # a running header: not a second Introduction
    "2 Low-rank points and their graph\nA point x is low rank when phi(x, ., .) has rank at most 4.\n"
    "Theorem 1. The number of low-rank points is about q^(n-4).\n"
    "3 The attack on ALTEQ param-\neters\nWe count the points with the rank filter and build the graph.\n"
    "1 The cost is dominated by the collision search.\n"   # not a heading: 1 does not follow 3
    "4 Conclusion\nThe rank invariant shrinks the guess space.\n",
    "References\n[1] Tang et al. ALTEQ.\n",
]


def test_split_sections_follows_the_numbering():
    titles = [t for t, _ in split_sections(PAGES)]
    assert titles == ["Front matter", "Abstract", "1 Introduction", "2 Low-rank points and their graph",
                      "3 The attack on ALTEQ parameters", "4 Conclusion", "References"]
    body = dict(split_sections(PAGES))["3 The attack on ALTEQ parameters"]
    assert body.startswith("We count the points") and "collision search" in body
    pages = split_sections(["One page of text.", "Another page."])
    assert [t for t, _ in pages] == ["page 1", "page 2"]          # too few headings: pages are the structure


def _source(tmp_path, monkeypatch, pages=PAGES):
    conn = db.connect(tmp_path / "r.db")
    pdf = tmp_path / "beullens.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(ingest, "pdf_pages", lambda p: pages)
    text = "\n\n".join(pages)
    res = ingest.ingest_text(conn, text, title="Graph-Theoretic Algorithms for ATFE", path=str(pdf), fmt="pdf",
                             kind="paper")
    return conn, res


def test_pdf_prose_is_readable_and_searchable(tmp_path, monkeypatch):
    conn, res = _source(tmp_path, monkeypatch)
    assert "prose kept in 7 sections" in res.note
    unit = store.select(conn, "units", "source_id = ?", (res.source_id,))[0]
    assert unit["anchor"]["section"] == "2 Low-rank points and their graph"   # the theorem knows its section
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    outline, _ = registry.execute("read_source", {"source_id": res.source_id, "level": "outline"}, ctx)
    assert "## 3 The attack on ALTEQ parameters  (" in outline and "characters of text" in outline
    text, _ = registry.execute("read_source", {"source_id": res.source_id, "level": "section", "section": "3",
                                               "purpose": "decide whether the rank filter is the whole attack"}, ctx)
    assert "We count the points with the rank filter" in text
    text, _ = registry.execute("read_source", {"source_id": res.source_id, "level": "section", "section": "2 low",
                                               "purpose": "decide whether the rank filter is the whole attack"}, ctx)
    assert "units stated here" in text and unit["id"] in text
    hits, _ = registry.execute("search_units", {"query": "collision"}, ctx)
    assert "section '3 The attack on ALTEQ parameters'" in hits and "[collision]" in hits


def test_long_sections_are_read_in_parts(tmp_path, monkeypatch):
    long_pages = ["Abstract\nShort.\n1 Introduction\n" + "word " * 4000 + "\n2 Model\nx\n3 End\ny\n"]
    conn, res = _source(tmp_path, monkeypatch, long_pages)
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    args = {"source_id": res.source_id, "level": "section", "section": "1 Intro", "purpose": "x" * 40}
    first, _ = registry.execute("read_source", args, ctx)
    second, _ = registry.execute("read_source", {**args, "part": 2}, ctx)
    assert "part 1 of 3; pass part=2" in first and "(part 2 of 3; pass part=3" in second


def test_fetch_adds_sections_to_pdfs_ingested_before(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    pdf = tmp_path / "old.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sid = store.insert(conn, "sources", kind="paper", title="An older PDF", path=str(pdf), format="pdf")
    monkeypatch.setattr(ingest, "pdf_pages", lambda p: PAGES)
    lines = []
    assert library.add_missing_sections(conn, lines.append) == [sid]
    assert ingest.section_count(conn, sid) == 7 and "text kept in 7 sections" in lines[0]
    assert library.add_missing_sections(conn, lines.append) == []                # done once
