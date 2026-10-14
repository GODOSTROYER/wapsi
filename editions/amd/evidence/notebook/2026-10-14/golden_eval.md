# Golden-set evaluation with the model under test (demo, 39 cases)

- Label: **AMD ROCm notebook run, 2026-10-14**
- LLM: `vllm-rocm:wapsi-llm` · scorer: `heuristic-v2` (roles and holds never come from the LLM) · generated 2026-10-14T13:57:40.582726+00:00

| Metric | Value |
|---|---|
| hold_exact_match_rate | 1.0 |
| false_freeze_count | 0 |
| disposition_accuracy | 1.0 |
| draft_verification_pass_rate | 1.0 |
| draft_numeric_accuracy | 1.0 |
| citation_validity | 1.0 |
| llm_only_hallucination_rate | 0.4872 |
| llm_prose_fallback_rate | 0.0332 |
| injection_flag_recall | 1.0 |
| injection_leaks | 0 |
| p50_time_to_hold_plan_s | 0.0606 |
| p95_total_s | 11.1211 |
