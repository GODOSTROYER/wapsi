# Voice intake benchmark — sample scripts (stand-in) + DeepSeek-V4-Flash on AMD

- Speech-to-text: `sample scripts` (fake) · text model: `Gemma-4-31B-IT` served by AMD Token Factory · JSON mode json_object · generated 2026-10-14T01:08:00.813554+00:00
- Speech-to-text: stand-in (the samples' scripts were used as transcripts). No speech model ran, so there is no real-time factor in this run.
- Translation and field extraction: DeepSeek-V4-Flash (6 answers), served by the AMD Token Factory (AMD GPUs on the provider side). Latency includes the network; this is not an MI300X droplet benchmark.
- The first model in the chain is Gemma-4-31B-IT; where another model answered, the first one failed its health probe or returned unusable output and was rejected (see models_tried).

| Metric | Value |
|---|---|
| Voice notes read | 6/6 |
| ASR real-time factor p50 / p95 | — / — |
| ASR seconds p50 | — |
| Extraction latency p50 / p95 (s) | 26.346 / 31.934 |
| Voice note to checked fields p50 (s) | 26.346 |
| Prompt / output tokens (mean) | 2,272.200 / 432.200 |
| Reported cost, all runs (USD) | 0.002 |
| Samples reconciled as documented | 5/6 |
| Extracted values agreeing with the expected answer | 0.917 |
| Answers repaired before validation / retried | 1 / 0 |
| Answers by model | DeepSeek-V4-Flash: 6 |
| Runs answered by a fallback model | 6 |
| Runs replayed from the LLM cache | 0 |

| Voice note | Audio (s) | ASR (s) | RTF | Text model | Extract (s) | Prompt tok | Output tok | Attempts | As documented | Values agree | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|
| meera-impersonation | 30.930 | — | — | DeepSeek-V4-Flash | 28.149 | 2,288 | 365 | 1 | yes | 10/10 |  |
| arjun-kyc-two-payments | 30.830 | — | — | DeepSeek-V4-Flash | 26.102 | 2,287 | 536 | 1 | no | 6/10 |  |
| fatima-courier-injection | 26.750 | — | — | DeepSeek-V4-Flash | 18.075 | 2,264 | 419 | 1 | yes | 10/10 |  |
| swati-task-scam | 26.290 | — | — | DeepSeek-V4-Flash | 22.672 | 2,263 | 372 | 1 | yes | 9/10 |  |
| amit-courier-two-payments | 27.970 | — | — | DeepSeek-V4-Flash | 26.589 | 2,277 | 545 | 1 | yes | 10/10 |  |
| kavitha-courier-injection | 27.000 | — | — | DeepSeek-V4-Flash | 33.196 | 2,254 | 356 | 1 | yes | 10/10 |  |
