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

## Running research cycles

```bash
pip install -e ".[anthropic]"      # or ".[openai]", or both
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY
mathresearch --dir ~/research run --cycles 5
mathresearch --dir ~/research run --cycles 5 --provider openai --model gpt-5
```

Each cycle picks a question (lane chosen by the charter's shares), assembles
the worker's context from the database, runs the model with tools
(`search_units`, `open_unit`, `search_literature`, `ingest_source`, `run_script`,
`run_attack`, `ladder_evaluate`, `list_catalog`, `read_problem`, `read_question`) and applies its structured write-back: findings with
evidence, new questions (a knowledge gap opens a `learn` question that blocks
its parent), candidate problems, and the rewritten question summary. The
conversation is discarded; only the write-back persists. `--provider fake`
exists for offline tests.

Provider adapters live in `mathresearch/providers/`: `anthropic_messages.py`
(Claude Messages API, `output_config.format` JSON schema, strict tools) and
`openai_responses.py` (Responses API, `store: false`, strict `text.format`,
encrypted reasoning items replayed). Both use the same strict JSON-schema
subset, so the write-back schema is defined once.

## The attack lab

A candidate problem advances only through computation. `mathresearch.lab`
holds **problem families** (a class with `generate`, `forward`, `verify`),
**attacks** (a class with `run(family, instance, time_limit)`), **sweeps**
that run an attack over growing sizes and classify the cost curve as
polynomial-like or exponential-like, and the **ladder** checks that record
findings and move a problem's status.

```bash
mathresearch --dir ~/research catalog load          # 16 known problems, 26 attacks with references
mathresearch lab run --family mq --p 7 --sizes 3,4,5,6
mathresearch lab run --family matsumoto_imai --attack patarin_linearization_equations --sizes 7,9,11,13
```

Families so far: `mq` (random quadratic systems over GF(p), planted
solution) and `matsumoto_imai` (the C* trapdoor over GF(2^n), the known
answer for the retrodiction test). Attacks: exhaustive search,
linearization, Patarin's linearization equations, and Gröbner bases through
SageMath when installed. Workers reach the lab through the `run_attack` and
`ladder_evaluate` tools.

## Literature ingestion

"Learn while thinking" needs literature the workers can cite exactly.
`mathresearch.ingest` downloads papers and splits them into knowledge
units with anchors (file, line, section, `\label`):

```bash
mathresearch --dir ~/research ingest search "ti:multivariate AND abs:linearization"
mathresearch --dir ~/research ingest fetch 2301.00001 https://eprint.iacr.org/2024/123 --domains multivariate
mathresearch --dir ~/research ingest file ~/papers/patarin95.tex --year 1995
mathresearch --dir ~/research units search "linearization equations"
```

arXiv e-print sources (LaTeX) give exact units: every theorem-like
environment, including ones declared with `\newtheorem`, plus `algorithm`
floats, with `\input` files inlined and zero-argument macros expanded.
IACR ePrint only publishes PDFs, so those go through `pypdf`
(`pip install -e ".[pdf]"`) and a heuristic that recognises paragraphs
starting with "Theorem 3.1.". Downloads are kept under
`~/research/sources/<service>/<id>/` so anchors can be reopened.

Workers reach the same code through the `search_literature`,
`ingest_source` and `list_sources` tools; a `learn` question is expected to
end with the needed material indexed and a finding per source.

## Status

- [x] PR 1: package, SQLite data model, charter, sandboxed `run_script`, CLI
- [x] PR 2: worker loop, provider adapters (OpenAI Responses, Claude Messages), write-back, agenda with lanes
- [x] PR 3: attack lab v1 (MQ and C* families, four attacks, sweeps, ladder) and seed catalogs
- [x] PR 4: literature ingestion (arXiv LaTeX sources, IACR ePrint PDFs, local files) and learn-question tools
- [ ] Reflection step and world digest; retrodiction test

The design review and build plan live in the project's design document.
