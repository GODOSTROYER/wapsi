# Wapsi end-to-end benchmark — demo

Generated 2026-10-14T14:47:59.666713+00:00 · host `jupyter-user-261014142914-35b202b3` · commit `fc20109`

Label: **AMD ROCm notebook run, 2026-10-14**

## Model

| LLM | Served from | Max model len | Scorer | Guard |
|---|---|---|---|---|
| vllm-rocm:wapsi-llm | google/gemma-4-31B-it | 16,384 | heuristic-v2 | rules |

## Throughput and cost

| Cases | Concurrency | Cases/hour | Hold plan p50 (s) | Hold plan p95 (s) | Case p50 (s) | Case p95 (s) | $/GPU-hour | $ per case | ₹ per case |
|---|---|---|---|---|---|---|---|---|---|
| 39/39 | 1 | 413.9000 | 0.0612 | 0.0664 | 8.3984 | 11.5554 | 1.9900 | 0.0048 | — |

## Quality (verifier checks on every draft)

| Drafts | Verification pass rate | Cases fully verified | LLM prose drafts | Prose fallback rate | Hold exact match | LLM calls/case | LLM p50 latency (s) |
|---|---|---|---|---|---|---|---|
| 301 | 1.000 | 1.000 | 83 | 0.108 | 1.000 | 2.513 | 3.147 |
