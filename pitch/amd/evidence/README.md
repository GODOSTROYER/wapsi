# Live evidence from the AMD endpoint

This folder holds the live smoke run against an OpenAI-compatible endpoint that serves open models on AMD GPUs
(AMD Token Factory by default), and the results files it writes. It is **not part of the test suite**: the
repository's tests make no network calls.

| File | What |
|---|---|
| `token_factory_smoke.py` | the run: a model-list request, a one-line health probe, showcase case 1 through the real pipeline, one tool-calling turn with a grounded follow-up, and field extraction from two Hinglish complaint scripts |
| `token-factory-smoke-<UTC date>.json` | written only by a healthy run: timings, token counts and pass or fail results. Never a key, a header, a prompt or a reply |

## Run it

```bash
export WAPSI_LLM_BASE_URL="<OpenAI-style base URL ending in /v1>"
export WAPSI_LLM_API_KEY="<your key>"          # never commit it; never paste it into a notebook or a chat
export WAPSI_LLM_MODEL="Gemma-4-31B-IT"
uv run python pitch/amd/evidence/token_factory_smoke.py --probe-only   # one chat request: is the model healthy?
uv run python pitch/amd/evidence/token_factory_smoke.py                # about ten spaced requests
```

The run is sequential, waits at least four seconds between requests, honours HTTP 429 with `Retry-After`, and
caches good responses on disk so a repeat costs nothing. It sends synthetic data only.

## What a healthy run proves, and what it does not

- **It proves** that the pipeline's drafting calls, native tool calling and Hinglish field extraction work
  against the model on AMD, that the holds still equal the hand-computed fixture, and how long each request took
  and how many tokens it used.
- **It does not** benchmark a dedicated MI300X. The shared endpoint is a trial service with request limits; its
  timings are one sequential request at a time from a laptop. Every figure taken from a results file is labelled
  "AMD Token Factory" with the model name and the date.

## The health gate

Every reply is checked for being language at all. If the health probe or any later reply is truncated or
degenerate, the run exits with status 3 and writes nothing (`--keep-unhealthy` writes a file whose name ends in
`-UNHEALTHY.json` and whose label says not to cite it). This matters because a degenerate answer is replaced by
the fixed paragraph inside the pipeline (the prose validator's `not_prose` check): without the gate, a run on a
degraded endpoint would look like a pass while measuring the fallback text, not the model.

**Status on 2026-10-14:** no results file is committed yet. The runs made that day met a degraded Gemma 4 model
on the shared endpoint and recorded nothing, as designed. The figures this run produces are open slots in
[`../../NUMBERS.md`](../../NUMBERS.md) until a healthy run is committed.

## Turning a results file into pitch numbers

1. Commit the results file.
2. In `pitch/NUMBERS.md`, add rows whose source is the file and whose locator is a JSON path (for example
   `json:pipeline.drafts_passing_verifier`). The "Pitched as" text starts with "AMD Token Factory".
3. Replace the matching open slots in the pitch, delete their rows from the slots table, and run
   `uv run python pitch/check_numbers.py`.
