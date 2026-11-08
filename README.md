# MathResearch

An autonomous mathematical research environment. Its objective is to discover
computational problems that are easy to compute forward and hard to invert,
reconstruct or classify, and to develop the promising ones into rigorous
candidate hardness assumptions for post-quantum cryptography.

Two principles shape the design:

- **Learn while thinking.** The system reads mathematics because a research
  question needs it, not as a separate phase. Hitting a knowledge gap opens a
  "learn" question and the investigation resumes when it closes.
- **Attack before believing.** No candidate problem advances on prose. Each
  one must become code (instance generator, forward map, verifier) and
  survive a battery of standard attacks run at growing sizes.

## Architecture

One Python process, one SQLite file, one loop.

```
Agenda (pick a question) -> assemble context from the database
  -> worker run with tools (search, open, run_sage, run_attack, propose)
  -> structured write-back (findings, new questions, candidates, summary)
  -> next cycle; every N runs: regenerate the world digest, re-rank, report
```

The database (`research.db`) holds eight tables: `sources`, `units`
(definitions, theorems, algorithms with exact statements and anchors),
`problems` (known and candidate computational problems), `attacks`,
`questions` (the agenda), `experiments`, `findings` (append-only claims with
evidence), and `runs`. Workers keep no state of their own.

Effort is split between three lanes set in the charter: **harvest** (many
cheap runs that read and propose candidates), **deepen** (few long runs on
candidates that passed the computational filters) and **open** (exploration
not tied to the objective).

## Quick start

```bash
pip install -e ".[dev]"
mathresearch --dir ~/research init      # writes charter.yaml and research.db
$EDITOR ~/research/charter.yaml         # the only input the operator provides
mathresearch --dir ~/research status
mathresearch --dir ~/research charter show
mathresearch --dir ~/research run-script examples/roots_mod_p.py
pytest
```

SageMath is used automatically when `sage` is on `PATH`; scripts fall back to
plain Python otherwise. msolve and Magma are picked up by the attack lab when
present.

## Status

- [x] PR 1: package, SQLite data model, charter, sandboxed `run_script`, CLI
- [ ] PR 2: worker loop and provider adapters (OpenAI Responses, Claude Messages)
- [ ] PR 3: attack lab v1 (MQ over finite fields) and seed catalogs
- [ ] Literature ingestion (arXiv / IACR ePrint LaTeX sources), retrodiction test

The design review and build plan live in the project's design document.
