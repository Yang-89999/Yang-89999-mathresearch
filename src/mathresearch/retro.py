"""Retrodiction tests: can the system rediscover a known result from the knowledge of its time?

A scenario fixes a date, loads only the literature and catalog entries known
by then, hides the lab's implementation of the answer, hands the system the
target and asks it to work. ``check`` then looks at the database for the
answer in the system's own words and code: a submitted attack that breaks
the target in polynomial time, verified by the lab on fresh instances.

The first scenario is Patarin's 1995 break of Matsumoto-Imai's C*.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from . import catalog, store
from .charter import Charter


@dataclass
class Scenario:
    name: str
    description: str
    year: int                               # knowledge cutoff (inclusive)
    units_file: str                         # YAML under mathresearch.catalog with sources + units
    hidden_attacks: list[str]               # lab attack implementations withheld from workers
    keep_catalog_ids: list[str]             # catalog entries to keep despite later references (folklore)
    remove_problem_ids: list[str]           # catalog problems replaced by the scenario's own target
    target_family: str
    target_sizes: list[int]                 # the submitted attack must break the largest of these
    problem: dict[str, Any]                 # the target problem row
    question: dict[str, Any]                # the opening question row
    forbidden_terms: list[str]              # must not appear anywhere in the seeded knowledge
    charter: dict[str, Any]
    extra_domains: list[str] = field(default_factory=list)


PATARIN_1995 = Scenario(
    name="patarin_1995",
    description="Rediscover Patarin's 1995 attack on the Matsumoto-Imai C* scheme from pre-1995 knowledge.",
    year=1994,
    units_file="retro_patarin_1995.yaml",
    hidden_attacks=["patarin_linearization_equations"],
    keep_catalog_ids=["atk_linearization", "atk_groebner_f4_f5", "atk_f4_f5_groebner", "atk_exhaustive_search"],
    remove_problem_ids=["prob_known_matsumoto_imai"],
    target_family="matsumoto_imai",
    target_sizes=[7, 9, 11, 13],
    problem={
        "id": "prob_retro_cstar",
        "name": "C* (Matsumoto-Imai 1988) inversion",
        "status": "well_defined",
        "domains": ["multivariate polynomials", "finite fields"],
        "objects": "n quadratic polynomials over GF(2) in n variables: P = T o F o S with F(u) = u^(1 + 2^theta) on GF(2^n)",
        "forward_map": "evaluate the n public quadratics at x in GF(2)^n",
        "forward_cost": "O(n^3) bit operations",
        "distribution": "random affine bijections S, T of GF(2)^n; theta = 1; n odd so that gcd(1 + 2^theta, 2^n - 1) = 1",
        "hidden": "a preimage x of a given y = P(x) (equivalently a zero of the published system P(x) - y)",
        "verification": "evaluate the public map and compare",
        "structure": {"trapdoor": "the inverse exponent h of 1 + 2^theta modulo 2^n - 1, known only with S, T and the field representation"},
        "quantum_notes": "not assessed",
        "generator_path": "matsumoto_imai",
        "refs": ["src_retro_mi88"],
    },
    question={
        "kind": "attack",
        "lane": "deepen",
        "status": "open",
        "title": "Find a polynomial-time attack on the C* scheme of Matsumoto and Imai",
        "body": (
            "The year is 1994. Matsumoto and Imai (EUROCRYPT 1988) proposed the public-key scheme C* "
            "(problem prob_retro_cstar; knowledge units from the paper and from Lidl-Niederreiter are indexed). "
            "Its security rests on a heuristic claim: that the affine maps S and T hide the structure of the "
            "central map u -> u^(1 + q^theta) well enough that an attacker is left with a generic quadratic "
            "system.\n\n"
            "Task: decide whether that claim holds. Study the public map experimentally (sample_instance gives "
            "instances in the format attack scripts receive; run_script lets you compute with them at small n; "
            "the family is matsumoto_imai with p = 2, theta = 1, n odd). Look for structure that survives the "
            "composition with S and T and that an attacker can exploit without the secret key. When you have an "
            "algorithm that recovers a preimage, submit it with submit_attack on sizes 7, 9, 11, 13; the verdict "
            "'broken' (every size solved, polynomial-like growth) settles the question. Record what you learn as "
            "findings with experiment evidence, including negative results about approaches that fail."
        ),
        "problem_id": "prob_retro_cstar",
        "domains": ["multivariate polynomials", "finite fields"],
        "priority": 1.0,
        "origin": "charter",
    },
    forbidden_terms=["Patarin", "linearization equation", "bilinear relation", "x^(q^theta) y", "Dobbertin"],
    charter={
        "objective": (
            "Cryptanalysis retrodiction: given only knowledge available in 1994, determine whether the "
            "Matsumoto-Imai C* scheme is secure, by finding and demonstrating an attack or by recording why "
            "every approach tried fails. Attack before believing: a claim without a verified experiment is "
            "a hypothesis."
        ),
        "domains": ["multivariate polynomials (quadratic maps over finite fields, their composition with affine maps)",
                    "finite fields (extension fields, Frobenius, linearized polynomials, power maps)"],
        "excluded_domains": [],
        "target": {"average_case": True, "efficient_verification": True, "wants_trapdoor": True,
                   "wants_group_action": False, "wants_homomorphism": False, "finite_field_formulation": True},
        "lanes": {"harvest": 0.0, "deepen": 1.0, "open": 0.0},
        "max_concurrent_runs": 1,
        "reflect_every_runs": 5,
        "hidden_attacks": ["patarin_linearization_equations"],
        "catalog_max_year": 1994,
        "notes": "Retrodiction scenario patarin_1995. Do not ingest literature published after 1994.",
    },
)

SCENARIOS: dict[str, Scenario] = {PATARIN_1995.name: PATARIN_1995}


def get_scenario(name: str) -> Scenario:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario {name!r}; available: {', '.join(SCENARIOS)}")
    return SCENARIOS[name]


# -- setup -----------------------------------------------------------------------

def load_units(conn: sqlite3.Connection, units_file: str) -> dict[str, int]:
    text = resources.files("mathresearch.catalog").joinpath(units_file).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    counts = {"sources": 0, "units": 0}
    for row in data.get("sources", []):
        row = dict(row)
        if store.get(conn, "sources", row["id"]) is None:
            store.insert(conn, "sources", **row)
            counts["sources"] += 1
    for row in data.get("units", []):
        row = dict(row)
        if row.get("anchor") is not None and not isinstance(row["anchor"], dict):
            row["anchor"] = {"note": str(row["anchor"])}
        store.insert(conn, "units", **row)
        counts["units"] += 1
    return counts


def setup(conn: sqlite3.Connection, workspace: Path, scenario: Scenario, charter_file: str = "charter.yaml") -> dict[str, Any]:
    """Prepare a fresh workspace for the scenario. The database must be empty of questions."""
    if conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]:
        raise RuntimeError("the workspace already has an agenda; use an empty workspace for a retrodiction test")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / charter_file).write_text(yaml.safe_dump(scenario.charter, sort_keys=False, allow_unicode=True),
                                          encoding="utf-8")
    cat = catalog.load(conn, max_year=scenario.year, hidden_attacks=set(scenario.hidden_attacks),
                       always_keep=set(scenario.keep_catalog_ids))
    for pid in scenario.remove_problem_ids:
        conn.execute("DELETE FROM problems WHERE id = ?", (pid,))
    units = load_units(conn, scenario.units_file)
    if store.get(conn, "problems", scenario.problem["id"]) is None:
        store.insert(conn, "problems", **scenario.problem)
    qid = store.insert(conn, "questions", **scenario.question)
    leak = leaks(conn, scenario)
    return {"scenario": scenario.name, "catalog": cat, "sources": units["sources"], "units": units["units"],
            "question_id": qid, "leaks": leak}


def leaks(conn: sqlite3.Connection, scenario: Scenario) -> list[str]:
    """Places where a forbidden term appears in the seeded knowledge (should be empty)."""
    out = []
    pattern = re.compile("|".join(re.escape(t) for t in scenario.forbidden_terms), re.IGNORECASE)
    for table, cols in (("units", ("label", "title", "statement", "computational_notes", "proof_sketch")),
                        ("problems", ("name", "objects", "forward_map", "distribution", "hidden", "structure",
                                      "quantum_notes", "best_attacks", "refs")),
                        ("attacks", ("name", "exploits", "complexity", "notes", "refs")),
                        ("weakness_patterns", ("name", "description", "signature", "test", "exploited_by",
                                               "examples", "refs")),
                        ("lessons", ("title", "lesson", "check_text", "refs")),
                        ("sources", ("title", "abstract", "card"))):
        for row in store.select(conn, table):
            for col in cols:
                v = row.get(col)
                if v is not None and pattern.search(str(v)):
                    out.append(f"{table}.{row['id']}.{col}")
    return out


# -- checking ----------------------------------------------------------------------

def check(conn: sqlite3.Connection, scenario: Scenario) -> dict[str, Any]:
    """Did the system find the answer? Looks for a submitted attack (an experiment with params.submitted)
    on the target family whose verdict is 'broken' at the largest target size."""
    largest = max(scenario.target_sizes)
    submissions = []
    for e in store.select(conn, "experiments", "kind = 'attack_sweep'", order="created_at"):
        params, result = e.get("params") or {}, e.get("result") or {}
        if not params.get("submitted") or params.get("family") != scenario.target_family:
            continue
        points = result.get("points") or []
        solved = [p["size"] for p in points if p.get("success")]
        submissions.append({"experiment_id": e["id"], "attack": params.get("attack"), "sizes": params.get("sizes"),
                            "solved": solved, "growth": result.get("growth"),
                            "largest_broken": result.get("largest_broken"), "run_id": e.get("run_id")})
    passing = [s for s in submissions if (s["largest_broken"] or 0) >= largest and s["growth"] == "polynomial-like"]
    runs = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status != 'running'").fetchone()["n"]
    findings = conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"]
    traceable = conn.execute("SELECT COUNT(*) AS n FROM findings WHERE evidence_kind != 'reasoning'").fetchone()["n"]
    question = store.select(conn, "questions", "kind = 'attack'", order="created_at", limit=1)
    return {
        "scenario": scenario.name,
        "passed": bool(passing),
        "passing_submissions": passing,
        "submissions": submissions,
        "runs": runs,
        "findings": findings,
        "findings_traceable": traceable,
        "question_status": question[0]["status"] if question else None,
        "leaks": leaks(conn, scenario),
    }


def render_check(result: dict[str, Any]) -> str:
    lines = [f"Retrodiction {result['scenario']}: {'PASSED' if result['passed'] else 'not yet'}",
             f"runs {result['runs']}, findings {result['findings']} ({result['findings_traceable']} with unit/source/"
             f"experiment evidence), opening question {result['question_status']}"]
    if result["submissions"]:
        lines.append("submitted attacks:")
        for s in result["submissions"]:
            mark = "*" if s in result["passing_submissions"] else " "
            lines.append(f" {mark} {s['attack']}: solved {s['solved']} of {s['sizes']}, growth {s['growth']} "
                         f"[{s['experiment_id']}]")
    else:
        lines.append("no attack submitted yet")
    if result["leaks"]:
        lines.append("WARNING forbidden terms present in: " + ", ".join(result["leaks"]))
    return "\n".join(lines) + "\n"


def charter_for(scenario: Scenario) -> Charter:
    return Charter.from_dict(scenario.charter)
