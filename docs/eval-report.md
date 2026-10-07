# Evaluation report format (`evals/reports/*.json`)

Written by `uv run python -m evals.harness --world <name>`; read by the UI KPI page, the `wapsi-eval` skill and
`pitch/check_numbers.py`. Every run writes `<world>-<UTC stamp>.json` and `.md`, plus one stable file:
`latest.json` for the heuristic scorer on showcase / demo / full, `<world>.json` for hard worlds (`hard.json`,
`hard-101.json`; never `latest.json`), `<world>-<xgb|gnn>.json` otherwise. `--report-name` overrides the stable
name (the committed `showcase.json` and `demo.json` were written that way). Options: [Evaluation](evaluation.md#scorers-and-harness-options).

```json
{
  "world": "demo",
  "generated_at": "2026-09-20T10:00:00+00:00",
  "config": {"rule": "PRO_RATA", "llm": "fake-llm", "scorer": "heuristic-v2", "scorer_spec": "heuristic",
             "search": true},
  "cases": 39,
  "adversarial_cases": 3,
  "metrics": {
    "traced_amount_recall": 0.99,            // located / stolen
    "totals_abs_error_inr": 0.0,             // sum |ours - golden| over all TraceTotals fields
    "hold_exact_match_rate": 1.0,            // accounts whose hold equals golden (±₹0.01)
    "over_hold_inr": 0.0,                    // held beyond golden hold
    "false_freeze_count": 0,                 // accounts held that golden says hold nothing
    "disposition_accuracy": 1.0,
    "process_accuracy": 1.0,
    "str_ground_accuracy": 1.0,
    "escalation_precision": 1.0, "escalation_recall": 1.0,
    "role_precision": {"MULE": 0.9, "INNOCENT_HOLDER": 0.95, "MERCHANT": 1.0, "INTERMEDIARY": 1.0},
    "role_recall":    {"MULE": 0.93, "INNOCENT_HOLDER": 0.88, "MERCHANT": 1.0, "INTERMEDIARY": 1.0},
                                             // bona fide recipients and unwitting conduits both score as
                                             // INNOCENT_HOLDER (wapsi.models.legacy_role)
    "draft_verification_pass_rate": 1.0,
    "draft_numeric_accuracy": 1.0,           // drafts whose numeric check passed
    "llm_only_hallucination_rate": null,     // real LLMs only: raw prose with a wrong amount (no slot discipline)
    "llm_prose_fallback_rate": 0.0,          // drafts whose LLM prose failed checks and used the deterministic fallback
    "citation_validity": 1.0,
    "injection_flag_recall": 1.0,            // adversarial complaints flagged by the guard
    "injection_leaks": 0,                    // drafts containing injected text
    "p50_time_to_hold_plan_s": 0.05, "p95_time_to_hold_plan_s": 0.12,
    "p95_total_s": 0.4
  },
  "baseline": {"whole_account_freeze_inr": 0.0, "wapsi_hold_inr": 0.0, "accounts_frozen_baseline": 0, "accounts_held": 0},
                                             // the whole-account freeze baseline: a comparison figure only
  "per_typology": {"T1_FANOUT_CASHOUT": {"cases": 5, "hold_exact_match_rate": 1.0, "disposition_accuracy": 1.0}},
  "classification": {                        // the scorer on the accounts it decides (truth MULE / INNOCENT_HOLDER / VICTIM)
    "accounts": 200, "truth": {"INNOCENT_HOLDER": 48, "MULE": 147, "VICTIM": 5},
    "predicted": {"INNOCENT_HOLDER": 35, "MULE": 158, "UNKNOWN": 7},
    "unknown_rate": 0.035,                   // share sent to the officer as UNKNOWN
    "mule_auc": 0.9411,                      // MULE-vs-rest ROC-AUC of the score
    "best_single_feature_auc": 0.9205, "best_single_feature": "hist_credit_turnover_ratio",
    "victim_on_trail_called_mule": 5
  },
  "by_scorer": {                             // only with several --scorer options: each scorer's headline metrics
    "heuristic-v2": {"scorer": "heuristic-v2", "scorer_spec": "heuristic", "disposition_accuracy": 0.9198, "...": "..."},
    "xgb:xgb_hard202": {"scorer": "xgb:xgb_hard202", "scorer_spec": "xgb:models/xgb_hard202.json", "...": "..."}
  },
  "failures": [{"case_id": "...", "check": "...", "detail": "..."}]
}
```

The `classification` and `by_scorer` examples above are the values in the committed `evals/reports/hard.json`.
