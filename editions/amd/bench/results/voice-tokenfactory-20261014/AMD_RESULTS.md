# Wapsi on AMD — voice intake results

- Client hosts: not recorded · commits: 1d500f6, fb43b54
- Measured on: 2026-10-14 · report generated 2026-10-14T01:39:44.695090+00:00
- Every number below is read from the JSON files listed at the end, produced by `editions/amd/bench/voice_bench.py`. Nothing is hand-entered. Each section states where its models ran.

## Headline

| Metric | Value |
|---|---|
| Voice intake (sample scripts (stand-in) + DeepSeek-V4-Flash on AMD) — transcript to checked fields p50 (s) | 26.346 |
| Voice intake (sample scripts (stand-in) + DeepSeek-V4-Flash on AMD) — samples reconciled as documented | 5/6 |
| Voice intake (sample scripts (stand-in) + Gemma 4 31B on AMD) — transcript to checked fields p50 (s) | 7.498 |
| Voice intake (sample scripts (stand-in) + Gemma 4 31B on AMD) — samples reconciled as documented | 4/6 |

## Voice intake: sample scripts (stand-in) + DeepSeek-V4-Flash on AMD

- Speech-to-text: stand-in (the samples' scripts were used as transcripts). No speech model ran, so there is no real-time factor in this run.
- Translation and field extraction: DeepSeek-V4-Flash (6 answers), served by the AMD Token Factory (AMD GPUs on the provider side). Latency includes the network; this is not an MI300X droplet benchmark.
- The first model in the chain is Gemma-4-31B-IT; where another model answered, the first one failed its health probe or returned unusable output and was rejected (see models_tried).
- Speech-to-text `sample scripts` (fake) · text model chain head `Gemma-4-31B-IT` · JSON mode json_object · measured 2026-10-14T01:08:00

| Voice notes | RTF p50 | RTF p95 | ASR p50 (s) | Extract p50 (s) | Extract p95 (s) | Note to fields p50 (s) | Output tok (mean) | As documented | Values agree | Repaired | Fallback model |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 6/6 | — | — | — | 26.346 | 31.934 | 26.346 | 432.200 | 5/6 | 0.917 | 1 | 6 |

Answers per text model (a number is credited only to the model that produced it):

| Model | Answers | Extract p50 (s) | Extract p95 (s) |
|---|---|---|---|
| DeepSeek-V4-Flash | 6 | 26.346 | 31.934 |

## Voice intake: sample scripts (stand-in) + Gemma 4 31B on AMD

- Speech-to-text: stand-in (the samples' scripts were used as transcripts). No speech model ran, so there is no real-time factor in this run.
- Translation and field extraction: Gemma-4-31B-IT (6 answers), served by the AMD Token Factory (AMD GPUs on the provider side). Latency includes the network; this is not an MI300X droplet benchmark.
- Speech-to-text `sample scripts` (fake) · text model chain head `Gemma-4-31B-IT` · JSON mode json_object · measured 2026-10-14T01:36:47

| Voice notes | RTF p50 | RTF p95 | ASR p50 (s) | Extract p50 (s) | Extract p95 (s) | Note to fields p50 (s) | Output tok (mean) | As documented | Values agree | Repaired | Fallback model |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 6/6 | — | — | — | 7.498 | 14.061 | 7.498 | 384.000 | 4/6 | 0.983 | 0 | 0 |

Answers per text model (a number is credited only to the model that produced it):

| Model | Answers | Extract p50 (s) | Extract p95 (s) |
|---|---|---|---|
| Gemma-4-31B-IT | 6 | 7.498 | 14.061 |

## Sources

- `editions\amd\bench\results\voice-tokenfactory-20261014\voice_bench_deepseek_failover.json`
- `editions\amd\bench\results\voice-tokenfactory-20261014\voice_bench_gemma4.json`

## Reproduce

See `editions/amd/RUNBOOK.md` (bench section): `gpu_telemetry.py` on the host, `llm_bench.py` against the vLLM endpoint, `e2e_bench.py` over the golden cases, `voice_bench.py` for voice intake, then this report.
