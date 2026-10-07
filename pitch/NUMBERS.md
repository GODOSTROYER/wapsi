# Pitch numbers: where every figure comes from

Every number in `pitch/` traces to a file in this repository. This page maps each one to its source file and field.
`uv run python pitch/check_numbers.py` re-reads every source, compares it with the **Value** column, and fails if
any rupee amount, percentage, dollar amount or GB figure written in `pitch/**/*.md` is missing from the
**Pitched as** column below. `tests/pitch/test_pitch_pack.py` runs the same check in the test suite.

Rules for editing the pitch:
- Add the row here first, then use the number. No row, no number.
- Amounts are Indian notation (₹1,42,848). 1 crore (Cr) = ₹1,00,00,000; 1 lakh (L) = ₹1,00,000.
- **All case data is synthetic.** Official statistics are verbatim-quoted in `data/docs/facts.json` and re-checked by
  `uv run python -m wapsi.search.check_citations`.
- Draft metrics come from the deterministic stub LLM (`fake-llm`). Real-LLM rows are **TBD** until measured on each
  platform; never fill them from memory.
- Locator kinds are explained in the docstring of `pitch/check_numbers.py`.

## The problem: official statistics (`data/docs/facts.json`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| sop_reported | ₹52,969 Cr reported (Apr 2021 to Nov 2025) | 52969 | data/docs/facts.json | `fact:CFCFRMS_AMOUNT_REPORTED` |
| sop_held | ₹7,647 Cr held; ₹7,647 crore | 7647 | data/docs/facts.json | `fact:CFCFRMS_AMOUNT_SAVED` |
| sop_restored | ₹167 Cr restored; ₹167 crore | 167 | data/docs/facts.json | `fact:CFCFRMS_AMOUNT_RESTORED` |
| sop_restored_pct | 2.18% of the money held | 2.18 | data/docs/facts.json | `fact:CFCFRMS_RESTORED_SHARE_OF_SAVED` |
| sop_restored_pct_check | 2.18% = 167 / 7,647 | 2.18 | derived | `calc:100 * sop_restored / sop_held` |
| saved_2026_06 | more than ₹11,158 Cr saved to 30.06.2026; ₹11,158 crore | 11158 | data/docs/facts.json | `fact:CFCFRMS_SAVED_TILL_2026_06` |
| complaints_2026_06 | 32.80 lakh complaints | 32.80 | data/docs/facts.json | `fact:CFCFRMS_COMPLAINTS_TILL_2026_06` |
| entities_2026_06 | 1,586 entities on CFCFRMS | 1586 | data/docs/facts.json | `fact:CFCFRMS_ENTITIES_ONBOARDED_2026_06` |
| foreign_banks | 40 foreign banks among them | 40 | data/docs/facts.json | `text:including 40 foreign banks operating in India` |
| l1_mules | 32.08 lakh Layer 1 mule accounts shared | 32.08 | data/docs/facts.json | `fact:SUSPECT_REGISTRY_L1_MULES_SHARED_2026_06` |
| declined | ₹25,698 Cr of transactions declined; ₹25,698 crore | 25698 | data/docs/facts.json | `fact:SUSPECT_REGISTRY_DECLINED_2026_06` |
| str_days | STR within 7 working days (FIU-IND FAQ) | 7 | data/docs/facts.json | `fact:STR_DUE_WORKING_DAYS` |
| hold_lapse_days | hold may lapse after 90 calendar days | 90 | data/docs/facts.json | `fact:HOLD_LAPSE_DAYS_ANNEX_I` |
| p1_threshold | interim custody above ₹50,000 | 50000 | data/docs/facts.json | `fact:P1_THRESHOLD_INR` |
| annex_v_balance | SOP Annexure-V worked example, ₹2,06,543 over five victims | 206543 | data/docs/facts.json | `fact:ANNEX_V_ILL2_BALANCE_INR` |
| rbi_draft_max_days | RBI DRAFT: 60-day maximum bank-initiated hold | 60 | data/docs/reg_radar.json | `text:is 60 days from the date of temporary debit hold` |
| rbi_draft_floor | RBI DRAFT: ₹1,000 transaction floor | 1000 | data/docs/reg_radar.json | `text:₹ 1000 and above` |

## Showcase case 1: hand-computed (`src/wapsi/fixtures/showcase.py`)

