import sqlite3

import pytest

from mathresearch import db, store


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_schema_created_and_versioned(conn):
    assert db.current_version(conn) == db.SCHEMA_VERSION
    counts = db.table_counts(conn)
    assert set(counts) == {"sources", "units", "problems", "attacks", "questions", "experiments", "findings", "runs",
                           "digests"}
    assert all(n == 0 for n in counts.values())


def test_migrate_is_idempotent(conn):
    db.migrate(conn)
    db.migrate(conn)
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert rows == db.SCHEMA_VERSION


def test_insert_roundtrips_json_columns(conn):
    sid = store.insert(conn, "sources", kind="paper", title="Hidden Field Equations", authors=["Patarin"],
                       year=1996, identifiers={"eprint": None}, domains=["mq"])
    src = store.get(conn, "sources", sid)
    assert src["authors"] == ["Patarin"]
    assert src["domains"] == ["mq"]
    assert sid.startswith("src_")


def test_units_fts_search(conn):
    sid = store.insert(conn, "sources", kind="book", title="Ideals, Varieties, and Algorithms")
    store.insert(conn, "units", source_id=sid, kind="theorem", label="Thm 2.5",
                 statement="Every ideal in k[x_1,...,x_n] has a Groebner basis.", hypotheses=["k a field"])
    store.insert(conn, "units", source_id=sid, kind="definition", label="Def 1.1",
                 statement="A monomial ordering is a total order on monomials compatible with multiplication.")
    hits = store.search_units(conn, "Groebner")
    assert len(hits) == 1 and hits[0]["label"] == "Thm 2.5"
    assert hits[0]["hypotheses"] == ["k a field"]


def test_question_blocking_and_unblocking(conn):
    parent = store.insert(conn, "questions", kind="investigate", lane="deepen",
                          title="Is the fiber of F over a random point recoverable?")
    child = store.insert(conn, "questions", kind="learn", lane="deepen",
                         title="Learn root counting over finite fields")
    store.block_on(conn, parent, child)
    assert store.get(conn, "questions", parent)["status"] == "blocked"
    assert store.get(conn, "questions", child)["parent_id"] == parent
    assert [q["id"] for q in store.open_questions(conn)] == [child]

    store.close_question(conn, child)
    assert store.get(conn, "questions", child)["status"] == "closed"
    assert store.get(conn, "questions", parent)["status"] == "open"


def test_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.insert(conn, "questions", kind="investigate", lane="deepen", title="x", status="weird")


def test_update_touches_updated_at(conn):
    pid = store.insert(conn, "problems", name="MQ", status="known")
    before = store.get(conn, "problems", pid)["updated_at"]
    conn.execute("UPDATE problems SET updated_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (pid,))
    store.update(conn, "problems", pid, status="idea")
    after = store.get(conn, "problems", pid)["updated_at"]
    assert after != "2000-01-01T00:00:00.000Z" and after >= before
