"""The logical structure of papers: \\ref links, citations, proofs, the bibliography, and the follow tool."""

from mathresearch import db, ingest, store
from mathresearch.cli import main
from mathresearch.ingest.latex import parse
from mathresearch.tools import registry

PAPER = r"""
\documentclass{article}
\newtheorem{theorem}{Theorem}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{definition}[theorem]{Definition}
\title{Planted subrepresentations}
\begin{document}
\section{Setup}
\begin{definition}\label{def:planted}
A planted instance is a representation $V$ with a hidden subrepresentation $W$.
\begin{equation}\label{eq:dim} \dim W = k \end{equation}
\end{definition}
\begin{lemma}\label{lem:rank}
If \eqref{eq:dim} holds then the moment map has rank at most $n-k$, see \cite{KS99}.
\end{lemma}
\begin{proof}
Apply Definition~\ref{def:planted} and the argument of \cite[Sec. 2]{Pat95,KS99}.
\end{proof}
\section{Main result}
\begin{theorem}\label{thm:main}
Recovering $W$ reduces to MinRank.
\end{theorem}
\begin{proof}
By Lemma~\ref{lem:rank} and Section~\ref{sec:missing}; compare \cite{BFP}.
\end{proof}
\begin{thebibliography}{9}
\bibitem{KS99} A. Kipnis and A. Shamir. Cryptanalysis of the HFE public key cryptosystem by relinearization.
CRYPTO 1999. \newblock doi:10.1007/3-540-48405-1_2
\bibitem[Pat95]{Pat95} J. Patarin. Cryptanalysis of the Matsumoto and Imai public key scheme. CRYPTO 1995.
\bibitem{BFP} M. Bardet, J.-C. Faugere, L. Perret. On the complexity of solving quadratic Boolean systems.
arXiv:1112.6263
\end{thebibliography}
\end{document}
"""


def test_parse_records_refs_cites_proofs_and_bibliography():
    doc = parse(PAPER)
    d, lem, thm = doc.units
    assert d.labels == ["def:planted", "eq:dim"] and d.refs == []
    assert lem.refs == ["eq:dim", "def:planted"] and lem.cites == ["KS99", "Pat95"]
    assert "argument of" in lem.proof and thm.refs == ["lem:rank", "sec:missing"] and thm.cites == ["BFP"]
    assert doc.bibliography["BFP"]["arxiv"] == "1112.6263"
    assert doc.bibliography["KS99"]["doi"].startswith("10.1007/3-540-48405-1_2")
    assert "Patarin" in doc.bibliography["Pat95"]["text"] and "arxiv" not in doc.bibliography["Pat95"]


def _ingest(conn, tmp_path):
    d = tmp_path / "paper"
    d.mkdir()
    (d / "main.tex").write_text(PAPER, encoding="utf-8")
    res = ingest.ingest_file(conn, d)
    return res, [store.get(conn, "units", i) for i in res.unit_ids]


def test_ingestion_links_units_and_follow_walks_them(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    res, (d, lem, thm) = _ingest(conn, tmp_path)
    assert "3 bibliography entries" in res.note
    assert lem["depends_on"] == [d["id"]] and thm["depends_on"] == [lem["id"]]   # eq:dim resolves to the definition
    assert store.get(conn, "sources", res.source_id)["bibliography"]["BFP"]["arxiv"] == "1112.6263"
    q = store.get(conn, "questions", store.insert(conn, "questions", kind="learn", lane="harvest", title="Q"))
    ctx = registry.ToolContext(conn=conn, workspace=tmp_path, question=q, run_id="r")
    text, err = registry.execute("follow", {"unit_id": lem["id"], "include_proof": False}, ctx)
    assert not err
    uses, used_by = text.split("Used by")
    assert d["id"] in uses and thm["id"] in used_by and "Cites (2)" in text
    assert "no arXiv/ePrint id" in text and "A proof is recorded" in text
    text, _ = registry.execute("follow", {"unit_id": thm["id"], "include_proof": True}, ctx)
    assert "ingest_source('1112.6263')" in text and "Proof:\nBy Lemma" in text
    # once the cited paper is ingested, follow points to it
    sid = store.insert(conn, "sources", kind="paper", title="Bardet et al.", identifiers={"arxiv": "1112.6263"})
    text, _ = registry.execute("follow", {"unit_id": thm["id"], "include_proof": False}, ctx)
    assert f"in the knowledge base as [{sid}]" in text


def test_relink_fills_in_sources_ingested_before(tmp_path, capsys):
    main(["--dir", str(tmp_path), "init"])
    conn = db.connect(tmp_path / "research.db")
    res, units = _ingest(conn, tmp_path)
    for u in units:  # what older ingestion stored: no links, cites or proofs
        store.update(conn, "units", u["id"], depends_on=None, cites=None, proof_sketch=None)
    store.update(conn, "sources", res.source_id, bibliography=None)
    capsys.readouterr()
    assert main(["--dir", str(tmp_path), "ingest", "relink"]) == 0
    assert "2 references resolved" in capsys.readouterr().out
    lem = store.get(conn, "units", units[1]["id"])
    assert lem["depends_on"] == [units[0]["id"]] and lem["cites"] == ["KS99", "Pat95"] and lem["proof_sketch"]
    assert main(["--dir", str(tmp_path), "units", "follow", units[2]["id"], "--proof"]) == 0
    assert "Proof:" in capsys.readouterr().out