One victim, one impersonation call, ₹4,80,000 stolen. These are the numbers every module and every edition must reproduce.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| sc1_stolen | ₹4,80,000 stolen | 480000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["totals"]["stolen"]` |
| sc1_hold_a | A ₹43,200; ₹43,200.00 | 43200 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["hold"][A]` |
| sc1_hold_b | B ₹27,648 | 27648 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["hold"][B]` |
| sc1_hold_d | D ₹48,000 | 48000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["hold"][D]` |
| sc1_hold_l | L (landlord, innocent holder) ₹4,800 | 4800 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["hold"][L]` |
| sc1_hold_p | P (payment-aggregator pool) ₹19,200 | 19200 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["hold"][P]` |
| sc1_total_hold | ₹1,42,848 held | 142848 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["total_hold"]` |
| sc1_held_accounts | held in 5 accounts | 5 | src/wapsi/fixtures/showcase.py | `py:positive(SHOWCASE_1["hold"])` |
| sc1_forward | ₹96,000 forward hold on A | 96000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["total_forward_hold"]` |
| sc1_outbound | ₹1,92,000 requested from the other bank (C ₹48,000 + E ₹1,44,000) | 192000 | src/wapsi/fixtures/showcase.py | `py:sum(SHOWCASE_1["outbound_request"].values())` |
| sc1_outbound_e | E ₹1,44,000 | 144000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["outbound_request"][E]` |
| sc1_cashed_out | ₹1,44,000 cashed out at ATMs | 144000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["totals"]["cashed_out"]` |
| sc1_recoverable | ₹3,34,848 recoverable | 334848 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["total_recoverable"]` |
| sc1_recoverable_pct | 69.76% of the stolen money recoverable | 69.76 | derived | `calc:100 * sc1_recoverable / sc1_stolen` |
| sc1_kirana | kirana shop ₹1,152, goods delivered, no hold | 1152 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["tainted_now"][K]` |
| sc1_pool_balance | pool account balance ₹5,00,20,000 keeps running | 50020000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["balance_now"][P]` |
| sc1_baseline | whole-account freeze baseline ₹5,03,50,000 | 50350000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["baseline_freeze_amount"]` |
| sc1_baseline_accounts | in 6 accounts | 6 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["baseline_freeze_accounts"]` |
| sc1_baseline_cr | more than ₹5 Cr (spoken rounding of ₹5,03,50,000) | 5 | derived | `calc:sc1_baseline / 10000000` |
| sc1_multiple | the freeze locks 352× what Wapsi holds | 352 | derived | `calc:sc1_baseline / sc1_total_hold` |
| sc1_over_hold | over-hold ₹0 | 0 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["over_hold_amount"]` |
| sc2_hold_m | case 2: ₹1,20,000 held for two victims | 120000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_2["hold"][M]` |
| sc2_victim_n2 | ₹75,000 for victim N2 | 75000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_2["per_victim_recoverable"][N2]` |
| sc2_victim_n3 | ₹45,000 for victim N3 | 45000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_2["per_victim_recoverable"][N3]` |
| tamper_to | tamper test: ₹43,200.00 changed to ₹45,000.00 | 45000 | .claude/skills/str-drafter/SKILL.md | `text:--tamper "₹43,200.00" "₹45,000.00"` |

## Showcase world: both hand-computed cases through the harness (`evals/reports/showcase.json`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| sw_cases | 2 cases, 3 victims | 2 | evals/reports/showcase.json | `json:cases` |
| sw_freeze | ₹5,04,70,000 (₹5.05 Cr) whole-account freeze baseline | 50470000 | evals/reports/showcase.json | `json:baseline.whole_account_freeze_inr` |
| sw_freeze_cr | ₹5.05 Cr | 5.05 | derived | `calc:sw_freeze / 10000000` |
| sw_freeze_accounts | in 7 accounts | 7 | evals/reports/showcase.json | `json:baseline.accounts_frozen_baseline` |
| sw_hold | ₹2,62,848 held by Wapsi | 262848 | evals/reports/showcase.json | `json:baseline.wapsi_hold_inr` |
| sw_hold_accounts | in 6 accounts | 6 | evals/reports/showcase.json | `json:baseline.accounts_held` |
| sw_pct | 0.52% of the freeze | 0.52 | derived | `calc:100 * sw_hold / sw_freeze` |
| sw_hold_exact | holds exact 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.hold_exact_match_rate` |
| sw_over_hold | over-hold ₹0 | 0.0 | evals/reports/showcase.json | `json:metrics.over_hold_inr` |
| sw_false_freeze | 0 false freezes | 0 | evals/reports/showcase.json | `json:metrics.false_freeze_count` |
| sw_disposition | dispositions 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.disposition_accuracy` |
| sw_str_ground | STR ground 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.str_ground_accuracy` |
| sw_ih_recall | innocent-holder recall 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.role_recall.INNOCENT_HOLDER` |
| sw_draft_pass | drafts passing the verifier 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.draft_verification_pass_rate` |
| sw_citation | citation validity 1.0 | 1.0 | evals/reports/showcase.json | `json:metrics.citation_validity` |
| sw_leaks | injection leaks 0 | 0 | evals/reports/showcase.json | `json:metrics.injection_leaks` |

