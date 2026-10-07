# ADR 0005: One portable core behind six ports, two thin platform editions

- **Status:** Accepted
- **Date:** 2026-09-16
- **Related:** [architecture.md](../architecture.md), [editions.md](../editions.md),
  [0003](0003-numbers-from-data-words-from-llm.md), [0008](0008-verifier-independent-of-planner.md)

## Context

Wapsi is entered in two hackathons, and each one wants a solution native to its platform:

| Event | Platform expectation (from the edition folders and `docs/originality.md`) |
|---|---|
| AI Builder Cup | Google Cloud services: ADK agents on Cloud Run, Gemini, BigQuery, Firestore (`editions/gcp/README.md`) |
| lablab.ai AMD ACT III | AI work on an AMD Instinct MI300X with ROCm, built during 12–18 Oct 2026 (`docs/originality.md`) |

The legal logic (tracing, pro-rata taint, dispositions, drafts, verification) is the hard part, and it must behave
identically everywhere: a hold amount cannot depend on which platform computed it. The team is small. Some
constraints also shape the design:

- `AGENTS.md` rule 7: Python only, version 3.11 or later.
- Victim and customer data should not have to move between platforms just to run the rules.

## Decision

1. **One MIT-licensed monorepo.** `src/wapsi` is the portable core. `editions/gcp` and `editions/amd` are thin
   layers that build adapters and platform interfaces around it.
2. **Six ports** in `src/wapsi/ports.py`, all `typing.Protocol` classes marked `@runtime_checkable`: `Ledger`,
   `DocSearch`, `LLM`, `Guard`, `CaseStore`, `ActionSink`. Structural typing means an adapter needs the right
   methods, not a base class or a registry.
3. **One entry point for everyone.** `wapsi.pipeline.run.process_case` and `approve` take adapters as arguments.
   The module docstring of `wapsi.pipeline.run` states the intent: every edition calls it with its own adapters.
4. **A small dependency footprint.** The core depends on pydantic, numpy, pandas, pyarrow, jinja2 and httpx.
   Everything else is an optional extra (`ml`, `docs`, `ui`, `amd`, `amd-app`, `gcp` in `pyproject.toml`) imported
   lazily.
   General-purpose cloud adapters (`BigQueryLedger`, `FirestoreCaseStore`, `ModelArmorGuard`, `GeminiLLM`) live in
   `src/wapsi/adapters/` and import their SDKs on first use.
5. **Portable by construction where platforms differ.** BM25 search is pure Python with no third-party
   dependencies (`wapsi.search.bm25`), so it runs in any container as is. Citation quotes ship inside the package
   (`src/wapsi/_data/citations.json`) so they resolve from an installed wheel or a zipped package without the
   repository's `data/` folder. CI runs on Python 3.11.
6. **Shared everything else:** the Streamlit officer app (`ui/`, with a `DataSource` protocol per edition), the
   golden sets and harness (`data/golden/`, `evals/`), the six agent skills (`.claude/skills/`, mirrored to
   `.agents/skills/`) and one `AGENTS.md` read by Claude Code and Gemini CLI.
7. **Editions do not change legal behaviour.** A rule fix goes into `src/wapsi` with a test. Each edition's
   adapters must reproduce the showcase numbers.

## Consequences

Positive:

- **Same numbers everywhere.** The showcase case gives ₹1,42,848.00 held and ₹3,34,848.00 recoverable whichever
  edition runs it, because the same function computes it.
- **One fix reaches both submissions.** The SOP Para 9.1(iii) correction ([ADR 0004](0004-intermediary-accounts-sop-9-1-iii.md))
  was made in the core, and every edition picked it up. Only the check that lives outside the core (the
  `lien-calculator` skill) had to repeat the rule.
- **Adapters are testable offline.** `tests/test_contracts.py` checks that the showcase ledger satisfies the
  `Ledger` protocol. The Google Cloud edition runs its BigQuery SQL on SQLite and compares it with
  `InMemoryLedger` (`editions/gcp/README.md`). The AMD edition swaps in `ParquetLedger`, a file case store and an
  OpenAI-compatible client without touching the core.
- **Each event still sees a native build.** The platform layer (ADK and Cloud Run, vLLM on ROCm with a GNN) is
  real platform work, and `docs/originality.md` records which work belongs to which event window.

Negative:

- **Lowest common denominator.** Python 3.11 is the floor. Heavy platform features are optional, never required
  by the core. The trace runs as Python everywhere, and `BigQueryLedger.reachable_accounts` is only an optional
  pre-filter.
- **Coupling near deadlines.** A core change lands in both editions at once. The guard is the test suite and the
  golden-set evaluations, which `.github/workflows/ci.yml` runs on every push; as of 2026-09-28 the repository has
  no git remote, so that workflow has not run on GitHub yet.
- **Originality bookkeeping.** Shared code predates some event windows. `docs/originality.md` and the
  `amd-baseline` tag are needed to show what was built when.

## Alternatives considered

| Option | Why not |
|---|---|
| Two separate products in two repositories | Two implementations of the same legal rules drift apart; two test suites; no shared golden set |
| Rewrite the engine natively per platform (BigQuery SQL, a GPU pipeline) | Exact ordered replay with per-victim taint vectors is hard to express and verify in SQL; two versions to keep equal to the paisa |
| One hosted core service that each platform calls over HTTP | It would move victim data between platforms and add a network dependency to every case |
| A plugin framework with entry points and registries | Unnecessary: Protocols give structural typing, and adapters are passed as arguments |

## Evidence

- Code: `src/wapsi/ports.py`, `src/wapsi/pipeline/run.py`, `src/wapsi/adapters/`, `src/wapsi/search/bm25.py`,
  `src/wapsi/citations.py` (`PACKAGED_CITATIONS`), `pyproject.toml`.
- Editions: `editions/gcp/README.md`, `editions/amd/app/__init__.py`.
- Tests: `tests/test_contracts.py::test_ledger_protocol`, `tests/gcp/` (adapter parity with fakes).
- CI: `.github/workflows/ci.yml`.
