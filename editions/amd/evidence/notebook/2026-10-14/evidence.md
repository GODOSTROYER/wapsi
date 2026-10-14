# Wapsi on MI300X (AMD ROCm notebook): evidence, 2026-10-14

- Label: AMD ROCm notebook run (lablab.ai AMD Developer Hackathon ACT III)
- Where it ran: AMD ROCm notebook (no Docker), AMD Instinct MI300X VF
- Results valid: **yes**
- Run: 2026-10-14T13:34:59Z .. 2026-10-14T15:23:16Z UTC · commit `fc2010957f8ac923045ea765a9ce0bc3be1911e4` · amd-baseline `1d500f6876ea7e0a97eebcb0328d9f779cc16f97` · served profile gemma4-base
- Event window: 2026-10-12T15:00:00Z .. 2026-10-18T14:00:00Z
- Generated 2026-10-14T15:36:50Z by `editions/amd/kickoff/collect_evidence.py` from the files in this folder. Every value is read from a result file (see `sources` in evidence.json).

## Hardware (`amd-smi static`)

| GPU | Name | SR-IOV VF | ROCm |
|---|---|---|---|
| 0 | AMD Instinct MI300X VF | yes | n/a |

Models: drafting `google/gemma-4-31B-it` (largest candidate that serves in 192 GB), judge `google/gemma-4-E4B-it`, LoRA preset gemma4-31b-mi300x.

## Headline

