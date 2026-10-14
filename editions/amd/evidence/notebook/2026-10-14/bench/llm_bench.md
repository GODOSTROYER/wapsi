# LLM serving benchmark — `wapsi-llm`

- Label: **AMD ROCm notebook run, 2026-10-14**
- Endpoint: `http://127.0.0.1:8000/v1` · model root: `google/gemma-4-31B-it` · max_model_len: 16384
- Prompts: synthetic · input ≈ 1024 tokens · output 256 tokens · ignore_eos=True · generated 2026-10-14T14:37:48.664888+00:00

| Concurrency | OK | TTFT p50 (s) | TTFT p95 (s) | ITL p50 (s) | TPOT p50 (s) | Output tok/s | Tok/s per user | Req/s |
|---|---|---|---|---|---|---|---|---|
| 1 | 8/8 | 0.131 | 0.131 | 0.018 | 0.018 | 55.370 | 56.760 | 0.216 |
| 4 | 16/16 | 0.450 | 0.451 | 0.020 | 0.020 | 181.030 | 48.500 | 0.707 |
| 16 | 64/64 | 1.156 | 1.682 | 0.027 | 0.029 | 474.350 | 34.550 | 1.853 |
| 64 | 256/256 | 4.533 | 16.048 | 0.037 | 0.048 | 699.220 | 19.960 | 2.731 |
