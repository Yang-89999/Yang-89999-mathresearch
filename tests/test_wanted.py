"""Full texts the system cannot fetch itself: arXiv copies of ePrint papers, PDFs the operator downloads,
papers the agents ask for, and textbooks first in search."""

import io
import tarfile
import urllib.error

from mathresearch import db, ingest, library, store, wanted
from mathresearch.cli import main
from mathresearch.tools import registry

PAPER = r"""\documentclass{article}\newtheorem{thm}{Theorem}\begin{document}
\section{One}\begin{thm}\label{t}Trilinear form equivalence is hard.\end{thm}\end{document}"""


def _atom(arxiv_id, title, authors):
    names = "".join(f"<author><name>{a}</name></author>" for a in authors)
    return (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            f"<id>http://arxiv.org/abs/{arxiv_id}v1</id><published>2022-01-01T00:00:00Z</published>"
            f"<title>{title}</title><summary>s</summary>{names}</entry></feed>").encode()


EMPTY = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


def _tar(text):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as tf:
        data = text.encode()
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


def _fetch(atom, calls):
    def fetch(url):
        calls.append(url)
        if url.endswith("2022/267.pdf"):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if url.endswith("2022/267"):
            return (b'<html><head><meta name="citation_title" content="Practical Signatures from Trilinear Forms">'
                    b'<meta name="citation_author" content="Gang Tang"></head><body>'
                    b'<h5>Abstract</h5><p>We build signatures.</p></body></html>')
        if "export.arxiv.org/api/query" in url:
            return atom
        if "arxiv.org/e-print/2203.01234" in url:
            return _tar(PAPER)
        raise AssertionError(url)
    return fetch


def test_find_arxiv_copy_matches_title_and_authors():
    atom = _atom("2203.01234", "Practical Signatures from Trilinear Forms", ["Gang Tang", "Dung Hoang Duong"])
    fetch = lambda url: atom  # noqa: E731
    assert ingest.ax.find_arxiv_copy("Practical signatures from trilinear forms", ["Gang Tang"],
                                     fetch=fetch, delay=0).identifier == "2203.01234"
    assert ingest.ax.find_arxiv_copy("Practical Signatures from Trilinear Forms", ["Someone Else"],
                                     fetch=fetch, delay=0) is None          # same title, other authors
    assert ingest.ax.find_arxiv_copy("Impractical Lattice Signatures Revisited Again", [], fetch=fetch,
                                     delay=0) is None
    assert ingest.ax.norm_title(r"Not enough LESS over $\mathbb{F}_q$") == "not enough less over"


