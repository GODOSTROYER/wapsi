# Pitch numbers: where every figure comes from

Every number in `pitch/` traces to a file in this repository. This page maps each one to its source file and field.
`uv run python pitch/check_numbers.py` re-reads every source, compares it with the **Value** column, and fails if
any rupee amount, percentage, dollar amount or GB figure written in `pitch/**/*.md`, `README.md` or `docs/index.md`
is missing from the **Pitched as** column below. `tests/pitch/test_pitch_pack.py` runs the same check in the test suite.

Rules for editing the pitch:
- Add the row here first, then use the number. No row, no number.
- Amounts are Indian notation (₹1,42,848). 1 crore (Cr) = ₹1,00,00,000; 1 lakh (L) = ₹1,00,000.
- **All case data is synthetic.** Official statistics are verbatim-quoted in `data/docs/facts.json` and re-checked by
  `uv run python -m wapsi.search.check_citations`.
- Draft metrics come from the deterministic stub LLM (`fake-llm`) unless a row says otherwise.
- **AMD measurements are slots until measured.** A figure that has not been measured on AMD-served models or AMD
  hardware is written `TBD(slot_id)` in the pitch, and the slot is listed in
  [AMD measurement slots](#amd-measurement-slots-open-not-claims) at the end of this page. A slot is not a claim:
  it carries no value. Fill it only from a results file committed to this repository, then move it into a
  numbers table with a locator. Never fill a slot from memory, from a CPU run or from a rehearsal.
- Every AMD row states where it ran: **AMD Token Factory** (models served on AMD GPUs by AMD's API), an **AMD GPU
  notebook** or an **MI300X droplet**. A Token Factory result is not an MI300X benchmark and is never worded as one.
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
| sc1_pool_free | ₹5,00,00,800 of the pool account stays free | 50000800 | derived | `calc:sc1_pool_balance - sc1_hold_p` |
| sc1_kirana_balance | kirana shop balance ₹51,200, all of it frozen under the baseline | 51200 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["balance_now"][K]` |
| sc1_kirana_sale | the kirana shop sold ₹1,200 of groceries to a mule | 1200 | src/wapsi/fixtures/showcase.py | `text:_t("TX-007", _ist(9, 10, 11, 40), B, K, 1200.0, Channel.UPI, "622210000007", "order 5521"),` |
| sc1_landlord_balance | landlord balance ₹1,55,000, all of it frozen under the baseline | 155000 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["balance_now"][L]` |
| sc1_landlord_rent | the landlord received ₹5,000 of rent from a mule | 5000 | src/wapsi/fixtures/showcase.py | `text:_t("TX-003", _ist(9, 10, 11, 15), A, L, 5000.0, Channel.UPI, "622210000003", "rent Sept"),` |
| sc1_atm_each | each of the first mule's two ATM withdrawals is ₹50,000 | 50000 | src/wapsi/fixtures/showcase.py | `text:_t("TX-004", _ist(9, 10, 11, 20), A, None, 50000.0, Channel.ATM, "ATM000000004", label="ATM PUNE-SHIVAJINAGAR-07"),` |
| sc1_atm_two | the first mule withdraws ₹1,00,000 in two ATM withdrawals | 100000 | derived | `calc:2 * sc1_atm_each` |
| sc1_pool_payment | the second mule pays ₹20,000 into the payment-aggregator pool | 20000 | src/wapsi/fixtures/showcase.py | `text:_t("TX-008", _ist(9, 10, 11, 45), B, P, 20000.0, Channel.UPI, "622210000008", "wallet topup"),` |
| sc1_b_to_d | the second mule passes ₹2,50,000 to the third | 250000 | src/wapsi/fixtures/showcase.py | `text:_t("TX-009", _ist(9, 10, 12, 5), B, D, 250000.0, Channel.IMPS, "425310000009"),` |
| sc1_d_to_e | the third mule sends ₹2,00,000 to the other bank | 200000 | src/wapsi/fixtures/showcase.py | `text:_t("TX-010", _ist(9, 10, 12, 30), D, E, 200000.0, Channel.IMPS, "425310000010"),` |
| sc1_mule_balances | three mule accounts hold ₹1,23,800 between them | 123800 | src/wapsi/fixtures/showcase.py | `py:SHOWCASE_1["balance_now"][A] + SHOWCASE_1["balance_now"][B] + SHOWCASE_1["balance_now"][D]` |
| sc1_honest_frozen | ₹5,02,26,200 of the frozen money sits in three honest accounts (landlord, kirana shop, payment-aggregator pool) | 50226200 | derived | `calc:sc1_kirana_balance + sc1_landlord_balance + sc1_pool_balance` |
| sc1_honest_share | 99.75% of what the freeze locks belongs to people who did nothing wrong | 99.75 | derived | `calc:100 * sc1_honest_frozen / sc1_baseline` |
| sc1_honest_held | Wapsi holds ₹24,000 in those three accounts, the victim's money only | 24000 | derived | `calc:sc1_hold_l + sc1_hold_p` |
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
| demo_p95_plan | p95 0.35 s from complaint to hold plan (laptop; no language model on that path) | 0.349 | evals/reports/demo.json | `json:metrics.p95_time_to_hold_plan_s` |
| demo_spared_accounts | 17 accounts a freeze would lock get no hold at all | 17 | derived | `calc:demo_freeze_accounts - demo_hold_accounts` |
| demo_scored_accounts | 106 accounts scored by the classifier | 106 | evals/reports/demo.json | `json:classification.accounts` |
| demo_merchant_precision | merchant precision 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.role_precision.MERCHANT` |
| demo_merchant_recall | merchant recall 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.role_recall.MERCHANT` |
| demo_process | restoration process (P1 or P2) chosen correctly 1.0 | 1.0 | evals/reports/demo.json | `json:metrics.process_accuracy` |

## Demo world: where the freeze baseline sits (`evals/reports/demo-baseline-split.json`)

The same world, code and seed as `demo.json`, re-run on 14-10-2026 with the stub LLM so the report stores the split
by account group (`baseline.by_group`: nodal / pool / escrow by account type, else the role the plan decided). Its
totals equal `demo.json`'s to the paisa; only its timings differ (another load on the laptop), and no timing is
quoted from it. Command: `uv run python -m evals.harness --world demo --report-name demo-baseline-split`.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| split_total_check | the split adds up to the same ₹35.26 Cr baseline | 352590069.78 | evals/reports/demo-baseline-split.json | `json:baseline.whole_account_freeze_inr` |
| split_pool_accounts | 3 nodal / pool accounts | 3 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.nodal_pool_escrow.accounts` |
| split_pool_freeze | the three nodal / pool accounts: ₹34,16,18,600.45 (₹34.16 Cr) of the freeze | 341618600.45 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.nodal_pool_escrow.whole_account_freeze_inr` |
| split_pool_share | 96.9% of the freeze baseline sits in the three nodal / pool accounts | 96.9 | derived | `calc:100 * split_pool_freeze / demo_freeze` |
| split_pool_hold | Wapsi holds ₹1,71,334.53 in them, the disputed amount only (SOP 9.1(iii)) | 171334.53 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.nodal_pool_escrow.wapsi_hold_inr` |
| split_merchant_accounts | 10 merchants | 10 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.merchant.accounts` |
| split_merchant_freeze | merchants: ₹62,15,805.14 frozen under the baseline | 6215805.14 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.merchant.whole_account_freeze_inr` |
| split_merchant_hold | merchants: ₹0 held (goods delivered, update only) | 0.0 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.merchant.wapsi_hold_inr` |
| split_ih_accounts | 10 innocent holders | 10 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.innocent_holder.accounts` |
| split_ih_freeze | innocent holders: ₹20,83,541.24 frozen under the baseline | 2083541.24 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.innocent_holder.whole_account_freeze_inr` |
| split_ih_hold | innocent holders: ₹2,50,110.97 held, the victim money only | 250110.97 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.innocent_holder.wapsi_hold_inr` |
| split_mule_freeze | mules: ₹26,72,122.95 frozen under the baseline | 2672122.95 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.mule.whole_account_freeze_inr` |
| split_mule_hold | mules: ₹24,97,800.19 held | 2497800.19 | evals/reports/demo-baseline-split.json | `json:baseline.by_group.mule.wapsi_hold_inr` |
| split_honest_freeze | honest merchants and customers: ₹82,99,346.38 would be frozen | 8299346.38 | derived | `calc:split_merchant_freeze + split_ih_freeze` |
| split_honest_freeze_l | ₹82.99 L (₹82.99 lakh) | 82.99 | derived | `calc:split_honest_freeze / 100000` |
| split_honest_hold | Wapsi holds ₹2,50,110.97 of it | 250110.97 | derived | `calc:split_merchant_hold + split_ih_hold` |
| split_honest_hold_l | ₹2.50 L (₹2.50 lakh) | 2.50 | derived | `calc:split_honest_hold / 100000` |
| split_honest_pct | 3.0% of what the freeze would lock on honest merchants and customers | 3.0 | derived | `calc:100 * split_honest_hold / split_honest_freeze` |
| split_nonpool_freeze | without the pool accounts the freeze is ₹1,09,71,469.33 (₹1.10 Cr) | 10971469.33 | derived | `calc:demo_freeze - split_pool_freeze` |
| split_nonpool_freeze_cr | ₹1.10 Cr | 1.10 | derived | `calc:split_nonpool_freeze / 10000000` |
| split_nonpool_hold | against ₹27,47,911.16 (₹27.48 L) held | 2747911.16 | derived | `calc:demo_hold - split_pool_hold` |
| split_nonpool_hold_l | ₹27.48 L | 27.48 | derived | `calc:split_nonpool_hold / 100000` |
| split_nonpool_pct | 25.0% of the freeze outside the pool accounts | 25.0 | derived | `calc:100 * split_nonpool_hold / split_nonpool_freeze` |

## Five held-out demo-size worlds, unseen seeds (`evals/reports/classify-after-heldout-*.json`)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| heldout_ih_recall_min | innocent-holder recall 75% to 100% across the five held-out worlds (lowest, seed 303) | 0.75 | evals/reports/classify-after-heldout-303.json | `json:report.metrics.role_recall.INNOCENT_HOLDER` |
| heldout_ih_recall_max | innocent-holder recall up to 100% (seed 202) | 1.0 | evals/reports/classify-after-heldout-202.json | `json:report.metrics.role_recall.INNOCENT_HOLDER` |
| heldout_disp_min | disposition accuracy 98.4% to 100% (lowest, seed 303) | 0.984 | evals/reports/classify-after-heldout-303.json | `json:report.metrics.disposition_accuracy` |
| heldout_disp_max | disposition accuracy up to 100% (seed 202) | 1.0 | evals/reports/classify-after-heldout-202.json | `json:report.metrics.disposition_accuracy` |

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
| hard_merchant_precision | merchant precision 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.role_precision.MERCHANT` |
| hard_merchant_recall | merchant recall 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.role_recall.MERCHANT` |
| hard_pool_recall | nodal / pool / escrow recall 1.0 | 1.0 | evals/reports/hard.json | `json:metrics.role_recall.INTERMEDIARY` |
| hard_spared_accounts | 14 accounts a freeze would lock get no hold at all | 14 | derived | `calc:hard_freeze_accounts - hard_hold_accounts` |
| hard_p95_plan | p95 0.50 s from complaint to hold plan on the hard world (laptop) | 0.50 | evals/reports/hard.json | `json:metrics.p95_time_to_hold_plan_s` |
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
| scorer_terms | transparent logistic scorer with 27 hand-set terms | 27 | src/wapsi/classify/scoring.py | `py:len(HEURISTIC_TERMS)` |
| scorer_features | over 33 behavioural features | 33 | src/wapsi/classify/features.py | `py:len(FEATURE_COLUMNS)` |
| scorer_mule_threshold | score at or above 0.6 is a mule | 0.6 | src/wapsi/config.py | `text:mule_threshold: float = 0.6` |
| scorer_innocent_threshold | score at or below 0.35 is an innocent holder | 0.35 | src/wapsi/config.py | `text:innocent_threshold: float = 0.35` |
## The Google ADK agent (`editions/shared/tools.py`)

The agent is built with Google's open-source Agent Development Kit. Its tools are the pipeline's own functions
(`OFFICER_TOOLS`, shared by the AMD entry and the optional Google Cloud app); the model behind it is a setting
(Gemma 4 served on AMD for this entry).

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| gcp_tools | ADK agent with 10 pipeline tools | 10 | editions/shared/tools.py | `py:len(OFFICER_TOOLS)` |

## Voice intake samples and the recorded agent run (`data/samples/voice/`, `src/wapsi/agenttrace/`)

The voice counts are the bundled samples' expected reconciliation with the offline model (the tests in
`tests/intake` check each one). With a live speech and extraction model, the counts are whatever the screen shows;
those are slots below until measured. The trace figures come from the recorded offline run (real ADK app, tools and
guards; scripted model).

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| voice_samples | six bundled synthetic voice notes | 6 | data/samples/voice/manifest.json | `len:samples` |
| voice_meera_matched | Meera Joshi's note: 9 of 9 claims match NCRP 31009260000101 | 9 | data/samples/voice/manifest.json | `json:samples.0.expect.counts.MATCHED` |
| voice_meera_seconds | a 31-second voice note | 31 | data/samples/voice/manifest.json | `json:samples.0.duration_s` |
| voice_fatima_matched | Fatima Shaikh's note: 8 of 8 claims match NCRP 31109260000103 | 8 | data/samples/voice/manifest.json | `json:samples.2.expect.counts.MATCHED` |
| voice_kavitha_matched | Kavitha Sundaram's note: 9 of 9 claims match NCRP 11208260078296 (offline expected answer) | 9 | data/samples/voice/manifest.json | `json:samples.5.expect.counts.MATCHED` |
| trace_verifier | Verifier · 17/17 passed (plan check plus 16 drafts) | 17 | src/wapsi/agenttrace/showcase_recording.json | `text:"checked": 17` |
| trace_grounding | Reply grounding · 23/23 values traced to tools | 23 | src/wapsi/agenttrace/showcase_recording.json | `text:"checked": 23` |

## Measured with Gemma 4 served on AMD (AMD Token Factory, 2026-10-14; committed results files)

Runs made during the event on synthetic data. The model ran on AMD GPUs on the provider's side (AMD's Token Factory
API); latency is measured at the client, from a laptop in India, and includes the network. These are not
benchmarks of a dedicated MI300X. Results files:

- the agent: `editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json`, written by
  `uv run python -m editions.amd.app.agent_demo --case 31009260000101 --live --prose endpoint --demo --out ...`
  with no failover list (Gemma 4 alone), so the draft prose inside the run was Gemma 4's too;
- the showcase world through the harness: `evals/reports/amd/showcase-gemma4-20261014.json`
  (`--llm openai-compat:Gemma-4-31B-IT`, one model, failover off). Its five prose answers were served from the
  prompt-hash cache written by the same day's recording run (the same model, 23 minutes earlier; the report says
  6 cache hits), and its two raw-model summaries were live;
- the public demo's recorded prose: `data/recordings/demo_prose.json` (`meta.runs`, last entry);
- the smoke run: `pitch/amd/evidence/token-factory-smoke-2026-10-14-replayed.json` (`pitch/amd/evidence/token_factory_smoke.py --allow-replay`). Its label says
  "replayed from cache (1 of 9 replies)": the one replayed reply is the first tool-calling turn (`calls`, `cached`);
  the health probe, the four pipeline prose requests, the tool follow-up and both extractions were live;
- voice: `editions/amd/bench/results/voice-tokenfactory-20261014/` (the text model only: the samples' scripts stood
  in for the speech recogniser, so no speech model ran).

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| tf_agent_tool_calls | 5 tool calls behind "Open case 31009260000101", Gemma 4 driving the ADK agent through native tool calls (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 5 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:runs.0.summary.tool_calls` |
| tf_agent_drafts | that live run ended VERIFIED: 16 of 16 drafts verified, with Gemma 4 on AMD writing the draft prose too | 16 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:outcome.drafts_verified` |
| tf_agent_prose | 3 of 3 prose fields in that run written by Gemma 4 on AMD | 3 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:outcome.drafts_with_prose` |
| tf_agent_prose_fallback | 0 of them replaced by the fixed paragraph | 0 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:outcome.drafts_with_prose_fallback` |
| adk_grounding | reply grounding of the live run: 24 of 24 values in the agent's reply (17 amounts, 7 account numbers) came from a tool | 24 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:runs.0.trace.steps.12.verifier.checked` |
| tf_agent_replies | 4 of 4 replies in that session quoted only values a tool returned | 4 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:outcome.replies_grounded` |
| tf_agent_refused | an analyst's approval refused by maker-checker in the live session | 1 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:runs.2.summary.blocked` |
| tf_agent_calls | 12 model calls in the session, 0 errors | 12 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:measured.model_calls` |
| tf_agent_p50_ms | a median of 3.86 s per model call (the HTTP request, laptop in India to the AMD Token Factory) | 3863.3 | editions/amd/bench/results/agent-tokenfactory-20261014/agent-demo-showcase-gemma4.json | `json:measured.latency_ms_p50` |
| tf_showcase_pass | showcase world with Gemma 4 writing the prose: drafts passing the verifier 1.0 (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 1.0 | evals/reports/amd/showcase-gemma4-20261014.json | `json:metrics.draft_verification_pass_rate` |
| tf_showcase_fallback | prose replaced by fixed text on that run 0.0 | 0.0 | evals/reports/amd/showcase-gemma4-20261014.json | `json:metrics.llm_prose_fallback_rate` |
| tf_showcase_hold_exact | holds exact 1.0 with Gemma 4 in the loop | 1.0 | evals/reports/amd/showcase-gemma4-20261014.json | `json:metrics.hold_exact_match_rate` |
| tf_smoke_latency | one prose request takes a median of 8.29 s (one request at a time, from a laptop in India; AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 8.29 | pitch/amd/evidence/token-factory-smoke-2026-10-14-replayed.json | `json:pipeline.latency_s_median` |
| tf_tokens_case_in | showcase case 1 uses 25,914 prompt tokens for its prose (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 25914 | pitch/amd/evidence/token-factory-smoke-2026-10-14-replayed.json | `json:pipeline.prompt_tokens` |
| tf_tokens_case_out | and 636 completion tokens (AMD Token Factory smoke run) | 636 | pitch/amd/evidence/token-factory-smoke-2026-10-14-replayed.json | `json:pipeline.completion_tokens` |
| tf_smoke_drafts | 16 of 16 drafts passing the verifier in the AMD Token Factory smoke run, holds equal to the hand-computed fixture | 16 | pitch/amd/evidence/token-factory-smoke-2026-10-14-replayed.json | `json:pipeline.drafts_passing_verifier` |
| tf_recorded_prose | the public demo replays 5 prose fields recorded from Gemma 4 on AMD (AMD Token Factory, 2026-10-14), labelled as a recording | 5 | data/recordings/demo_prose.json | `len:entries` |
| tf_voice_notes | Gemma 4 on AMD answered for all 6 sample transcripts (AMD Token Factory, text model only) | 6 | editions/amd/bench/results/voice-tokenfactory-20261014/voice_bench_gemma4.json | `json:metrics.answers_by_model.Gemma-4-31B-IT` |
| tf_voice_extract_p50 | a median of 7.5 s per note to translate and extract the fields (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 7.5 | editions/amd/bench/results/voice-tokenfactory-20261014/voice_bench_gemma4.json | `json:metrics.extract_s.p50` |
| tf_voice_agreement | share of extracted values that agree with the samples' expected answers (AMD Token Factory run) | 0.9833 | editions/amd/bench/results/voice-tokenfactory-20261014/voice_bench_gemma4.json | `json:metrics.value_agreement` |
| tf_voice_agreement_pct | 98.3% of the extracted values agree with the expected answers (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14) | 98.3 | derived | `calc:100 * tf_voice_agreement` |
| tf_voice_as_documented | 4 of 6 notes reconciled as documented on the AMD Token Factory run; the other two were false alarms on a caller's closing plea | 4 | editions/amd/bench/results/voice-tokenfactory-20261014/voice_bench_gemma4.json | `json:metrics.samples_as_documented` |

## Measured on an AMD Instinct MI300X (AMD AI Notebook, ROCm 7.14, vLLM), 2026-10-14

Gemma 4 31B (`google/gemma-4-31B-it`) served by vLLM on an AMD AI Notebook, ROCm 7.14. Each session had one whole AMD Instinct MI300X, exposed to the notebook as a single SR-IOV virtual function, and the two sessions got different MI300X GPUs (ASIC serials `0xD14074DBA91C454A` and `0x8816642CF4E8921A`, in `session4/amd_smi_static.json` and `amd_smi_static.json`). Not the AMD Developer Cloud droplet and not the Token Factory. The golden set (`nb_golden_*`, `nb_raw_*`, `nb_bound_*`) ran in GPU session 4, the rest in session 5, same model and code (`editions/amd/evidence/notebook/2026-10-14/SESSIONS.md`). Rows filled by `fill_pitch.py` from the same bundle are in the generated section below; these are the figures the pitch quotes by hand from the result files.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| nb_tok_s_c64 | 699 output tokens/s at concurrency 64 (Gemma 4 31B, vLLM on the MI300X; synthetic 1,024-token prompts, 256 out) | 699 | editions/amd/evidence/notebook/2026-10-14/bench/llm_bench.json | `json:levels.3.output_tokens_per_s` |
| nb_draft_p50 | a real drafting request takes a median of 3.07 s one at a time (Wapsi's own prompts) | 3.07 | editions/amd/evidence/notebook/2026-10-14/bench/llm_bench_wapsi_prompts.json | `json:levels.0.e2e_s.p50` |
| nb_cases_hour_c1 | 414 complete cases per hour at concurrency 1 | 414 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c1.json | `json:metrics.cases_per_hour` |
| nb_cases_hour_c8 | 1,410 complete cases per hour at concurrency 8 | 1410 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c8.json | `json:metrics.cases_per_hour` |
| nb_e2e_cases_ok | 39 of 39 cases completed in each end-to-end run | 39 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:metrics.cases_ok` |
| nb_e2e_pass | drafts passing the verifier 1.0 in the end-to-end runs | 1.0 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:metrics.draft_verification_pass_rate` |
| nb_e2e_llm_drafts | 83 model-written prose fields per end-to-end run (drafts that ask the model for prose) | 83 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:metrics.llm_drafts` |
| nb_e2e_fallbacks | 9 of 83 model-written prose fields were replaced by fixed text at concurrency 16 | 9 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:metrics.prose_fallbacks` |
| nb_e2e_fallback_rate | 0.1084 of the model-written prose fields replaced by fixed text at concurrency 16 | 0.1084 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:metrics.prose_fallback_rate` |
| nb_usd_per_case | $0.0013 per case at concurrency 16 at the $1.99 per GPU-hour list price | 0.0013 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:cost.usd_per_case` |
| nb_usd_per_1000 | $1.25 per 1,000 cases | 1.25 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:cost.usd_per_1000_cases` |
| nb_gpu_hour_usd | the $1.99 per GPU-hour list price the cost per case is computed at | 1.99 | editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json | `json:cost.gpu_hour_usd` |
| nb_vram_peak | device memory peak 97.0 GB while serving (rocm-smi) | 97.0 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.vram_peak_gb` |
| nb_vram_total | of 191.7 GB | 191.7 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.vram_total_gb` |
| nb_golden_cases | demo world, 39 cases, Gemma 4 writing the prose | 39 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:cases` |
| nb_golden_hold_exact | holds exact 1.0 | 1.0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.hold_exact_match_rate` |
| nb_golden_false_freezes | false freezes 0 | 0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.false_freeze_count` |
| nb_golden_pass | drafts passing the verifier 1.0 (every draft passes the verifier) | 1.0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.draft_verification_pass_rate` |
| nb_golden_numeric | numeric accuracy 1.0 | 1.0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.draft_numeric_accuracy` |
| nb_golden_citations | citation validity 1.0 | 1.0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.citation_validity` |
| nb_golden_leaks | injection leaks 0 | 0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.injection_leaks` |
| nb_golden_fallback | prose replaced by fixed text in 0.0332 of all 301 drafts | 0.0332 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.llm_prose_fallback_rate` |
| nb_golden_p95 | p95 11.12 s per case to verified drafts | 11.12 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.p95_total_s` |
| nb_raw_rate | raw Gemma 4 in free text: a wrong amount in 0.4872 of cases | 0.4872 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:metrics.llm_only_hallucination_rate` |
| nb_raw_wrong | 19 of 39 cases | 19 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:ab_test.llm_only.summaries_with_wrong_amounts` |
| nb_bound_drafts | the same run's 301 Wapsi drafts | 301 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:ab_test.wapsi_bound.drafts` |
| nb_bound_wrong | 0 of them with a numeric error | 0 | editions/amd/evidence/notebook/2026-10-14/golden_eval.json | `json:ab_test.wapsi_bound.drafts_with_numeric_errors` |
| nb_ab_raw_wrong | 4 of 20 free-form STRs with a wrong amount | 4 | editions/amd/evidence/notebook/2026-10-14/ab_hallucination.json | `json:arms.raw.docs_with_wrong_amount` |
| nb_ab_raw_docs | 20 free-form STRs (20 cases) | 20 | editions/amd/evidence/notebook/2026-10-14/ab_hallucination.json | `json:arms.raw.docs` |
| nb_ab_str_wrong | 0 of 17 Wapsi-bound STRs with a wrong amount | 0 | editions/amd/evidence/notebook/2026-10-14/ab_hallucination.json | `json:arms.bound.str_only.docs_with_wrong_amount` |
| nb_ab_str_docs | 17 Wapsi-bound STRs | 17 | editions/amd/evidence/notebook/2026-10-14/ab_hallucination.json | `json:arms.bound.str_only.docs` |
| nb_gnn_params | the GNN trained on the MI300X has 410k parameters | 410017 | editions/amd/evidence/notebook/2026-10-14/gnn_metrics.json | `json:parameters` |
| nb_gnn_hard_disp | MI300X-trained GNN on the hard world: dispositions 0.9198 | 0.9198 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.gnn:nb-base.disposition_accuracy` |
| nb_gnn_hard_ih | innocent-holder recall 0.875 | 0.875 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.gnn:nb-base.role_recall.INNOCENT_HOLDER` |
| nb_gnn_hard_mule_recall | its mule recall falls to 0.7415 | 0.7415 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.gnn:nb-base.role_recall.MULE` |
| nb_heur_hard_auc | against the default heuristic's mule ROC-AUC 0.9411 (same run) | 0.9411 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.heuristic-v2.classification.mule_auc` |
| nb_heur_hard_ih | the heuristic's innocent-holder recall 0.6875 | 0.6875 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.heuristic-v2.role_recall.INNOCENT_HOLDER` |
| nb_heur_hard_mule_recall | the heuristic's mule recall 0.966 | 0.966 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.heuristic-v2.role_recall.MULE` |
| nb_heur_hard_disp | the heuristic's dispositions 0.9198 | 0.9198 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.heuristic-v2.disposition_accuracy` |
| nb_best_feature_auc | level with the best single feature (ROC-AUC 0.9205, `hist_credit_turnover_ratio`) | 0.9205 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.gnn:nb-base.classification.best_single_feature_auc` |
| nb_gnn_hard_unknown | it sends more accounts to the officer as UNKNOWN (0.185 of accounts) | 0.185 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.gnn:nb-base.classification.unknown_rate` |
| nb_heur_hard_unknown | against the heuristic's 0.035 | 0.035 | editions/amd/evidence/notebook/2026-10-14/gnn_hard_eval.json | `json:by_scorer.heuristic-v2.classification.unknown_rate` |
| nb_whisper_rtf | Whisper large-v3-turbo on the MI300X: steady-state real-time factor 0.0312 (warm-up excluded) | 0.0312 | editions/amd/bench/results/voice-mi300x-notebook-20261014/whisper_rtf.json | `json:steady_state_rtf_excluding_first` |
| nb_whisper_meera | the 31-second note transcribed in 0.92 s | 0.92 | editions/amd/bench/results/voice-mi300x-notebook-20261014/whisper_rtf.json | `json:samples.4.asr_s` |

## AMD, MI300X droplet mode: memory plan and budget (design from config in `editions/amd/`; not a measurement)

These rows are arithmetic over configuration values. They say what is planned to fit on one MI300X, not what was
measured on one. Measured memory per container is the slot `droplet_vram`.

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

<!-- amd-evidence:begin (generated by editions/amd/kickoff/fill_pitch.py; do not edit by hand) -->
## AMD edition: measured on MI300X (AMD ROCm notebook)

Written by `editions/amd/kickoff/fill_pitch.py --write` from the evidence bundles of the AMD ROCm notebook runs on 2026-10-14 (UTC). Each row re-reads `headline` in the `evidence.json` named in its Source, which `collect_evidence.py` built from that run's result files (the bundle's `compute` says where it ran). A later bundle replaces only the rows of the values it measured.

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| amd_ab_bound_docs | 155 (A/B: Wapsi-bound drafts) | 155 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.ab_bound_docs` |
| amd_ab_bound_docs_with_wrong_amount | 0 (A/B: Wapsi-bound drafts with a wrong amount) | 0 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.ab_bound_docs_with_wrong_amount` |
| amd_e2e_best_cases_per_hour | 1,590 (cases per hour (best concurrency)) | 1590 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.e2e_best_cases_per_hour` |
| amd_gnn_epoch_s | 0.06 s (GNN seconds per epoch on the AMD GPU) | 0.06 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.gnn_epoch_s` |
| amd_gnn_hard_mule_auc | 0.9202 (AMD-trained GNN: mule ROC-AUC on the hard world) | 0.9202 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.gnn_hard_mule_auc` |
| amd_llm_tok_s_c1 | 55 (drafting model output tokens/s at concurrency 1) | 55 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.llm_tok_s_c1` |
| amd_llm_tok_s_c16 | 474 (drafting model output tokens/s at concurrency 16) | 474 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.llm_tok_s_c16` |
| amd_llm_tok_s_c4 | 181 (drafting model output tokens/s at concurrency 4) | 181 | editions/amd/evidence/notebook/2026-10-14/evidence.json | `json:headline.llm_tok_s_c4` |
<!-- amd-evidence:end -->

## Command outputs (checked by hand on 29-09-2026; re-run before quoting)

| ID | Pitched as | Value | Source | Locator |
|---|---|---|---|---|
| cmd_citations | 121/121 citation, fact and radar quote checks pass | 121 | src/wapsi/search/check_citations.py | `cmd:uv run python -m wapsi.search.check_citations` |
| cmd_radar_quotes | 52/52 radar quotes verified | 52 | src/wapsi/search/radar.py | `cmd:uv run python -m wapsi.search.radar verify` |
| cmd_drill | 6/6 tampering attempts detected (plus an in-store edit), 40-event chain | 6 | src/wapsi/audit/drill.py | `cmd:uv run python -m wapsi.audit drill` |
| cmd_policy | 48/48 policy re-checks pass on showcase case 1 | 48 | .claude/skills/lien-calculator/scripts/hold_plan.py | `cmd:uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory` |
| cmd_str_slots | 78 numbers in the STR recomputed by the verifier | 78 | .claude/skills/str-drafter/scripts/draft_and_verify.py | `cmd:uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory` |
| cmd_tests | 2,226 tests, 0 fail (full suite, 14-10-2026), with the docs index and demo world built; a fresh clone runs 2,215 and skips 11 | 2226 | pyproject.toml | `cmd:uv run pytest -q` |
| cmd_trace_run1 | 5 tool calls behind "Open case 31009260000101" (agent trace demo, 07-10-2026) | 5 | editions/gcp/app/trace_demo.py | `cmd:uv run --extra gcp python -m editions.gcp.app.trace_demo` |
| cmd_trace_case | Agent trace · 8: 8 tool calls in 4 runs on the showcase case, 2 blocked (agent trace demo, 07-10-2026) | 8 | editions/gcp/app/trace_demo.py | `cmd:uv run --extra gcp python -m editions.gcp.app.trace_demo` |

## AMD measurement slots (open; not claims)

Each slot is a figure the pitch wants and does not have yet. In the pitch it appears as `TBD(slot_id)`;
`pitch/check_numbers.py` fails on a bare TBD and on a slot id that is not in this table. A slot has no value. To fill
one: commit the results file, add a row with a locator to a numbers table above, replace `TBD(slot_id)` in the pitch
with the figure, and delete the slot's row here. If a slot is still open when the video is recorded, its sentence is
cut; it is never estimated.

"Where it runs" is part of the claim and goes on the slide with the figure.

**Check the run was healthy before promoting a figure from the shared endpoint.** A degenerate answer is rejected
by the prose validator and replaced by the fixed paragraph, so a run made while the endpoint was returning
degenerate text still reports every draft as passing: what it measured is the fallback text, not the model. Run
`uv run python pitch/amd/evidence/token_factory_smoke.py --probe-only` before and after the measurement, read the
run's prose fallback rate and a few draft bodies, and look at the timings: a fallback rate near 1.0, or replies
that all stop at the token limit and take several times longer than usual, are the signature of a degraded model.
A figure from an unhealthy run is discarded.

**One model per figure.** A figure pitched as Gemma 4's comes from a run in which Gemma 4 wrote everything the
figure counts: run the harness with `--llm openai-compat:Gemma-4-31B-IT` (one model, failover off), or check that the
report's `prose_by_model` (and, for the raw-model comparison, `ab_test.llm_only.answered_by`) names Gemma 4 alone
before a figure is filled. A run that a failover model partly wrote is reported under the models that wrote it.

**Filling slots from a kickoff run.** `uv run python editions/amd/kickoff/fill_pitch.py --evidence <evidence.json>`
does the steps above for every slot an evidence bundle carries (dry-run diff; `--write` applies it): it replaces
each `TBD(slot_id)` in the pitch, adds the rows that back the figure to a generated section of this page and
deletes the slot's row below. `--slots` lists which slots a bundle fills and which stay with their own results
files.

The demo-world figures with Gemma 4 on an AMD Instinct MI300X are measured (the MI300X section above); the `tf_*`
and `ab_raw_wrong` slots below are the same figures through the AMD Token Factory, which are still open.

| Slot | Will be pitched as | Where it runs | Planned source |
|---|---|---|---|
| tf_demo_pass | demo world (39 cases), drafts passing the verifier with Gemma 4 writing the prose | AMD Token Factory, Gemma-4-31B-IT | `evals/reports/amd/` harness report; filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| tf_demo_fallback | demo world, share of drafts whose model prose was rejected and replaced by fixed text | AMD Token Factory, Gemma-4-31B-IT | `evals/reports/amd/` harness report (`metrics.llm_prose_fallback_rate`); filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| tf_demo_hold_exact | demo world, holds exact with the real model in the loop | AMD Token Factory, Gemma-4-31B-IT | `evals/reports/amd/` harness report (`metrics.hold_exact_match_rate`); filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| tf_call_latency | drafting request latency at p50 in the kickoff run (the smoke run's 8.29 s is a separate, one-at-a-time figure) | AMD Token Factory, Gemma-4-31B-IT | filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| ab_raw_wrong | raw Gemma 4 asked for the hold plan in free text: share of cases with a wrong amount | AMD Token Factory, Gemma-4-31B-IT | `evals/reports/amd/` harness report (`metrics.llm_only_hallucination_rate`); filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| voice_asr_samples | bundled voice notes (of 6) whose Whisper transcript reconciles as the manifest expects | local Whisper (hardware stated with the figure) + Gemma-4-31B-IT on AMD for the fields | `uv run python -m editions.amd.bench.voice_bench --asr local` (`voice_bench.json` under `editions/amd/bench/results/`) |
| droplet_vram | measured GPU memory per container (drafting model, verifier, GNN) | MI300X droplet | `editions/amd/AMD_RESULTS.md`; filled by `editions/amd/kickoff/fill_pitch.py` from an evidence bundle |
| evolus_blocks | Evolus building blocks driven from the app through the REST API (at least 2) | Evolus event workspace + own vLLM endpoint on AMD | `pitch/EVOLUS_TRACK.md` evidence table |