| Metric | Value |
|---|---|
| Drafting model output tokens/s at concurrency 1 / 4 / 16 / 64 | 55.4 / 181 / 474.4 / 699.2 |
| Request latency p50 / p95 at concurrency 1 (s) | 4.62 / 4.63 |
| TTFT p50 / p95 at concurrency 1 (s, as the client sees it) | 0.131 / 0.131 |
| Output tokens/s per request, p50, at concurrency 1 / 4 / 16 / 64 | 55.4 / 45.2 / 29.7 / 12.6 |
| Real drafting prompts: request latency p50 / p95 at concurrency 1 (s) | 3.07 / 3.73 |
| Cases per hour at concurrency 1 / 8 / 16 | 413.9 / 1,410 / 1,590.2 |
| Drafts per hour (best run) | 12,273.5 |
| LLM calls per case / call latency p50 / p95 (s) / LLM seconds per case p50 | 2.56 / 12.06 / 20.56 / 30.81 |
| Draft verification pass rate / prose fallback rate | 1 / 0.1084 |
| Drafts per GPU-hour (best concurrency) | 12,273.5 |
| $ per case (best concurrency) | 0.00125138 |
| ₹ per case (best concurrency, the day's rate) | n/a |
| Peak VRAM: llm / verifier / gnn / device (GB) | n/a / n/a / n/a / 97 |
| GNN: s per epoch / validation AUC / peak memory (GB) | 0.055 / 1 / 0.39 |
| GNN on the hard world: dispositions / mule AUC / innocent-holder recall | 0.9198 / 0.9202 / 0.875 |
| LoRA: verifier pass rate base -> tuned | n/a / n/a |
| LoRA: prose fallback rate base -> tuned | n/a / n/a |
| LoRA training: tokens/s / peak memory (GB) | n/a / n/a |
| Golden set with this model: cases / hold exact match / false freezes | 39 / 1 / 0 |
| Golden set: draft numeric accuracy / citation validity / verifier pass rate | 1 / 1 / 1 |
| Golden set: prose fallback rate / injection leaks | 0.0332 / 0 |
| Same model without slots (LLM-only baseline): cases with an amount not in the case | 0.4872 |
| A/B like with like, STRs with a wrong amount: raw / bound | 4 / 0 |
| A/B STRs compared: raw / bound | 20 / 17 |
| A/B all bound drafts with a wrong amount / all bound drafts | 0 / 155 |
| A/B invented account numbers: raw / bound | 0 / 0 |
| A/B unknown or repealed citations: raw / bound | 0 / 0 |
| A/B clean documents rate: raw / bound | 0.8 / 1 |

## End to end (golden cases through the pipeline)

| Concurrency | Cases | Cases/hour | Drafts/GPU-hour | Hold plan p50 (s) | Case p95 (s) | Verifier pass | Prose fallback | $ per case | ₹ per case | File |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 39/39 | 413.9 | 3,194.4 | 0.0612 | 11.56 | 1 | 0.1084 | 0.00480792 | n/a | bench/e2e_c1.json |
| 16 | 39/39 | 1,590.25 | 12,273.5 | 0.2208 | 47.25 | 1 | 0.1084 | 0.00125138 | n/a | bench/e2e_c16.json |
| 8 | 39/39 | 1,409.95 | 10,881.9 | 0.0642 | 27.84 | 1 | 0.1205 | 0.0014114 | n/a | bench/e2e_c8.json |

## Serving: Drafting model `wapsi-llm`

Label: AMD ROCm notebook run, 2026-10-14

| Concurrency | OK | Output tok/s | TTFT p50 (s) | TTFT p95 (s) | Latency p95 (s) | Req/s |
|---|---|---|---|---|---|---|
| 1 | 8/8 | 55.37 | 0.1307 | 0.1313 | 4.63 | 0.2163 |
| 4 | 16/16 | 181.03 | 0.45 | 0.4511 | 5.66 | 0.7071 |
| 16 | 64/64 | 474.35 | 1.16 | 1.68 | 8.66 | 1.85 |
| 64 | 256/256 | 699.22 | 4.53 | 16.05 | 29.37 | 2.73 |

## Serving: Drafting model, real drafting prompts `wapsi-llm`

Label: AMD ROCm notebook run, 2026-10-14

| Concurrency | OK | Output tok/s | TTFT p50 (s) | TTFT p95 (s) | Latency p95 (s) | Req/s |
|---|---|---|---|---|---|---|
| 1 | 8/8 | 48.74 | 0.0576 | 0.7691 | 3.73 | 0.3173 |
| 8 | 32/32 | 272.92 | 0.0873 | 0.1456 | 4.94 | 1.73 |
| 32 | 128/128 | 772.62 | 0.1203 | 0.3812 | 7.53 | 4.86 |

## Stages

| # | Stage | Status | Started (UTC) | Ended (UTC) | Seconds | Window |
|---|---|---|---|---|---|---|
| 1 | env | ok | 2026-10-14T14:29:54Z | 2026-10-14T14:31:38Z | 104 | inside |
| 2 | models | ok | 2026-10-14T14:31:45Z | 2026-10-14T14:32:32Z | 47 | inside |
| 3 | gnn | ok | 2026-10-14T14:57:18Z | 2026-10-14T15:23:03Z | 1,545 | inside |
| 8 | serve | ok | 2026-10-14T14:32:32Z | 2026-10-14T14:35:50Z | 198 | inside |
| 9 | eval | ok | 2026-10-14T13:34:59Z | 2026-10-14T13:57:40Z | 1,361 | inside |
| 10 | bench | ok | 2026-10-14T14:35:52Z | 2026-10-14T14:51:10Z | 918 | inside |
| 11 | ab | ok | 2026-10-14T14:51:12Z | 2026-10-14T14:54:42Z | 210 | inside |
| 12 | evidence | ok | 2026-10-14T15:23:16Z | 2026-10-14T15:23:16Z | 0 | inside |

Budget: not shown. The stage records counted GPU hours from the notebook host's boot time (2026-10-02T14:47:52Z), not from the start of the session, so their total is not what this run cost; the session times are in the stage table.

Not measured in this run: api_usd_per_case_c1, api_usd_per_case_c16, api_usd_per_case_c8, e2e_best_api_inr_per_case, e2e_best_api_usd_per_case, e2e_best_inr_per_case, inr_per_case_c1, inr_per_case_c16, inr_per_case_c8, inr_per_usd.
