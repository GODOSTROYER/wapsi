# Voice intake on the AMD Token Factory: live smoke runs, 14 October 2026

Two runs of `editions/amd/bench/voice_bench.py --asr scripts` against the AMD Token Factory
(`https://developer.amd.com.cn/radeon/api/v1`), made from a developer laptop during the event. `AMD_RESULTS.md` is
`report.py` over the two JSON files.

| File | When (UTC) | Text model that answered | Extraction latency p50 / p95 | Reconciled as documented | Values agreeing with the expected answer |
|---|---|---|---|---|---|
| `voice_bench_deepseek_failover.json` | 01:08 | DeepSeek-V4-Flash (6 of 6) | 26.3 s / 31.9 s | 5 of 6 | 91.7% |
| `voice_bench_gemma4.json` | 01:36 | Gemma-4-31B-IT (6 of 6) | 7.5 s / 14.1 s | 4 of 6 | 98.3% |

## What these numbers are

- **Text model only.** `--asr scripts` uses each sample's script as the transcript, so no speech model ran and
  there is no real-time factor here. The scripts spell numbers out digit by digit, as a recogniser would.
- **AMD-served, not a droplet benchmark.** The models run on AMD GPUs on the provider's side. Latency is measured
  at the client and includes the network. These are not MI300X droplet numbers.
- **Six synthetic samples, one pass each.** A smoke test, not an evaluation.

## What happened

- At 01:08 UTC `Gemma-4-31B-IT` was failing on the shared endpoint: it answered HTTP 200 with degenerate output
  for every prompt. Its one-line health probe failed, it was skipped, and `DeepSeek-V4-Flash` answered all six
  notes (`models_tried` on every run). Nothing from the broken model reached a result.
- By 01:36 UTC it had recovered and answered all six. Every field, amount, date and reference agreed with the
  expected answer except one account number written without its hyphen, which still matched the ledger.
- `DeepSeek-V4-Flash` returned the name "Ramesh" for the caller who said "Arjun Nair", and misread two amounts.
  The ledger reconciliation showed each as a mismatch and the note still matched its complaint through the UTR.
  A name that is not in the transcript is now also marked low-confidence in code (`ground_numbers`).
- "4 of 6" for Gemma is two false alarms, not two wrong extractions: it listed the callers' closing pleas
  ("Mera paisa wapas dila dijiye.", "Jaldi kijiye please.") as instruction-like text, so those two clean notes
  were flagged. The prompt was tightened after this run (a plea for help is not instruction-like). That change
  has **not** been measured live yet.

## Provenance

- `voice_bench_gemma4.json` was produced by the code at commit `fb43b54`.
- `voice_bench_deepseek_failover.json` was produced from the working tree that became `fb43b54`, before it was
  committed; its `env.git_commit` field therefore still names the parent commit.
- The client's host name was removed from both files (`env.host`). Nothing else was edited.
- Re-run: `uv run python -m editions.amd.bench.voice_bench --asr scripts --out <dir>` with `WAPSI_LLM_BASE_URL`
  and `WAPSI_LLM_API_KEY` in the environment (seven requests when the first model is healthy).
