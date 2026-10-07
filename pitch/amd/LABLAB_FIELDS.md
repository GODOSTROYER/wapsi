# lablab.ai submission fields (AMD Developer Hackathon ACT III)

Submit by **18 Oct 2026, 17:30 IST** (our internal stop; the official deadline is 14:00 UTC). Counts are checked by
`uv run python pitch/check_numbers.py`; every number is a row in [`../NUMBERS.md`](../NUMBERS.md).

## Project title (limit 50 characters)

<!-- limit:50 -->
Wapsi: On-Prem Fraud Restoration AI on MI300X
<!-- /limit -->

Length: 45 / 50 characters

## Short description (limit 255 characters)

<!-- limit:255 -->
Wapsi traces cyber-fraud money through mule accounts, holds only the victim's share instead of freezing whole accounts, and drafts verified STRs. Two Gemma 4 models and a GNN run on one AMD MI300X, so bank data never leaves the building.
<!-- /limit -->

Length: 237 / 255 characters

## Long description (target 1,500 characters)

<!-- limit:1500 -->
India held ₹7,647 Cr for cyber-fraud victims through CFCFRMS from April 2021 to November 2025 and restored ₹167 Cr: 2.18% (MHA/I4C SOP, 2026). Whole-account freezes lock innocent customers, merchants and payment-aggregator pools, and restoration stalls.

Wapsi is a copilot for a bank's cyber-fraud nodal officer. It traces a complaint's money through layers of mule accounts, splits each account's balance pro-rata between victims (the SOP's own method, reproduced to the paisa), and holds only the victim's money. It drafts the STR, hold instructions and restoration packet. The LLM writes prose only: every amount, account, UTR and date is filled from data and recomputed by a verifier before a supervisor approves.

The AMD edition runs all of it inside the bank on one MI300X (192 GB): Gemma 4 31B with an STR-drafting LoRA in BF16 on vLLM-ROCm, a Gemma 4 E4B judge for untrusted complaint text, and a GraphSAGE mule scorer in PyTorch on ROCm, next to the app and the ledger. No external API sees customer data.

On a 39-case synthetic test world, a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84, with 0 false freezes and every hold exact. On a harder 64-case world built to fool the classifier, dispositions are 91.98% right and holds stay exact. Benchmarks measured on the MI300X during the event are in AMD_RESULTS.md.
<!-- /limit -->

Length: 1,361 / 1,500 characters

## Technologies and tags

AMD Instinct MI300X · AMD Developer Cloud · ROCm · vLLM · Gemma 4 · LoRA · PyTorch · Graph Neural Network ·
FastAPI · Streamlit · Docker Compose · Python · BFSI · Fraud · RegTech

## Category

Open / product track, if ACT III has one **[check the track list at kickoff, 12 Oct 15:00 UTC]**.

## Links (fill on submission day)

| Field | Value |
|---|---|
| Video (3:30 to 4:45) | **[YouTube link of the 4:15 cut, `VIDEO_SCRIPT.md`]** |
| Slides (PDF) | **[PDF exported from `DECK_OUTLINE.md`]** |
| Repository (MIT) | **[public GitHub URL; tag `amd-baseline` at kickoff and the submission tag]** |
| Demo URL | **[demo-mode app, CPU and stub LLM, preloaded with MI300X case bundles; say so on the page]** |
| Cover image | the baseline strip (₹35.26 Cr frozen vs ₹29.19 L held) over an MI300X photo from AMD's press kit **[check its licence]** |

## Originality statement (paste into the description or the README section it links to)

The portable Wapsi core, the compose stack, the benchmark harness and the runbook were prepared before the event and
are tagged `amd-baseline`. Everything that ran on AMD hardware was done between 12 and 18 Oct 2026 on the MI300X: GNN
training, the Gemma 4 LoRA fine-tune and merge, vLLM-ROCm serving and every benchmark. Details in
`docs/originality.md`.