## Demo world: 39 synthetic cases, 10 typologies (`evals/reports/demo.json`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| demo_cases | 39 cases | 39 | evals/reports/demo.json | `json:cases` |
| demo_adversarial | 3 adversarial (prompt-injection) cases | 3 | evals/reports/demo.json | `json:adversarial_cases` |
| demo_typologies | 10 fraud typologies | 10 | evals/reports/demo.json | `len:per_typology` |
| demo_freeze | ₹35,25,90,069.78 (₹35.26 Cr; ₹35.26 crore) whole-account freeze baseline | 352590069.78 | evals/reports/demo.json | `json:baseline.whole_account_freeze_inr` |
| demo_freeze_cr | ₹35.26 Cr | 35.26 | derived | `calc:demo_freeze / 10000000` |
| demo_freeze_accounts | in 101 accounts | 101 | evals/reports/demo.json | `json:baseline.accounts_frozen_baseline` |
| demo_hold | ₹29,19,245.69 (₹29.19 L; ₹29.19 lakh) held by Wapsi | 2919245.69 | evals/reports/demo.json | `json:baseline.wapsi_hold_inr` |
| demo_hold_l | ₹29.19 L | 29.19 | derived | `calc:demo_hold / 100000` |
| demo_hold_accounts | in 84 accounts | 84 | evals/reports/demo.json | `json:baseline.accounts_held` |
| demo_pct | 0.83% of the freeze | 0.83 | derived | `calc:100 * demo_hold / demo_freeze` |
| demo_released | ₹34.97 Cr of non-victim money left free | 34.97 | derived | `calc:(demo_freeze - demo_hold) / 10000000` |
| demo_hold_exact | holds exact to the paisa 1.0 (100%) | 1.0 | evals/reports/demo.json | `json:metrics.hold_exact_match_rate` |
| demo_over_hold | over-hold ₹0 | 0.0 | evals/reports/demo.json | `json:metrics.over_hold_inr` |
| demo_false_freeze | 0 false freezes | 0 | evals/reports/demo.json | `json:metrics.false_freeze_count` |
| demo_traced | traced-amount recall 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.traced_amount_recall` |
| demo_disposition | dispositions 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.disposition_accuracy` |
| demo_str_ground | STR ground 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.str_ground_accuracy` |
| demo_ih_recall | innocent-holder recall 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.role_recall.INNOCENT_HOLDER` |
| demo_draft_pass | drafts passing the verifier 1.0 (100%) | 1.0 | evals/reports/demo.json | `json:metrics.draft_verification_pass_rate` |
| demo_numeric | draft numeric accuracy 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.draft_numeric_accuracy` |
| demo_citation | citation validity 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.citation_validity` |
| demo_injection_recall | injection flag recall 1.0 (3 of 3) | 1.0 | evals/reports/demo.json | `json:metrics.injection_flag_recall` |
| demo_leaks | injection leaks 0 | 0 | evals/reports/demo.json | `json:metrics.injection_leaks` |
| demo_prose_fallback | prose fallback 0.0 with a well-behaved LLM | 0.0 | evals/reports/demo.json | `json:metrics.llm_prose_fallback_rate` |
| demo_totals_error | totals rounding error ₹0.34 over 39 cases × 7 totals | 0.34 | evals/reports/demo.json | `json:metrics.totals_abs_error_inr` |
| demo_p95_total | p95 0.72 s per case, trace to verified drafts (laptop, stub LLM) | 0.72 | evals/reports/demo.json | `json:metrics.p95_total_s` |

## Hallucination stress test: stub LLM writes a wrong rupee amount into every prose field (`evals/reports/baseline/demo-fake-hallucinate.json`)

