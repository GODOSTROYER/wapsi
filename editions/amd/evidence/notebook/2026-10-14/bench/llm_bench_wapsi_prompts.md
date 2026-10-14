# LLM serving benchmark — `wapsi-llm`

- Label: **AMD ROCm notebook run, 2026-10-14**
- Endpoint: `http://127.0.0.1:8000/v1` · model root: `google/gemma-4-31B-it` · max_model_len: 16384
- Prompts: wapsi · input ≈ 1024 tokens · output 256 tokens · ignore_eos=True · generated 2026-10-14T14:40:59.909100+00:00

| Concurrency | OK | TTFT p50 (s) | TTFT p95 (s) | ITL p50 (s) | TPOT p50 (s) | Output tok/s | Tok/s per user | Req/s |
|---|---|---|---|---|---|---|---|---|
| 1 | 8/8 | 0.058 | 0.769 | 0.019 | 0.019 | 48.740 | 52.740 | 0.317 |
| 8 | 32/32 | 0.087 | 0.146 | 0.025 | 0.026 | 272.920 | 39.020 | 1.731 |
| 32 | 128/128 | 0.120 | 0.381 | 0.036 | 0.036 | 772.620 | 27.850 | 4.855 |
