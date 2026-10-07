# ADR 0006: A transparent heuristic classifier by default; XGBoost only on request

- **Status:** Accepted
- **Date:** 2026-09-16 (the classifier evaluation reports in `evals/reports/classify-*.json` were generated that day)
- **Related:** [0001](0001-amount-specific-holds.md), [0007](0007-synthetic-data-only.md),
  [0008](0008-verifier-independent-of-planner.md) (why the verifier cannot catch a role error)

## Context

The role of an account decides how it is treated. A `MULE` gets `HOLD_AMOUNT` and can appear in the STR. An
`INNOCENT_HOLDER` (SOP Para 5(xii), an account "operated without the knowledge or connivance or consent of the
account holder" `[SOP-5-XII]`) gets `HOLD_AMOUNT_NO_ESCALATION`, an EDD memo, no escalation and no automatic STR,
and is never named in the STR draft. Since 2026-09-28 the innocent band is split into `BONA_FIDE_RECIPIENT` (kept
the money; wire value `INNOCENT_HOLDER`) and `UNWITTING_CONDUIT` (passed at least half of the victim money it
received on), for which an STR is recommended for the officer's decision. An
`UNKNOWN` account is held and sent to the officer (`requires_human=True`, reason `ROLE_UNKNOWN`).

So the classifier has legal consequences, and the officer must be able to see why an account got its label. The
only labels available are synthetic ([ADR 0007](0007-synthetic-data-only.md)): every account's true role comes from
Wapsi's own generator.

The repository contains two scorers behind one `Scorer` protocol (`wapsi.classify.scoring`):

- `HeuristicScorer` (`heuristic-v2`): a hand-set logistic model. Each term has a published weight and a note
  (`HEURISTIC_TERMS`, printable with `describe()`); the score is clipped to [0.01, 0.99] so it never claims
  certainty.
- `XgbScorer`: a multi-class XGBoost model trained by `python -m wapsi.classify.train` on a generated world
  (optional `ml` extra), with TreeSHAP contributions for explanations.

A scorer decides the role only for accounts the hard rules (victim, intermediary, merchant) leave open, and its
answer passes through the safety guard `innocent_blockers`, which turns an innocent-holder score into `UNKNOWN`
when the account forwarded most of the money quickly, cashed it out, shares a device with the trail, woke from
dormancy or was reported repeatedly.

## What the evaluation showed

On 2026-09-16 both scorers were run through the full pipeline on the demo world (seed 7) and on five held-out worlds
generated with seeds 101, 202, 303, 404 and 505 (`data/generated/heldout-*`, same size and generator, different
seeds). The XGBoost model `xgb:demo-newfeat` was trained on the demo world, so its demo row is in-sample.

| World | Heuristic: innocent recall | Heuristic: disposition accuracy | XGBoost: innocent recall | XGBoost: disposition accuracy | XGBoost: mule recall |
|---|---:|---:|---:|---:|---:|
| demo (in-sample for XGBoost) | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| heldout-101 | 0.9 | 0.9929 | 1.0 | 1.0 | 0.9915 |
| heldout-202 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| heldout-303 | 0.75 | 0.984 | 1.0 | 1.0 | 0.9907 |
| heldout-404 | 0.8333 | 0.9929 | 1.0 | 1.0 | 1.0 |
| heldout-505 | 0.875 | 0.9907 | 0.875 | 0.9907 | 1.0 |

Source: `summary` blocks of `evals/reports/classify-after-*.json` (heuristic) and
`evals/reports/classify-xgb-newfeat-*.json` (XGBoost). The heuristic's mule recall is 1.0 in every world. Hold
amounts matched the golden set exactly in all twelve runs (`hold_exact` 1.0), because an account's role changes its
disposition, not the amount held.

Across the five held-out worlds the heuristic made 5 disposition errors and XGBoost made 1. Every one was the same
kind: an innocent holder got `HOLD_AMOUNT` instead of `HOLD_AMOUNT_NO_ESCALATION`. Re-running the five heuristic
cases on 2026-09-28 shows how they differ:

| World | Account | Role given | Effect beyond the disposition |
|---|---|---|---|
| heldout-101 | `HB01-4359960871` | `UNKNOWN` | sent to the officer (`requires_human`) |
| heldout-303 | `HB01-2514336274` | `UNKNOWN` | sent to the officer (`requires_human`) |
| heldout-505 | `HB01-2652345386` | `UNKNOWN` | sent to the officer (`requires_human`) |
| heldout-303 | `HB01-1877884517` | `MULE` | no review flag; STR ground `PROCEEDS_OF_CRIME` proposed and the STR draft names the account |
| heldout-404 | `HB01-3089895534` | `MULE` | no review flag; STR ground `PROCEEDS_OF_CRIME` proposed and the STR draft names the account |

The two `MULE` errors match the heuristic's mule precision below 1.0 on heldout-303 (0.9907) and heldout-404
(0.9918), and its `str_ground_acc` of 0.975 (39 of 40 cases) and 0.9767 (42 of 43 cases) in those worlds. Both
cases still reached status `VERIFIED`, STR draft included: the verifier's
`innocent_in_str` check reads roles from the classifier, so it cannot catch a role error
([ADR 0008](0008-verifier-independent-of-planner.md)).

So XGBoost scored slightly higher on held-out synthetic worlds. It was still kept opt-in, for three reasons.

**1. It leans on generator artifacts.** The saved model's split gains (`evals/reports/classify-xgb-demo-newfeat.json`,
computed with the command under Evidence) put 21.0% of the total gain on `is_merchant`, 18.9% on
`hist_distinct_payers_90d`, 15.2% on `out_count_24h`, 13.9% on `in_amount_24h` and 11.4% on `night_share`.

