# Voice intake benchmark — sample scripts (stand-in) + Gemma 4 31B on AMD

- Speech-to-text: `sample scripts` (fake) · text model: `Gemma-4-31B-IT` served by AMD Token Factory · JSON mode json_object · generated 2026-10-14T01:36:47.153073+00:00
- Speech-to-text: stand-in (the samples' scripts were used as transcripts). No speech model ran, so there is no real-time factor in this run.
- Translation and field extraction: Gemma-4-31B-IT (6 answers), served by the AMD Token Factory (AMD GPUs on the provider side). Latency includes the network; this is not an MI300X droplet benchmark.

| Metric | Value |
|---|---|
| Voice notes read | 6/6 |
| ASR real-time factor p50 / p95 | — / — |
| ASR seconds p50 | — |
| Extraction latency p50 / p95 (s) | 7.498 / 14.061 |
| Voice note to checked fields p50 (s) | 7.498 |
| Prompt / output tokens (mean) | 2,348.500 / 384.000 |
| Reported cost, all runs (USD) | 2.04e-04 |
| Samples reconciled as documented | 4/6 |
| Extracted values agreeing with the expected answer | 0.983 |
| Answers repaired before validation / retried | 0 / 0 |
| Answers by model | Gemma-4-31B-IT: 6 |
| Runs answered by a fallback model | 0 |
| Runs replayed from the LLM cache | 0 |

| Voice note | Audio (s) | ASR (s) | RTF | Text model | Extract (s) | Prompt tok | Output tok | Attempts | As documented | Values agree | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|
| meera-impersonation | 30.930 | — | — | Gemma-4-31B-IT | 9.577 | 2,361 | 389 | 1 | no | 10/10 |  |
| arjun-kyc-two-payments | 30.830 | — | — | Gemma-4-31B-IT | 15.555 | 2,362 | 444 | 1 | no | 10/10 |  |
| fatima-courier-injection | 26.750 | — | — | Gemma-4-31B-IT | 6.619 | 2,337 | 339 | 1 | yes | 10/10 |  |
| swati-task-scam | 26.290 | — | — | Gemma-4-31B-IT | 6.996 | 2,342 | 352 | 1 | yes | 9/10 |  |
| amit-courier-two-payments | 27.970 | — | — | Gemma-4-31B-IT | 8.000 | 2,354 | 431 | 1 | yes | 10/10 |  |
| kavitha-courier-injection | 27.000 | — | — | Gemma-4-31B-IT | 6.638 | 2,335 | 349 | 1 | yes | 10/10 |  |