def test_blocked_eprint_takes_the_arxiv_version(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    conn = db.connect(tmp_path / "r.db")
    atom = _atom("2203.01234", "Practical Signatures from Trilinear Forms", ["Gang Tang"])
    res = ingest.ingest_reference(conn, "2022/267", tmp_path, fetch=_fetch(atom, []), delay=0)
    src = store.get(conn, "sources", res.source_id)
    assert src["format"] == "latex" and res.kinds == {"theorem": 1} and "arXiv version 2203.01234" in res.note
    assert src["identifiers"] == {"eprint": "2022/267", "arxiv": "2203.01234"}


def test_no_arxiv_version_keeps_the_abstract_and_is_not_rechecked_for_a_month(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    conn = db.connect(tmp_path / "r.db")
    calls = []
    res = ingest.ingest_reference(conn, "2022/267", tmp_path, fetch=_fetch(EMPTY, calls), delay=0)
    src = store.get(conn, "sources", res.source_id)
    assert src["format"] == "html" and "arxiv_checked" in src["identifiers"]
    n = len(calls)
    assert ingest.attach_arxiv_copy(conn, src["id"], tmp_path, fetch=_fetch(EMPTY, calls), delay=0) is None
    assert len(calls) == n                                              # no new arXiv query


def _abstract_only(conn, eprint_id, title):
    sid = store.insert(conn, "sources", kind="paper", title=title, identifiers={"eprint": eprint_id,
                       "arxiv_checked": "2999-01-01"}, format="html")
    store.insert(conn, "units", source_id=sid, kind="remark", title="Abstract", statement="An abstract.")
    return sid


def test_fetch_picks_up_browser_downloads(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    monkeypatch.setattr(ingest, "ingest_reference", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(ingest, "pdf_to_text", lambda p: "Theorem 1. The full text.")
    pages = {"939.pdf": "A Decade of Lattice Cryptography\nChris Peikert",
             "975.pdf": "Some other paper entirely about elliptic curves",
             "2022-214.pdf": ""}
    monkeypatch.setattr(library, "_first_page", lambda p: pages.get(p.name, ""))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for name in pages:
        (downloads / name).write_bytes(b"%PDF-1.4")
    peikert = _abstract_only(conn, "2015/939", "A Decade of Lattice Cryptography")
    sidh = _abstract_only(conn, "2022/975", "An efficient key recovery attack on SIDH")
    rainbow = _abstract_only(conn, "2022/214", "Breaking Rainbow Takes a Weekend on a Laptop")
    found = library.find_download(downloads, "2015/939", "A Decade of Lattice Cryptography")
    assert found and found.name == "939.pdf"
    assert library.find_download(downloads, "2022/975", "An efficient key recovery attack on SIDH") is None
    lines = []
    res = library.fetch(conn, tmp_path, report=lines.append, downloads=downloads)
    assert {"peikert_decade", "beullens_rainbow_weekend"} <= set(res["completed"])
    assert store.get(conn, "sources", peikert)["format"] == "pdf"
    assert store.get(conn, "sources", rainbow)["format"] == "pdf"           # year-qualified name taken as is
    assert store.get(conn, "sources", sidh)["format"] == "html"             # 975.pdf is another paper
    assert (tmp_path / "library" / "peikert_decade.pdf").is_file()
    assert any("picked up" in l and "939.pdf" in l for l in lines)


def test_agents_request_papers_and_the_operator_sees_why(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    sid = _abstract_only(conn, "2022/975", "An efficient key recovery attack on SIDH")
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="run_1")
    text, err = registry.execute("request_paper", {"citation": "Castryck, Decru. An efficient key recovery attack "
                                                   "on SIDH. Eurocrypt 2023", "reference": "2022/975",
                                                   "why": "need the glue-and-split lemma"}, ctx)
    assert not err and "Requested as want_" in text and "from memory" in text
    text, _ = registry.execute("request_paper", {"citation": "Castryck-Decru SIDH attack", "reference":
                                                 "https://eprint.iacr.org/2022/975", "why": "torsion images"}, ctx)
    assert "asked 2 times" in text
    text, _ = registry.execute("request_paper", {"citation": "Fulton, Harris. Representation Theory. 1991",
                                                 "why": "Weyl character formula statement"}, ctx)
    rows = wanted.open_requests(conn)
    assert [r["times"] for r in rows] == [2, 1] and rows[0]["source_id"] == sid
    assert rows[0]["whys"] == ["need the glue-and-split lemma", "torsion images"]
    assert rows[0]["link"] == "https://eprint.iacr.org/2022/975.pdf" and rows[0]["question_id"] == q["id"]
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "library", "wanted"]) == 0
    out = capsys.readouterr().out
    assert "asked 2x" in out and "glue-and-split" in out and "Fulton" in out and "2 requests" in out
    # the operator supplies the PDF: the request closes
    paper = tmp_path / "sidh.tex"
    paper.write_text(PAPER, encoding="utf-8")
    assert main(["--dir", str(tmp_path), "ingest", "file", str(paper), "--eprint", "2022/975", "--no-seed"]) == 0
    assert [r["citation"][:6] for r in wanted.open_requests(conn)] == ["Fulton"]
    assert main(["--dir", str(tmp_path), "library", "dismiss", rows[1]["id"]]) == 0
    assert wanted.open_requests(conn) == []
    # a paper already held in full is not requested
    text, _ = registry.execute("request_paper", {"citation": "x", "reference": "2022/975", "why": "y"}, ctx)
    assert "already in the knowledge base" in text


def test_library_open_lists_the_missing_links(tmp_path, capsys, monkeypatch):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    _abstract_only(conn, "2015/939", "A Decade of Lattice Cryptography")
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.setattr("time.sleep", lambda s: None)
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "library", "open", "--print-only"]) == 0
    assert "https://eprint.iacr.org/2015/939.pdf" in capsys.readouterr().out and not opened
    assert main(["--dir", str(tmp_path), "library", "open"]) == 0
    assert opened == ["https://eprint.iacr.org/2015/939.pdf"] and "library fetch" in capsys.readouterr().out


