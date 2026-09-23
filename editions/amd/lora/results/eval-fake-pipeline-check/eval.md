# Wapsi prose fine-tune: evaluation

Generated 2026-09-23T15:13:09Z - 50 held-out cases from heldout-101, heldout-202, heldout-303, heldout-404, heldout-505 - variants clean, tempting, injection. Measured values from this run only.

| Metric | fake | noisy | hallucinating | injection-obedient |
|---|---|---|---|---|
| acceptance_rate | 1.0 | 0.7799 | 0.0 | 0.8252 |
| accepted_after_retry_rate | 0.0 | 0.1165 | 0.0 | 0.0 |
| fallback_rate | 0.0 | 0.1036 | 1.0 | 0.1748 |
| prose_verifier_pass_rate | 1.0 | 0.9612 | 1.0 | 1.0 |
| all_drafts_verifier_pass_rate | 1.0 | 0.9896 | 1.0 | 1.0 |
| placeholder_coverage | 1.0 | 1.0 | 1.0 | 1.0 |
| invented_number_rate | 0.0 | 0.2201 | 1.0 | 0.1553 |
| forbidden_token_rate | 0.0 | 0.2201 | 1.0 | 0.1553 |
| injection_obeyed_rate | 0.0 | 0.0097 | 0.0 | 0.1748 |
| json_valid_rate | 1.0 | 1.0 | 1.0 | 1.0 |
| str_ground_named_rate | 1.0 | 1.0 | 1.0 | 1.0 |
| length_in_range_rate | 0.5922 | 0.699 | 0.9903 | 0.6699 |
| llm_error_rate | 0.0 | 0.0 | 0.0 | 0.0 |
| latency_s_mean | 0.001 | 0.0004 | 0.0005 | 0.0004 |
| latency_s_p95 | 0.0014 | 0.0008 | 0.0009 | 0.0007 |

## Acceptance rate by variant

| Variant | fake | noisy | hallucinating | injection-obedient |
|---|---|---|---|---|
| clean | 1.0 | 0.8252 | 0.0 | 0.8252 |
| tempting | 1.0 | 0.7184 | 0.0 | 0.8252 |
| injection | 1.0 | 0.7961 | 0.0 | 0.8252 |

## Acceptance rate by task

| Task | fake | noisy | hallucinating | injection-obedient |
|---|---|---|---|---|
| str_grounds | 1.0 | 0.8116 | 0.0 | 0.8043 |
| edd_rationale | 1.0 | 0.8095 | 0.0 | 1.0 |
| case_summary | 1.0 | 0.7467 | 0.0 | 0.82 |

Specs: `fake` = `mock:fake`; `noisy` = `fake-noisy`; `hallucinating` = `fake-hallucinate`; `injection-obedient` = `fake-inject`
