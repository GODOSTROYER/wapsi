# ADR 0007: Synthetic banking data only; public legal texts fetched, not redistributed

- **Status:** Accepted
- **Date:** 2026-09-16
- **Related:** [0006](0006-transparent-heuristic-classifier-default.md), [0002](0002-pro-rata-apportionment.md)

## Context

Wapsi works on highly sensitive data: victims' names and phone numbers, account numbers, transaction trails, and
suspicion about named people. A hackathon project cannot use real bank data, and it must be safe to publish, demo
on a public URL and run in trial cloud accounts.

At the same time, Wapsi's claims are numerical ("holds ₹1,42,848.00, not ₹5,03,50,000.00"), so the evaluation needs
exact ground truth: the correct tainted amount, role and disposition for every account, to the paisa.

The legal texts behind every rule (the MHA/I4C SOP, BNSS 2023, the PML Rules 2005, the FIU-IND FAQ, the RBI
Fraud Risk Management Directions, 2026) are public, but the corpus manifest records none of them as openly
licensed for redistribution; the SOP states no open licence at all (`data/docs/MANIFEST.md`).

## Decision

1. **All banking data is synthetic** (`AGENTS.md`: "All data is synthetic. Never add real customer, victim or
   employer data.").
   - People, merchants, employers, banks, account numbers, GSTINs, PANs and mobiles are generated. Bank names carry
     "(synthetic)", for example "Sahyadri Co-operative Bank (synthetic)" in the showcase; mobiles and PANs are
     masked (`98XXXXXX21`, `XXXXX1234X`).
   - External risk signals are synthetic stand-ins: `ExternalSignal.fri_risk` for the DoT Financial Fraud Risk
     Indicator and `mulehunter_score` for a MuleHunter-style score (`src/wapsi/models.py`).
   - CFCFRMS responses from other banks are simulated by `Ledger.consortium_transactions`.
2. **Deterministic generator.** `uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo`
   writes a Parquet world; the same seed gives byte-identical files. Sizes: `showcase` (the hand-computed fixture,
   2 cases), `demo` (2,997 accounts, 62,162 transactions, 30 rings, 43 complaints, 39 cases, 3 adversarial), and
   `full` (about 60,000 accounts and 2 million transactions). Generated worlds are gitignored.
3. **Independent ground truth.** Expected values come from `wapsi.synth.reference`, which never imports
   `wapsi.graph`, `wapsi.classify`, `wapsi.draft` or `wapsi.verify` ([ADR 0008](0008-verifier-independent-of-planner.md)). Golden sets live in
   `data/golden/<world>/cases.jsonl`; `tests/synth` fails if a committed golden file no longer matches the
   generator.
4. **Ground truth stays out of the product.** The `labels_accounts`, `labels_flows` and `labels_complaints` tables
   are read only by evaluations and classifier training, never by the pipeline (`docs/data-contract.md`).
5. **Realistic difficulty on purpose.** Ten fraud typologies, from fan-out cash-out (T1) to nodal/pool (T10);
   about 7% of complaints carry injected instructions in English or Hinglish; and deliberate noise for classifiers:
   about 15% of mules have clean-looking external signals, about 4% of legitimate accounts have medium
   MuleHunter-style scores, and some legitimate accounts share a family device (`data/golden/README.md`).
6. **Legal texts are fetched, not redistributed.** `python -m wapsi.search.fetch` downloads them into
   `data/docs/raw/` (gitignored) and records URL, size, SHA-256 and retrieval time in `data/docs/MANIFEST.md`. Only
   quotes of at most 25 words are committed (`data/docs/citations.json`), and
   `python -m wapsi.search.check_citations` checks every quote, fact and regulatory-radar quote against the
   downloaded files (121/121 passed on 2026-09-29).

## Consequences

Positive:

- **No personal data anywhere.** The repository, the demos and the cloud deployments carry no real person's data,
  so the Google Cloud edition can put its Streamlit service on a public URL for judging (`editions/gcp/README.md`).
- **Exact metrics.** Because the truth is known to the paisa, `hold_exact_match_rate`, `over_hold_inr` and
  `totals_abs_error_inr` are meaningful. On the demo world: hold exact match 1.0, over-hold ₹0.00, total absolute
  error ₹0.34 across all cases and total fields (`evals/reports/latest.json`).
- **Reproducible by anyone** with the commands above, on a laptop or in CI (`.github/workflows/ci.yml` generates
  the showcase and demo worlds and evaluates them on every push).

Negative:

- **Every metric measures agreement with Wapsi's own generator.** A score of 1.0 on the demo world shows the engine
  implements the rules consistently with an independent reference. It does not show performance on real trails.
- **Classifier results are the weakest evidence.** Held-out worlds come from the same generator, so a learned model
  can score well by learning the generator ([ADR 0006](0006-transparent-heuristic-classifier-default.md)).
- **In-house test sets flatter.** The prompt-injection guard scores precision and recall of 1.0 on the dev and test
  splits of a corpus written by the same author, but recall 0.75 (57 of 76) on a blind hold-out written by a
  separate agent (`evals/reports/guard-latest.json`).
- **Realism gaps.** Typologies, time-of-day profiles and amounts are hand-designed; CFCFRMS behaviour is simulated.
- **A pilot is required before real use.** Any bank deployment must first be evaluated on the bank's own data,
  under the bank's governance.

## Alternatives considered

| Option | Why not |
|---|---|
| Real bank data | Not available to the team; would make the repository, demos and cloud deployments unshareable |
| Anonymised real data | Not available; transaction trails are hard to anonymise without losing the structure that matters |
| Public AML datasets | No NCRP complaints, CFCFRMS semantics, merchant invoices or SOP roles. The AMD edition considers IBM AMLworld only for GNN pre-training during the event window (`editions/amd/gnn/train.py`) |
| Data written by an LLM | Not deterministic, and no exact ground truth to the paisa |

## Evidence

- Code: `src/wapsi/synth/` (`world.py`, `typologies.py`, `rings.py`, `background.py`, `reference.py`, `golden.py`),
  `src/wapsi/search/fetch.py`, `src/wapsi/search/check_citations.py`.
- Data: `data/golden/README.md`, `data/golden/demo/SUMMARY.md`, `data/docs/MANIFEST.md`, `data/docs/README.md`.
- Contract: [docs/data-contract.md](../data-contract.md).
- Tests: `tests/synth/`.