Command: `uv run python -m evals.harness --world demo --llm fake-hallucinate --out evals/reports/baseline --report-name demo-fake-hallucinate`.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| hall_fallback | 27.57% of drafts had their prose rejected and replaced by fixed text | 0.2757 | evals/reports/baseline/demo-fake-hallucinate.json | `json:metrics.llm_prose_fallback_rate` |
| hall_draft_pass | drafts passing the verifier still 1.0 (100%) | 1.0 | evals/reports/baseline/demo-fake-hallucinate.json | `json:metrics.draft_verification_pass_rate` |
| hall_numeric | draft numeric accuracy still 1.0 | 1.0 | evals/reports/baseline/demo-fake-hallucinate.json | `json:metrics.draft_numeric_accuracy` |
| hall_hold_exact | holds still exact 1.0 | 1.0 | evals/reports/baseline/demo-fake-hallucinate.json | `json:metrics.hold_exact_match_rate` |

## Hard world: 15 typologies incl. 5 realistic confounders, where Wapsi is not perfect (`evals/reports/hard.json`, `evals/reports/hard-101.json`)

`hard` is seed 7 and `hard-101` is seed 101 of `uv run python -m wapsi.synth --size hard`. The GNN rows are a
**CPU development checkpoint** (`gnn:cpu-dev-base`), not an MI300X result.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| hard_cases | 64 cases | 64 | evals/reports/hard.json | `json:cases` |
| hard_adversarial | 5 adversarial cases | 5 | evals/reports/hard.json | `json:adversarial_cases` |
| hard_typologies | 15 typologies | 15 | evals/reports/hard.json | `len:per_typology` |
| hard_accounts | 200 scored accounts | 200 | evals/reports/hard.json | `json:classification.accounts` |
| hard_hold_exact | holds exact 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.hold_exact_match_rate` |
| hard_over_hold | over-hold ₹0 | 0.0 | evals/reports/hard.json | `json:metrics.over_hold_inr` |
| hard_false_freeze | 0 false freezes | 0 | evals/reports/hard.json | `json:metrics.false_freeze_count` |
| hard_disposition | dispositions 0.9198 (91.98%; spoken "0.92" or "92%") | 0.9198 | evals/reports/hard.json | `json:metrics.disposition_accuracy` |
| hard_str_ground | STR ground 0.875 (87.5%) | 0.875 | evals/reports/hard.json | `json:metrics.str_ground_accuracy` |
| hard_ih_recall | innocent-holder recall 0.6875 | 0.6875 | evals/reports/hard.json | `json:metrics.role_recall.INNOCENT_HOLDER` |
| hard_ih_precision | innocent-holder precision 0.9429 | 0.9429 | evals/reports/hard.json | `json:metrics.role_precision.INNOCENT_HOLDER` |
| hard_mule_precision | mule precision 0.8987 | 0.8987 | evals/reports/hard.json | `json:metrics.role_precision.MULE` |
| hard_mule_recall | mule recall 0.966 | 0.966 | evals/reports/hard.json | `json:metrics.role_recall.MULE` |
| hard_mule_auc | mule ROC-AUC 0.9411 (heuristic) | 0.9411 | evals/reports/hard.json | `json:classification.mule_auc` |
| hard_unknown | 3.5% of accounts sent to the officer as UNKNOWN | 0.035 | evals/reports/hard.json | `json:classification.unknown_rate` |
| hard_draft_pass | drafts passing the verifier 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.draft_verification_pass_rate` |
| hard_citation | citation validity 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.citation_validity` |
| hard_leaks | injection leaks 0 | 0 | evals/reports/hard.json | `json:metrics.injection_leaks` |
| hard_freeze | ₹39,55,06,565.52 (₹39.55 Cr) whole-account freeze baseline | 395506565.52 | evals/reports/hard.json | `json:baseline.whole_account_freeze_inr` |
| hard_freeze_cr | ₹39.55 Cr | 39.55 | derived | `calc:hard_freeze / 10000000` |
| hard_freeze_accounts | in 174 accounts | 174 | evals/reports/hard.json | `json:baseline.accounts_frozen_baseline` |
| hard_hold | ₹70,72,636.72 (₹70.73 L) held by Wapsi | 7072636.72 | evals/reports/hard.json | `json:baseline.wapsi_hold_inr` |
| hard_hold_l | ₹70.73 L | 70.73 | derived | `calc:hard_hold / 100000` |
| hard_hold_accounts | in 160 accounts | 160 | evals/reports/hard.json | `json:baseline.accounts_held` |
| hard_pct | 1.79% of the freeze | 1.79 | derived | `calc:100 * hard_hold / hard_freeze` |
| hard_xgb_disposition | XGBoost trained on hard-202: dispositions 0.9481 | 0.9481 | evals/reports/hard.json | `json:by_scorer.xgb:xgb_hard202.disposition_accuracy` |
| hard_gnn_disposition | GNN (CPU dev): dispositions 0.9151 | 0.9151 | evals/reports/hard.json | `json:by_scorer.gnn:cpu-dev-base.disposition_accuracy` |
| hard_gnn_auc | GNN (CPU dev): mule ROC-AUC 0.9786 | 0.9786 | evals/reports/hard.json | `json:by_scorer.gnn:cpu-dev-base.classification.mule_auc` |
| hard_gnn_ih_recall | GNN (CPU dev): innocent-holder recall 0.875 | 0.875 | evals/reports/hard.json | `json:by_scorer.gnn:cpu-dev-base.role_recall.INNOCENT_HOLDER` |
| hard_gnn_mule_recall | GNN (CPU dev): mule recall 0.7415 | 0.7415 | evals/reports/hard.json | `json:by_scorer.gnn:cpu-dev-base.role_recall.MULE` |
| hard_gnn_unknown | GNN (CPU dev): 18% sent to the officer as UNKNOWN | 0.18 | evals/reports/hard.json | `json:by_scorer.gnn:cpu-dev-base.classification.unknown_rate` |
| h101_cases | hard-101: 62 cases | 62 | evals/reports/hard-101.json | `json:cases` |
| h101_hold_exact | hard-101: holds exact 1.0 | 1.0 | evals/reports/hard-101.json | `json:metrics.hold_exact_match_rate` |
| h101_false_freeze | hard-101: 0 false freezes | 0 | evals/reports/hard-101.json | `json:metrics.false_freeze_count` |
| h101_over_hold | hard-101: over-hold ₹0 | 0.0 | evals/reports/hard-101.json | `json:metrics.over_hold_inr` |
| h101_draft_pass | hard-101: drafts passing the verifier 1.0 | 1.0 | evals/reports/hard-101.json | `json:metrics.draft_verification_pass_rate` |
| h101_citation | hard-101: citation validity 1.0 | 1.0 | evals/reports/hard-101.json | `json:metrics.citation_validity` |
| h101_leaks | hard-101: injection leaks 0 | 0 | evals/reports/hard-101.json | `json:metrics.injection_leaks` |
| h101_disposition | hard-101: dispositions 0.9139 | 0.9139 | evals/reports/hard-101.json | `json:metrics.disposition_accuracy` |
| h101_str_ground | hard-101: STR ground 0.9032 | 0.9032 | evals/reports/hard-101.json | `json:metrics.str_ground_accuracy` |
| h101_ih_recall | hard-101: innocent-holder recall 0.68 | 0.68 | evals/reports/hard-101.json | `json:metrics.role_recall.INNOCENT_HOLDER` |
| h101_xgb_disposition | hard-101: XGBoost (hard-202) dispositions 0.9569 | 0.9569 | evals/reports/hard-101.json | `json:by_scorer.xgb:xgb_hard202.disposition_accuracy` |
| h101_gnn_disposition | hard-101: GNN (CPU dev) dispositions 0.9378 | 0.9378 | evals/reports/hard-101.json | `json:by_scorer.gnn:cpu-dev-base.disposition_accuracy` |

## Prompt-injection guard (`evals/reports/guard-latest.json`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| guard_blind_n | blind holdout of 151 texts by a separate author | 151 | evals/reports/guard-latest.json | `json:blind_holdout.n` |
| guard_blind_precision | blind-holdout precision 0.9661 | 0.9661 | evals/reports/guard-latest.json | `json:blind_holdout.precision` |
| guard_blind_recall | blind-holdout recall 0.75 | 0.75 | evals/reports/guard-latest.json | `json:blind_holdout.recall` |
| guard_blind_fpr | blind-holdout false-positive rate 2.67% | 0.0267 | evals/reports/guard-latest.json | `json:blind_holdout.false_positive_rate` |
| guard_test_recall | same-author test split recall 1.0 | 1.0 | evals/reports/guard-latest.json | `json:test.recall` |

## Legal spine and controls (registries in the repo)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| citations_n | 35 citable ids in the registry | 35 | data/docs/citations.json | `len:` |
| facts_n | 33 pinned facts | 33 | data/docs/facts.json | `len:facts` |
| radar_rows | 11 regulatory-radar rows | 11 | data/docs/reg_radar.json | `len:clauses` |
| radar_sources | 10 radar sources | 10 | data/docs/reg_radar.json | `len:sources` |
| lea_fields | 8-field police / LEA request checklist | 8 | src/wapsi/verify/lea_request.py | `py:len(LeaField)` |
| account_level_reasons | 4 recorded reasons for an account-level exception | 4 | src/wapsi/models.py | `py:len(AccountLevelReason)` |
| skills_n | 6 Claude Code skills | 6 | .claude/skills | `glob:.claude/skills/*/SKILL.md` |

## Google Cloud edition (`editions/gcp/`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| gcp_tools | ADK agent with 10 pipeline tools | 10 | editions/gcp/architecture.md | `text:10 pipeline tools` |
| gcp_cost | expected cost at demo scale $0 on the free trial | 0 | editions/gcp/README.md | `text:Expected cost at demo scale is **$0**` |

## AMD edition: memory plan and budget (config in `editions/amd/`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| mi300x_gb | one MI300X, 192 GB HBM3 | 192 | editions/amd/docker-compose.yml | `text:ONE AMD Instinct MI300X (192 GB)` |
| llm_util | drafting model gets 0.55 of GPU memory | 0.55 | editions/amd/.env.example | `text:VLLM_GPU_UTIL=0.55` |
| verifier_util | verifier model gets 0.15 | 0.15 | editions/amd/.env.example | `text:VERIFIER_GPU_UTIL=0.15` |
| weights_gb | Gemma 4 31B in BF16 ≈ 62 GB of weights | 62 | editions/amd/.env.example | `text:62 GB bf16 weights` |
| weights_check | 62 GB = 31B parameters × 2 bytes | 62 | derived | `calc:31 * 2` |
| llm_pool_gb | 105.6 GB for the drafting model | 105.6 | derived | `calc:mi300x_gb * llm_util` |
| kv_gb | 43.6 GB left for its KV cache | 43.6 | derived | `calc:mi300x_gb * llm_util - weights_gb` |
| verifier_pool_gb | 28.8 GB for the verifier | 28.8 | derived | `calc:mi300x_gb * verifier_util` |
| free_gb | 57.6 GB left for the GNN and headroom | 57.6 | derived | `calc:mi300x_gb * (1 - llm_util - verifier_util)` |
| llm_ctx | 16384-token context for drafting | 16384 | editions/amd/.env.example | `text:VLLM_MAX_MODEL_LEN=16384` |
| gnn_params | GNN of 78k parameters | 78 | editions/amd/gnn/README.md | `text:(78k parameters)` |
| gpu_hour | $1.99 per GPU-hour | 1.99 | editions/amd/RUNBOOK.md | `text:$1.99 per GPU-hour` |
| gpu_budget | GPU budget cap $99.50 | 99.50 | editions/amd/RUNBOOK.md | `text:**$99.50**` |

## Command outputs (checked by hand on 29-09-2026; re-run before quoting)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| cmd_citations | 121/121 citation, fact and radar quote checks pass | 121 | src/wapsi/search/check_citations.py | `cmd:uv run python -m wapsi.search.check_citations` |
| cmd_radar_quotes | 52/52 radar quotes verified | 52 | src/wapsi/search/radar.py | `cmd:uv run python -m wapsi.search.radar verify` |
| cmd_drill | 6/6 tampering attempts detected (plus an in-store edit), 40-event chain | 6 | src/wapsi/audit/drill.py | `cmd:uv run python -m wapsi.audit drill` |
| cmd_policy | 48/48 policy re-checks pass on showcase case 1 | 48 | .claude/skills/lien-calculator/scripts/hold_plan.py | `cmd:uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory` |
| cmd_str_slots | 78 numbers in the STR recomputed by the verifier | 78 | .claude/skills/str-drafter/scripts/draft_and_verify.py | `cmd:uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory` |
| cmd_tests | 1,623 tests pass, 0 fail (full suite, 07-10-2026) | 1623 | pyproject.toml | `cmd:uv run pytest -q` |
