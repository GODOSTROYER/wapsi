# Wapsi evaluation — showcase

Generated 2026-10-14T10:54:38.461776+00:00 · 2 cases (0 adversarial) · LLM `Gemma-4-31B-IT on AMD @ developer.amd.com.cn`

| Metric | Value |
|---|---|
| traced_amount_recall | 1.0 |
| totals_abs_error_inr | 0.0 |
| hold_exact_match_rate | 1.0 |
| over_hold_inr | 0.0 |
| false_freeze_count | 0 |
| disposition_accuracy | 1.0 |
| process_accuracy | 1.0 |
| str_ground_accuracy | 1.0 |
| escalation_precision | 1.0 |
| escalation_recall | 1.0 |
| role_precision | {"MULE": 1.0, "INNOCENT_HOLDER": 1.0, "MERCHANT": 1.0, "INTERMEDIARY": 1.0} |
| role_recall | {"MULE": 1.0, "INNOCENT_HOLDER": 1.0, "MERCHANT": 1.0, "INTERMEDIARY": 1.0} |
| draft_verification_pass_rate | 1.0 |
| draft_numeric_accuracy | 1.0 |
| citation_validity | 1.0 |
| llm_only_hallucination_rate | 0.0 |
| llm_prose_fallback_rate | 0.0 |
| injection_flag_recall | 1.0 |
| injection_leaks | 0 |
| p50_time_to_hold_plan_s | 0.0678 |
| p95_time_to_hold_plan_s | 0.0741 |
| p95_total_s | 0.3717 |

## Whole-account freeze baseline vs Wapsi

- Whole-account freeze baseline (comparison only): ₹5,04,70,000.00 across 7 accounts
- Wapsi holds ₹2,62,848.00 across 6 accounts

| Group | Accounts | Whole-account freeze | Wapsi holds |
|---|---|---|---|
| Nodal / pool / escrow (SOP 9.1(iii)) | 1 | ₹5,00,20,000.00 | ₹19,200.00 in 1 |
| Merchants (SOP 9.2) | 1 | ₹51,200.00 | ₹0.00 in 0 |
| Innocent holders (SOP 5(xii)) | 1 | ₹1,55,000.00 | ₹4,800.00 in 1 |
| Mules | 4 | ₹2,43,800.00 | ₹2,38,848.00 in 4 |

## Per typology

| Typology | Cases | Hold exact | Disposition acc |
|---|---|---|---|
| T2_CHAIN_LAYERING | 1 | 1.0 | 1.0 |
| T7_MULTI_VICTIM | 1 | 1.0 | 1.0 |

## LLM

- Model `Gemma-4-31B-IT` (openai-compatible @ developer.amd.com.cn): 8 requests, 2 API calls, 6 cache hits (75%), 0 retries, 0 rate-limited (429), 0 errors
- Tokens: 7603 in, 794 out, 0 thinking (total 8397); waited 0.0 s for the rate limit
- Cost this run: $0.000321 (reported by the endpoint itself (usage.cost of each answer; on a free allowance this is usage counted against the daily cap, not a bill))
- Prose fallback rate 0.0, draft verification pass rate 1.0
- Prose by model (drafts whose prose a model wrote): Gemma-4-31B-IT on AMD @ developer.amd.com.cn (5)

## A/B: raw LLM vs Wapsi-bound drafts

| Arm | Units | With wrong amounts | Hallucination rate |
|---|---|---|---|
| LLM only (raw fact sheet; answered by Gemma-4-31B-IT (2)) | 2 summaries | 0 | 0.0 |
| Wapsi (slots + verifier) | 21 drafts | 0 | 0.0 |

## Classification (scorer-decided accounts)

- 7 accounts, truth {'INNOCENT_HOLDER': 1, 'MULE': 6}, predicted {'INNOCENT_HOLDER': 1, 'MULE': 6}
- MULE-vs-rest ROC-AUC 1.0; best single feature age_days (AUC 1.0); sent to officer (UNKNOWN) 0.0