def test_search_units_puts_textbooks_first(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    paper = store.insert(conn, "sources", kind="paper", title="A paper on MinRank attacks")
    book = store.insert(conn, "sources", kind="book", title="Ideals, Varieties, and Algorithms")
    store.insert(conn, "units", source_id=paper, kind="theorem", statement="MinRank by Groebner basis is fast.")
    store.insert(conn, "units", source_id=paper, kind="lemma", statement="Groebner basis degree bound for MinRank.")
    store.insert(conn, "units", source_id=book, kind="definition", statement="A Groebner basis of an ideal is ...")
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    text, _ = registry.execute("search_units", {"query": "Groebner"}, ctx)
    first = text.splitlines()[0]
    assert "definition" in first and "(textbook: Ideals, Varieties" in first and text.count("(paper:") == 2
    text, _ = registry.execute("search_units", {"query": "Groebner", "in": "books"}, ctx)
    assert "(paper:" not in text and "textbook" in text
    text, _ = registry.execute("search_units", {"query": "MinRank", "in": "books"}, ctx)
    assert text.startswith("No knowledge units match in the textbooks")
    listing, _ = registry.execute("list_sources", {}, ctx)
    assert "textbook: Ideals" in listing and "paper: A paper" in listing


def test_fetch_records_requested_papers_and_matches_downloads_by_title(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "r.db")
    monkeypatch.setattr(ingest.ax, "polite_sleep", lambda s: None)
    monkeypatch.setattr(ingest, "pdf_to_text", lambda p: "Theorem 1. The full text.")
    titles = {"2022/1528": "Graph-Theoretic Algorithms for the Alternating Trilinear Form Equivalence Problem",
              "2024/364": "Algebraic Algorithm for the Alternating Trilinear Form Equivalence Problem"}

    class Res:
        def __init__(self, sid):
            self.source_id = sid

        def summary(self):
            return f"ingested {self.source_id}"

    def fake_reference(conn_, ref, workspace, **kw):
        if ref not in titles:
            raise AssertionError(ref)
        return Res(_abstract_only(conn_, ref, titles[ref]))

    monkeypatch.setattr(ingest, "ingest_reference", fake_reference)
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    for ref, why in [("2022/1528", "graph walks"), ("2024/364", "Groebner attack"), (None, "weights")]:
        wanted.request(conn, f"Some citation {ref}", why, reference=ref, question_id=q["id"])
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    pages = {  # named after the page title by the browser, or renamed by hand (with a typo)
        "Graph-Theoretic Algorithms for the Alternating Trilinear Form E.pdf":
            "Graph-Theoretic Algorithms for the Alternating\nTrilinear Form Equivalence Problem\nWard Beullens\n"
            "Abstract. We give an algebraic algorithm ...",
        "Algebriac Algorithm for the Altering Trilinear Form.pdf":
            "Algebraic Algorithm for the Alternating Trilinear Form Equivalence Problem\nLars Ran",
        "MAYO.pdf": "MAYO Specification Document",
    }
    for name in pages:
        (downloads / name).write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(library, "_first_page", lambda p: pages.get(p.name, ""))
    lines = []
    res = library.fetch(conn, tmp_path, report=lines.append, downloads=downloads, fetch_fn=lambda u: EMPTY)
    assert sorted(res["requested"]) == ["2022/1528", "2024/364"]
    for ref in titles:
        src = ingest.find_source(conn, "eprint", ref)
        assert src["format"] == "pdf", (ref, lines)
    assert [r["citation"] for r in wanted.open_requests(conn)] == ["Some citation None"]
    assert res["unmatched"] == [str(downloads / "MAYO.pdf")]
    assert any("not matched to any paper" in l and "MAYO.pdf" in l for l in lines)


def test_title_match_needs_the_whole_title_at_the_top():
    algebraic = "Algebraic Algorithm for the Alternating Trilinear Form Equivalence Problem"
    graph_page = ("Graph-Theoretic Algorithms for the Alternating Trilinear Form Equivalence Problem\n"
                  "Abstract. Unlike the algebraic algorithm for the alternating trilinear form equivalence problem")
    graph = graph_page.splitlines()[0]
    assert library._title_at_top(algebraic, graph_page)                          # alone, the quote fools it
    assert not library._title_at_top(algebraic, graph_page, rivals=[graph, algebraic])
    assert library._title_at_top(graph, graph_page, rivals=[graph, algebraic])
    assert not library._title_at_top(algebraic, "x" * 500 + algebraic)           # not at the top
    assert library._title_at_top(algebraic, "Algebraic Algoﬁrithm".replace("ﬁ", "") +
                                 " for the Alternating Trilinear\nForm Equivalence Problem")
    assert library._title_at_top("Practical Signatures from Trilinear Forms", "PracticalSignaturesfrom Trilinear Forms")