- `is_merchant` is the `MERCHANT` account type, which the hard rules already decide before any scorer runs.
- `out_count_24h` and `in_amount_24h` follow the generator's own parameters: ring sizes, hop counts and amounts
  set per typology (`data/golden/README.md`, typology table).
- `night_share` separates classes because of how time of day is generated: fraud episodes start between 09:00 and
  21:00 IST (`src/wapsi/synth/typologies.py`), while background traffic follows hand-set hour-of-day profiles
  (`src/wapsi/synth/background.py`). That is a property of the generator, not of fraud.

Held-out worlds come from the same generator with different seeds, so they reward a model for learning the
generator. They cannot show whether that skill transfers to real data.

**2. It missed mules the heuristic caught.** Mule recall of 0.9915 on heldout-101 and 0.9907 on heldout-303,
against 1.0 for the heuristic. Dispositions were unaffected in those worlds, but a missed mule is the error the
product can least afford to make silently.

**3. The heuristic can be reviewed before it is used.** Its weights are a table a compliance reviewer can read and
challenge (`HeuristicScorer.describe()`); its reasons are its own terms. XGBoost's TreeSHAP reasons explain each
prediction after the fact, but there is no equivalent table to approve in advance.

The heuristic also improved during the same session. The `classify-before-*` and `classify-after-*` reports,
all generated on 2026-09-16 between 18:09 and 18:30 UTC, bracket a revision of the heuristic. Innocent-holder recall went from 0.375–0.667 before to
0.75–1.0 after on the held-out worlds, with disposition accuracy from 0.96–0.9818 to 0.984–1.0. The current weights
include the three counterparty-history features (`hist_distinct_payers_90d`, `hist_credit_turnover_ratio`,
`known_payee_out_ratio_24h`) that also separate the XGBoost `oldfeat` and `newfeat` models.

