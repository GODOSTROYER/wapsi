# Wapsi end-to-end benchmark — demo

Generated 2026-10-14T14:49:40.036903+00:00 · host `jupyter-user-261014142914-35b202b3` · commit `fc20109`

Label: **AMD ROCm notebook run, 2026-10-14**

## Model

| LLM | Served from | Max model len | Scorer | Guard |
|---|---|---|---|---|
| vllm-rocm:wapsi-llm | google/gemma-4-31B-it | 16,384 | heuristic-v2 | rules |

## Throughput and cost

| Cases | Concurrency | Cases/hour | Hold plan p50 (s) | Hold plan p95 (s) | Case p50 (s) | Case p95 (s) | $/GPU-hour | $ per case | ₹ per case |
|---|---|---|---|---|---|---|---|---|---|
| 39/39 | 8 | 1,409.9500 | 0.0642 | 0.4676 | 17.7234 | 27.8412 | 1.9900 | 0.0014 | — |

## Quality (verifier checks on every draft)

| Drafts | Verification pass rate | Cases fully verified | LLM prose drafts | Prose fallback rate | Hold exact match | LLM calls/case | LLM p50 latency (s) |
|---|---|---|---|---|---|---|---|
| 301 | 1.000 | 1.000 | 83 | 0.120 | 1.000 | 2.538 | 6.876 |
