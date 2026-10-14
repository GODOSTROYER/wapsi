# Wapsi on AMD Instinct MI300X — measured results

- Hardware: AMD Instinct MI300X VF · hosts: jupyter-user-261014142914-35b202b3 · commits: fc20109
- Measured on: 2026-10-14 · report generated 2026-10-14T14:51:10.289072+00:00
- Every number below is read from the JSON files listed at the end, produced by `editions/amd/bench/*` runs on the host named above (an MI300X droplet or an AMD GPU notebook; the run's evidence.md says which). Nothing is hand-entered.

## Headline

| Metric | Value |
|---|---|
| wapsi-llm — TTFT p50 at concurrency 1 (s) | 0.131 |
| wapsi-llm — peak output tokens/s (concurrency 64) | 699.220 |
| wapsi-llm — TTFT p50 at concurrency 1 (s) | 0.058 |
| wapsi-llm — peak output tokens/s (concurrency 32) | 772.620 |
| Cases/hour (demo, concurrency 1) | 413.900 |
| Time to hold plan p50 (s) | 0.061 |
| Cost per case (USD at $1.99/GPU-hour) | 0.005 |
| Draft verification pass rate | 1.000 |
| LLM prose fallback rate | 0.108 |
| Cases/hour (demo, concurrency 16) | 1,590.250 |
| Time to hold plan p50 (s) | 0.221 |
| Cost per case (USD at $1.99/GPU-hour) | 0.001 |
| Draft verification pass rate | 1.000 |
| LLM prose fallback rate | 0.108 |
| Cases/hour (demo, concurrency 8) | 1,409.950 |
| Time to hold plan p50 (s) | 0.064 |
| Cost per case (USD at $1.99/GPU-hour) | 0.001 |
| Draft verification pass rate | 1.000 |
| LLM prose fallback rate | 0.120 |
| GPU0 peak VRAM (GB of 191.7) | 97.036 |

## Serving: `wapsi-llm`

Root `google/gemma-4-31B-it` · max_model_len 16384 · prompts synthetic · input ≈ 1024 tok · output 256 tok

| Concurrency | OK | TTFT p50 (s) | TTFT p95 (s) | ITL p50 (s) | TPOT p50 (s) | Output tok/s | Tok/s per user | Req/s |
|---|---|---|---|---|---|---|---|---|
| 1 | 8/8 | 0.131 | 0.131 | 0.018 | 0.018 | 55.370 | 56.760 | 0.216 |
| 4 | 16/16 | 0.450 | 0.451 | 0.020 | 0.020 | 181.030 | 48.500 | 0.707 |
| 16 | 64/64 | 1.156 | 1.682 | 0.027 | 0.029 | 474.350 | 34.550 | 1.853 |
| 64 | 256/256 | 4.533 | 16.048 | 0.037 | 0.048 | 699.220 | 19.960 | 2.731 |

## Serving: `wapsi-llm`

Root `google/gemma-4-31B-it` · max_model_len 16384 · prompts wapsi · input ≈ 1024 tok · output 256 tok

| Concurrency | OK | TTFT p50 (s) | TTFT p95 (s) | ITL p50 (s) | TPOT p50 (s) | Output tok/s | Tok/s per user | Req/s |
|---|---|---|---|---|---|---|---|---|
| 1 | 8/8 | 0.058 | 0.769 | 0.019 | 0.019 | 48.740 | 52.740 | 0.317 |
| 8 | 32/32 | 0.087 | 0.146 | 0.025 | 0.026 | 272.920 | 39.020 | 1.731 |
| 32 | 128/128 | 0.120 | 0.381 | 0.036 | 0.036 | 772.620 | 27.850 | 4.855 |

## End to end: demo (39/39 cases, concurrency 1)

LLM `vllm-rocm:wapsi-llm` · scorer `heuristic-v2` · guard `rules`

| Cases/hour | Hold plan p50 (s) | Hold plan p95 (s) | Case p50 (s) | Case p95 (s) | LLM calls/case | Verification pass | Prose fallback | Hold exact match | $ per case | ₹ per case |
|---|---|---|---|---|---|---|---|---|---|---|
| 413.9000 | 0.0612 | 0.0664 | 8.3984 | 11.5554 | 2.5128 | 1.0000 | 0.1084 | 1.0000 | 0.0048 | — |

## End to end: demo (39/39 cases, concurrency 16)

LLM `vllm-rocm:wapsi-llm` · scorer `heuristic-v2` · guard `rules`

| Cases/hour | Hold plan p50 (s) | Hold plan p95 (s) | Case p50 (s) | Case p95 (s) | LLM calls/case | Verification pass | Prose fallback | Hold exact match | $ per case | ₹ per case |
|---|---|---|---|---|---|---|---|---|---|---|
| 1,590.2500 | 0.2208 | 1.1245 | 31.1142 | 47.2548 | 2.5641 | 1.0000 | 0.1084 | 1.0000 | 0.0013 | — |

## End to end: demo (39/39 cases, concurrency 8)

LLM `vllm-rocm:wapsi-llm` · scorer `heuristic-v2` · guard `rules`

| Cases/hour | Hold plan p50 (s) | Hold plan p95 (s) | Case p50 (s) | Case p95 (s) | LLM calls/case | Verification pass | Prose fallback | Hold exact match | $ per case | ₹ per case |
|---|---|---|---|---|---|---|---|---|---|---|
| 1,409.9500 | 0.0642 | 0.4676 | 17.7234 | 27.8412 | 2.5385 | 1.0000 | 0.1205 | 1.0000 | 0.0014 | — |

## GPU memory and utilisation (source `rocm-smi`, 367 samples every 2.0 s)

| GPU | Name | VRAM total (GB) | Peak used (GB) | Mean used (GB) | GFX mean % | GFX peak % | Power mean (W) |
|---|---|---|---|---|---|---|---|
| 0 | AMD Instinct MI300X VF | 191.7 | 97.0 | 96.8 | 97.0 | 100.0 | 733.0 |

## Sources

- `editions/amd/evidence/notebook/2026-10-14/bench/llm_bench.json`
- `editions/amd/evidence/notebook/2026-10-14/bench/llm_bench_wapsi_prompts.json`
- `editions/amd/evidence/notebook/2026-10-14/bench/e2e_c1.json`
- `editions/amd/evidence/notebook/2026-10-14/bench/e2e_c16.json`
- `editions/amd/evidence/notebook/2026-10-14/bench/e2e_c8.json`
- `editions/amd/evidence/notebook/2026-10-14/bench/telemetry_summary.json`

## Reproduce

See `editions/amd/RUNBOOK.md` (bench section): `gpu_telemetry.py` on the host, `llm_bench.py` against the vLLM endpoint, `e2e_bench.py` over the golden cases, `voice_bench.py` for voice intake, then this report.
