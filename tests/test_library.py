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


def _abstract_only(conn, eprint_id, title):
    sid = store.insert(conn, "sources", kind="paper", title=title, identifiers={"eprint": eprint_id}, format="html",
                       provided_by="operator")
    store.insert(conn, "units", source_id=sid, kind="remark", title="Abstract", statement="An abstract.")
    from mathresearch import reading
    return sid, reading.seed_reference(conn, sid)


def test_full_text_completes_abstract_only_eprint_sources(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    monkeypatch.setattr(ingest, "ingest_reference", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    peikert, seed = _abstract_only(conn, "2015/939", "A Decade of Lattice Cryptography")
    tensors, _ = _abstract_only(conn, "2019/687", "General Linear Group Action on Tensors")
    assert {t for t, _, _ in library.missing_full_text(conn, tmp_path)} == {
        "A Decade of Lattice Cryptography", "General Linear Group Action on Tensors"}
    url, dest = [(u, d) for t, u, d in library.missing_full_text(conn, tmp_path) if "Decade" in t][0]
    assert url == "https://eprint.iacr.org/2015/939.pdf" and dest == tmp_path / "library" / "peikert_decade.pdf"
    st = {r["id"]: r["state"] for r in library.status(conn, tmp_path, topic="lattices")}
    assert st["peikert_decade"] == "abstract only"

    # the operator saves the paper under its ePrint number; the tensors paper has an arXiv copy we fetch ourselves
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "2015-939.tex").write_text(PAPER, encoding="utf-8")

    def arxiv_source(arxiv_id, dest, fetch=None):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "main.tex").write_text(PAPER, encoding="utf-8")
        return "latex"

    monkeypatch.setattr(ingest.ax, "download_arxiv_source", arxiv_source)
    res = library.fetch(conn, tmp_path, report=lambda s: None)
    assert {"peikert_decade", "ji_qiao_song_yun_tensors"} <= set(res["completed"])
    for sid in (peikert, tensors):
        src = store.get(conn, "sources", sid)
        units = store.select(conn, "units", "source_id = ?", (sid,))
        assert src["format"] == "latex" and [u["kind"] for u in units] == ["theorem"]
    assert store.get(conn, "sources", tensors)["identifiers"] == {"eprint": "2019/687", "arxiv": "1906.04330"}
    assert store.get(conn, "questions", seed)["origin_id"] == peikert          # the seed read still points here
    assert not library.missing_full_text(conn, tmp_path)
    assert library.fetch(conn, tmp_path, report=lambda s: None)["completed"] == []   # nothing left to complete


def test_cli_ingest_file_attaches_to_an_abstract_only_source(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    sid, _ = _abstract_only(conn, "2022/975", "An efficient key recovery attack on SIDH")
    paper = tmp_path / "sidh.tex"
    paper.write_text(PAPER, encoding="utf-8")
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "ingest", "file", str(paper), "--eprint", "2022/975", "--no-seed"]) == 0
    assert "full text attached" in capsys.readouterr().out
    assert store.get(conn, "sources", sid)["format"] == "latex" and len(store.select(conn, "sources")) == 1
