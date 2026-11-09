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
  -> next cycle; every N runs: reflect (tidy the agenda, regenerate the world digest)
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
(`search_units`, `open_unit`, `read_source`, `search_literature`, `ingest_source`,
`list_sources`, `run_script`, `run_attack`, `ladder_evaluate`, `sample_instance`, `submit_attack`,
`list_catalog`, `list_weakness_patterns`, `list_lessons`, `read_problem`, `read_question`) and applies its structured
write-back: findings with evidence, new questions with what sparked them (a knowledge gap opens a `learn`
question that blocks its parent), candidate problems, reading notes, paper cards, weakness audits, and the
rewritten question summary. The
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

## Purpose-driven reading

The models already know the field. Reading is for exact statements, results newer than the model's
training, and focus; it is a catalyst, not a course. Three rules follow.

**Your references are seeds.** Every paper you ingest with `ingest fetch` or `ingest file` gets a *seed
read*: a question that reads it as a PQC designer ("what does this make me want to try?"). It must write
the paper's card and open at least one question or candidate sparked by the paper, or record a negative
finding saying why the paper suggests nothing; it cannot close otherwise. `--no-seed` indexes a paper
without queuing one, and `ingest seed SOURCE_ID` queues one later. Papers the workers fetch themselves are
indexed but not seeded.

**Read top-down, with a brief.** `read_source` serves three levels: the paper card (L0: problem, hardness
assumption, structure, results, attacks, parameters, open problems, relevance), the outline (L1: sections
and their units) and full sections (L2). A section is served only with a `purpose`, the decision the
reading will change. Every source read in a run comes back as a reading note: the worker's prior, what
was confirmed, what surprised it, the structure the paper's attacks exploit, the limits the authors admit,
and the ideas it gives. Ideas become questions or candidates.

**Every question records its origin.** A new question or candidate says what sparked it: a reference
(source id), an experiment (experiment id), or the model's prior. Ids are checked; an unknown id counts as
prior. When prior-only ideas exceed `reading.max_prior_share` of the open ideas while grounded ones wait,
the reflection step lowers their priority. The digest shows the origin mix and which of your references
led to questions, candidates and experiments, which are still waiting for their seed read, and which led
nowhere.

**Weakness patterns.** `catalog load` also loads twelve weakness patterns, the structural reasons known
schemes broke (bilinear input-output relations, low rank hidden in the public forms, a common vanishing
subspace, an early Groebner degree fall, cheap orbit invariants, hidden abelian structure, auxiliary
public data, and others), each with its signature in public data and a computational test. Candidates are
audited against the patterns that apply to their domains; `avoided` and `vulnerable` need an experiment
id, otherwise the entry is recorded as `untested`, and a verified vulnerability is a negative finding.

## The reading list

`mathresearch library` holds a curated reading list (`catalog/reading_list.yaml`, 46 entries): the
standard textbooks of the charter's domains (Cox-Little-O'Shea, Kreuzer-Robbiano, Eisenbud, Sturmfels,
Derksen-Kemper, Fulton-Harris, Serre, Lidl-Niederreiter, Shoup, von zur Gathen-Gerhard, Ding-Petzoldt-
Schmidt, MacWilliams-Sloane, Micciancio-Goldwasser, Edelsbrunner-Harer, Hatcher), and open-access surveys
and foundational papers on arXiv and IACR ePrint: lattice and code-based cryptography surveys, quantum
algorithms for algebraic problems, tensor and polynomial isomorphism, cryptographic group actions, CSIDH
and the SIDH break, the UOV/Rainbow attacks and MAYO, Groebner-basis complexity, MinRank solvers, code
and lattice equivalence, invariant theory and orbit-closure algorithms, geometric complexity theory.
Every open-access id was checked against the record it names.

```bash
mathresearch --dir ~/research library list --topic representation_theory
mathresearch --dir ~/research library fetch          # arXiv and ePrint entries, plus any textbook you placed
```

Textbooks are paywalled, so the system cannot download them: put your copy at
`~/research/library/<id>.pdf` (or a `.tex` file or LaTeX directory named `<id>`) and the next
`library fetch` ingests it; `library list` prints the exact path for each one. Everything the list
brings in counts as an operator reference and gets a seed read.

## Lessons from the field

`catalog load` also loads thirteen lessons drawn from the published work of Oded Regev, Chris Peikert,
Leo Ducas, Vadim Lyubashevsky and Shi Bai: back average-case hardness with a reduction, attack the noise
when noise carries the hardness, justify every piece of algebraic structure, keep a reduction that survives
the structure, instantiate exactly as analysed, state the assumption precisely, asymptotics are not
security, classification problems need failing invariants, design out statistical leakage, keep a knob that
scales security, small secrets need their own attacks, measure attacks rather than only model them, and
quantum algorithms keep improving. Each lesson is our paraphrase with its references and a concrete check;
workers are told to argue from the papers and never to speak for the authors.

The lessons reach the workers in two ways. Every worker prompt carries them as a short block. And when a
candidate reaches a new ladder status (from `reviews.from_status`, default `well_defined`), the reflection
step opens a **review** in the deepen lane: the candidate is checked against one expert's lessons, reading
their papers where a lesson needs exact content, with a finding per lesson. Experts rotate per candidate,
preferring those whose lessons fit its domains; `reviews.max_per_reflection` caps how many open at once.