**Update, hard world (2026-09-28).** On the 64-case hard world (`evals/reports/hard.json`), built with five
realistic confounders, the heuristic scores dispositions 0.9198 and an XGBoost model trained on another hard seed
(202) scores 0.9481; on the held-out seed 101 (`evals/reports/hard-101.json`) they score 0.9139 and 0.9569. Holds
stay exact with every scorer. The harness now compares scorers directly (`--scorer`, repeatable; see
[Evaluation](../evaluation.md#scorers-and-harness-options)). The trade-off in this ADR stands: the trained model
labels better on these worlds, but it learns the generator's labels, while the heuristic stays readable and
reviewable before use.

## Decision

1. **`HeuristicScorer` is the default.** `classify_trace(ledger, trace, cfg, model=None)` uses it when no model is
   passed, and the pipeline passes none unless the caller does.
2. **XGBoost is opt-in.** A caller passes an `XgbScorer` as `scorer=` to `process_case` (or `model=` to
   `classify_trace`). The `ml` extra is needed; nothing imports XGBoost otherwise.
3. **Any future scorer uses the same protocol** and must return additive, log-odds-like contributions per feature,
   so `wapsi.classify.explain` can render reasons. The AMD edition's GNN client follows this and falls back to the
   heuristic.
4. **Uncertainty goes to a human.** Thresholds stay at 0.6 (mule) and 0.35 (innocent), with `UNKNOWN` in between
   and the `innocent_blockers` guard after the score.

## Consequences

- Officers see reasons built from a fixed, published weights table.
- Innocent-holder recall on held-out worlds is 0.75–1.0 with the default scorer. Misclassified innocent holders are
  still held for the right amount, but lose the SOP Para 5(xii) treatment until the officer corrects the role. When
  the error is `MULE` rather than `UNKNOWN`, nothing in the pipeline flags it, and the proposed STR names the
  account; the supervisor who approves the STR is the only check. [architecture.md](../architecture.md#limitations)
  lists this as a known limitation.
- A learned scorer (the XGBoost baseline, or a GNN) does not become the core's default unless its metrics beat
  the heuristic, and metrics on synthetic worlds alone are not enough: the model must also not depend on generator
  artifacts, which in practice means evidence from data the generator did not produce.
- Revisit this decision when labelled data from outside the generator is available (a bank pilot, or public data
  with comparable labels).

## Alternatives considered

| Option | Why not (for now) |
|---|---|
| XGBoost as the default | Slightly better held-out scores, but on the generator's own features and artifacts; lower mule recall in two worlds; no weights table to review in advance |
| A GNN as the default | Planned for the AMD edition's event window; same protocol, same synthetic-label problem |
| Rules only, no score | Cannot separate a long-standing salaried account that received money from a fresh account that forwarded it; every non-rule account would be `UNKNOWN` |
| Label everything `UNKNOWN` and leave it to the officer | Safe but unhelpful: loses the SOP Para 5(xii) protection for clearly innocent holders and adds workload |

## Evidence

- Code: `src/wapsi/classify/__init__.py` (`classify_trace`), `src/wapsi/classify/scoring.py` (`HEURISTIC_TERMS`,
  `HeuristicScorer`, `XgbScorer`), `src/wapsi/classify/rules.py` (`hard_rules`, `innocent_blockers`),
  `src/wapsi/classify/train.py`.
- Reports: `evals/reports/classify-before-*.json`, `classify-after-*.json`, `classify-xgb-oldfeat-*.json`,
  `classify-xgb-newfeat-*.json`, `classify-xgb-heldout-account-metrics.json`; saved models
  `classify-xgb-demo-oldfeat.json` and `classify-xgb-demo-newfeat.json`.
- Tests: `tests/classify/test_classify_heuristic.py`, `tests/classify/test_classify_counterparty.py`,
  `tests/classify/test_classify_xgb.py`.
- Regenerate a held-out world (the 2026-09-28 re-run of the error cases used this; the golden set is written to
  `data/generated/heldout-303/golden/`):

  ```bash
  uv run python -m wapsi.synth --size demo --seed 303 --name heldout-303 --out data/generated/heldout-303 --golden-dir ''
  ```
- Reproduce the split gains (needs the `ml` extra):

  ```bash
  uv run python -c "import xgboost as xgb; b = xgb.Booster(); b.load_model('evals/reports/classify-xgb-demo-newfeat.json'); g = b.get_score(importance_type='total_gain'); t = sum(g.values()); print(sorted(((k, round(v / t, 3)) for k, v in g.items()), key=lambda kv: -kv[1])[:5])"
  ```
