"""The reading list: entries, local textbooks, fetching with seed reads."""

import re

from mathresearch import db, ingest, library, store
from mathresearch.cli import main

PAPER = r"""\documentclass{article}\newtheorem{thm}{Theorem}\title{A Book}\begin{document}
\section{One}\begin{thm}\label{t}Every ideal has a Groebner basis.\end{thm}\end{document}"""


def test_reading_list_is_well_formed():
    rows = library.entries()
    assert len(rows) >= 40 and len({r["id"] for r in rows}) == len(rows)
    for r in rows:
        assert r["title"] and r["authors"] and r["year"] and r["why"] and r["topics"] and r["domains"], r["id"]
        assert r["kind"] in ("book", "notes", "survey", "paper") and r["access"] in ("arxiv", "eprint", "file")
        if r["access"] == "file":
            assert "ref" not in r
        else:
            assert ingest.ax.classify(r["ref"]) is not None, r["ref"]
            assert re.fullmatch(r"\d{4}/\d{3,5}|arXiv:\d{4}\.\d{4,5}", r["ref"]), r["ref"]
    assert {"computational_algebra", "computational_algebraic_geometry", "representation_theory"} <= set(library.topics())
    assert len(library.entries(kind="book")) >= 15
    assert library.entries(topic="representation_theory")


def test_fetch_ingests_open_access_and_placed_books(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    fetched = []

    def fake(conn_, ref, ws, **kw):
        fetched.append(ref)
        sid = store.insert(conn_, "sources", kind="paper", title=f"paper {ref}",
                           identifiers=dict([ingest.ax.classify(ref)]))
        return ingest.IngestResult(source_id=sid, title=f"paper {ref}", format="latex", units=1)

    monkeypatch.setattr(ingest, "ingest_reference", fake)
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "cox_little_oshea_iva.tex").write_text(PAPER, encoding="utf-8")
    lines = []
    res = library.fetch(conn, tmp_path, topic="computational_algebra", report=lines.append)
    open_access = [e["ref"] for e in library.entries(topic="computational_algebra") if e["access"] != "file"]
    assert fetched == open_access and "cox_little_oshea_iva" in res["ingested"]
    assert "kreuzer_robbiano_cca" in res["waiting"] and not res["failed"]
    book = ingest.find_source(conn, "library", "cox_little_oshea_iva")
    assert book["kind"] == "book" and book["provided_by"] == "operator" and book["year"] == 2015
    assert "Cox" in " ".join(book["authors"]) and book["domains"] == ["computational algebraic geometry", "commutative algebra"]
    seeds = store.select(conn, "questions", "kind = 'seed'")
    assert len(seeds) == len(res["ingested"]) and all("seed read queued" in l for l in lines)
    # idempotent
    res2 = library.fetch(conn, tmp_path, topic="computational_algebra", report=lines.append)
    assert not res2["ingested"] and set(res2["already"]) >= set(res["ingested"])
    assert len(store.select(conn, "questions", "kind = 'seed'")) == len(seeds)
    st = {r["id"]: r["state"] for r in library.status(conn, tmp_path, topic="computational_algebra")}
    assert st["cox_little_oshea_iva"] == "ingested" and st["kreuzer_robbiano_cca"] == "waiting for file"


def test_fetch_survives_a_failing_reference(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")

    def flaky(conn_, ref, ws, **kw):
        if ref == "2015/939":
            raise OSError("network down")
        sid = store.insert(conn_, "sources", kind="paper", title=ref, identifiers=dict([ingest.ax.classify(ref)]))
        return ingest.IngestResult(source_id=sid, title=ref, format="html", units=0)

    monkeypatch.setattr(ingest, "ingest_reference", flaky)
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    res = library.fetch(conn, tmp_path, topic="lattices", report=lambda s: None)
    assert any(f.startswith("peikert_decade") for f in res["failed"]) and res["ingested"]


def test_cli_library(tmp_path, capsys, monkeypatch):
    main(["--dir", str(tmp_path), "init"])
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "library", "list", "--topic", "representation_theory"]) == 0
    out = capsys.readouterr().out
    assert "fulton_harris_rt" in out and "waiting for file" in out and "etingof_rt_notes" in out and "to fetch" in out
    assert main(["--dir", str(tmp_path), "library", "list", "--topic", "nope"]) == 1
    capsys.readouterr()
    monkeypatch.setattr(ingest, "ingest_reference", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "serre_lrfg.tex").write_text(PAPER, encoding="utf-8")
    assert main(["--dir", str(tmp_path), "library", "fetch", "--topic", "representation_theory"]) == 0
    out = capsys.readouterr().out
    assert "1 ingested" in out and "seed read queued" in out and "still wait for a file" in out
