# Evaluation report format (`evals/reports/*.json`)

Written by `uv run python -m evals.harness --world <name>`; read by the UI KPI page, the `wapsi-eval` skill and
`pitch/check_numbers.py`. Every run writes `<world>-<UTC stamp>.json` and `.md`, plus one stable file:
`latest.json` for the heuristic scorer on showcase / demo / full, `<world>.json` for hard worlds (`hard.json`,
`hard-101.json`; never `latest.json`), `<world>-<xgb|gnn>.json` otherwise; a real-model run (`--llm gemini`,
`--llm openai-compat`) writes `<world>-gemini.json` / `<world>-openai-compat.json` and never replaces the offline
reference. `--report-name` overrides the stable
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
  "llm": {                                   // real models only (--llm gemini | openai-compat | openai:...); null with the fake LLM
    "model_id": "gemini-3.8-flash", "backend": "aistudio",   // openai-compat: "openai-compatible"
    "endpoint_host": null,                     // openai-compat: the endpoint's host, e.g. "developer.amd.com.cn"
    "requests": 122, "api_calls": 123, "ok": 122, "cache_hits": 0, "cache_hit_rate": 0.0,
    "retries": 1, "rate_limited_429": 1, "unavailable_5xx": 0, "unusable_answers": 0, "errors": 0,
    "quota_exhausted": false, "rate_limit_wait_s": 610.2, "backoff_wait_s": 7.0,
    "limits": {"rpm": 10, "rpd": 250, "used_today": 123, "remaining_today": 127, "min_interval_s": 0.0},
    "reported_cost_usd": 0.0,                  // what the endpoint itself reported (usage.cost), summed; 0.0 if it reports none
    "tokens": {"prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0},
    "tokens_served_from_cache": {"prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "total_tokens": 0},
    "cost_estimate_usd": {"this_run": 0.0,     // Gemini API free tier: no charge (429 over the limit, never a bill)
                          "paid_tier_equivalent": 0.0, "saved_by_cache_paid_equivalent": 0.0,
                          "price_per_1m_tokens": {"input": 0.3, "output": 2.5}, "basis": "assumed list price ..."}
  },
  "ab_test": {                               // real models only: the same model with and without Wapsi's slot binding
    "llm_only": {"cases": 39, "summaries_with_wrong_amounts": 0, "hallucination_rate": 0.0},
    "wapsi_bound": {"drafts": 300, "drafts_with_numeric_errors": 0, "hallucination_rate": 0.0},
    "method": "..."
  },
  "failures": [{"case_id": "...", "check": "...", "detail": "..."}]
}
```

The `llm` and `ab_test` numbers above only show the shape (illustrative, not measured). The `llm` block comes
from `wapsi.adapters.llm_resilience.ResilientLLM`: free-tier RPM / RPD limiting, 429 / 503 retries and a prompt-hash
response cache under `.cache/llm` (gitignored; replay only, never training data), so a rerun costs no requests.

`--llm openai-compat` evaluates the OpenAI-compatible endpoint in `WAPSI_LLM_BASE_URL` / `WAPSI_LLM_API_KEY` /
`WAPSI_LLM_MODEL` (the AMD Token Factory, or a vLLM server on AMD; `openai-compat:<model>` picks another model on
the same endpoint). Retries honour the server's `Retry-After`, and the cache is kept apart per endpoint host. When
the endpoint prices its own answers (`usage.cost`), `cost_estimate_usd.this_run` is that figure and
`paid_tier_equivalent` is `null`; otherwise the cost block is n/a for a self-served model. An evaluation scores the
model as it answers: degenerate output is rejected by the prose validator and shows up in
`llm_prose_fallback_rate`. Label every number from such a run with the endpoint that served it and the date, and
never present it as a benchmark of hardware it was not measured on.

The `classification` and `by_scorer` examples above are the values in the committed `evals/reports/hard.json`.