```bash
mathresearch --dir ~/research lessons list --expert Peikert
mathresearch --dir ~/research lessons ingest            # fetch the cited papers and queue seed reads
```

`lessons ingest` fetches the eleven cited papers (arXiv gives full LaTeX; IACR ePrint gives title and
abstract, so download the PDFs you care about and run `ingest file`). Retrodiction scenarios drop every
lesson published after their date.

## Reflection and the world digest

Every `reflect_every_runs` runs (charter; default 20) the loop pauses to
tidy the agenda and regenerate the **world digest**, a short markdown
summary that every later worker sees in its context: live candidates and
their ladder status, what broke and why, notable anomalies, lane shares
against the charter, starved domains, blocked and stale questions, cost.
The same step marks same-named candidates as duplicates, abandons
questions whose problem is broken or duplicate, decays the priority of
questions that keep running without closing, and re-seeds a survey for
any charter domain with no live question. `mathresearch reflect` runs it
on demand and writes `digest.md`; digests are also kept in the `digests`
table.

## Submitted attacks and the retrodiction test

Workers can propose attacks as code. `submit_attack` takes a self-contained
script, runs it in the sandbox on fresh instances the lab generates (the
script sees only `instance.json`, the public polynomials), verifies every
claimed solution, sweeps sizes for a cost curve, records an experiment and a
finding, and registers the attack when it works. A candidate is `broken`
only when every size is solved with polynomial-like growth.

The same machinery drives the **retrodiction test**, the acceptance test for
the whole system: can it rediscover a known result from the knowledge of
its time?

```bash
mathresearch --dir ~/retro retro init --scenario patarin_1995
mathresearch --dir ~/retro run --cycles 10
mathresearch --dir ~/retro retro check
```

`patarin_1995` seeds a workspace with only pre-1995 knowledge (Matsumoto and
Imai 1988, Lidl and Niederreiter, Garey and Johnson, Buchberger), removes
every catalog entry published later, hides the lab's own implementation of
Patarin's attack from the tools, and opens one `attack` question on C*.
`retro check` passes when a submitted attack breaks `matsumoto_imai` at
n = 13 with polynomial-like growth, and warns if any forbidden term leaked
into the seeded knowledge.

What this test does and does not show: the model has almost certainly seen
Patarin's 1995 paper in training. Hiding it from the knowledge base and the
lab cannot hide it from the model's weights, so passing shows that the
pipeline can turn an idea into a verified attack, not that the system can
discover one. A discovery test needs a result published after the model's
training cutoff.

## Spending caps and parallel workers

Every run records its tokens and its cost in dollars. Claude model prices
are built in; for any other model (OpenAI's included) put the price in the
charter, in dollars per million tokens:

```yaml
pricing:
  your-openai-model-id: {input: 1.25, cached_input: 0.125, output: 10.0}
budget:
  max_usd_per_run: 2.0        # a run at its cap is told to write back and stop
  max_tokens_per_run: 600000  # works even for a model without a price
  max_usd_per_day: 20.0       # no new run starts once the day's cap is used
  max_usd_total: null         # lifetime cap for this workspace
```

`run` refuses to start a model that has no price while dollar caps are set
(`--allow-unpriced` runs it under the token cap alone). `--budget-usd 5`
caps a single invocation. A run near a daily or total cap gets only what is
left as its own cap, and runs in flight hold their allowance until they
finish, so parallel workers cannot overshoot together. `status` shows spend
today and in total, by model; the world digest shows spend by lane.

`max_concurrent_runs` in the charter (or `run --workers N`) runs that many
questions at once, each worker with its own database connection; the main
thread alone picks questions, checks the caps and runs the reflection step.

Ctrl+C stops `run` safely. A run cut off mid-way is recorded as cancelled with
the tokens and dollars it had used, and its question goes back on the agenda.
If a process dies without that chance (a crash, a closed laptop lid), the
next `run` marks its unfinished runs cancelled and reopens their questions;
`status` then notes that their spend was not recorded. Run one `run` process
per workspace at a time, since that recovery assumes no other process is
working on the same database.

## Status

- [x] PR 1: package, SQLite data model, charter, sandboxed `run_script`, CLI
- [x] PR 2: worker loop, provider adapters (OpenAI Responses, Claude Messages), write-back, agenda with lanes
- [x] PR 3: attack lab v1 (MQ and C* families, four attacks, sweeps, ladder) and seed catalogs
- [x] PR 4: literature ingestion (arXiv LaTeX sources, IACR ePrint PDFs, local files) and learn-question tools
- [x] PR 5: reflection step, agenda hygiene and the world digest
- [x] PR 6: submitted attacks, hidden-knowledge scenarios, retrodiction test `patarin_1995`
- [x] PR 7: cost accounting, spending caps, parallel workers
- [x] PR 8: purpose-driven reading (seed reads, levelled reading, reading notes, question origins, weakness patterns)
- [x] PR 9: lessons from experts' published work in every prompt, timely expert reviews of candidates
- [x] PR 10: the reading list (textbooks the operator places, open-access papers fetched automatically)
- [ ] More families (HFE, UOV, MinRank, syndrome decoding) and attacks (MinRank, msolve Gröbner)

The design review and build plan live in the project's design document.
