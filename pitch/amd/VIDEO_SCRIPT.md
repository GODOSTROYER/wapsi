# AMD ACT III video: 4:15 (allowed 3:30 to 4:45)

Separate cut from the AI Builder Cup video. **Screen:** the MI300X droplet (two terminals: `amd-smi` and
`docker compose`), the Wapsi UI served from the droplet through an SSH tunnel, `AMD_RESULTS.md`, and slides for the
memory arithmetic and the ask. **Voice:** one narrator, about 520 words. Every number is a row in
[`../NUMBERS.md`](../NUMBERS.md); **TBD** lines are read from `editions/amd/AMD_RESULTS.md` on the day and added to
`NUMBERS.md` before recording.

**Capture rules (from `editions/amd/RUNBOOK.md` step 12)**
- Record on the droplet between 12 and 18 Oct only; keep the date visible once (`date -u` in the telemetry terminal).
- Keep the HF token, IP addresses and SSH keys off screen.
- Run a concurrency-16 `llm_bench` or `e2e_bench` while recording, so utilisation and power move on camera. On an
  SR-IOV virtual function, sensors that do not report show "n/a (SR-IOV VF)"; say so rather than cutting it.

---

## 0:00 to 0:20. The number

**Screen:** baseline strip: `Whole-account freeze: ₹35.26 Cr · 101 accounts` / `Wapsi: ₹29.19 L · 84 accounts` /
`0 false freezes`. Then the words "one MI300X, inside the bank".

**VO:** "When a cyber-fraud victim reports a theft, banks often freeze every account the money touched. On our 39-case
test world that locks ₹35.26 crore. Wapsi holds ₹29.19 lakh, only the victim's money. And it does it on one AMD
GPU, inside the bank."

## 0:20 to 0:45. Why it has to be on-prem

**Screen:** slide: `₹7,647 Cr held · ₹167 Cr returned · 2.18%`, then three icons: victim identities, account trails,
confidential STRs.

**VO:** "India returned ₹167 crore of the ₹7,647 crore it held for fraud victims: 2.18%. Fixing that means an AI
that reads victim identities, account trails and confidential suspicious-transaction reports. That data cannot go to
an outside API. So we put the whole copilot on one MI300X."

## 0:45 to 1:30. One GPU, three models

**Screen:** left terminal `docker compose ps` (llm, verifier-llm, gnn, app all healthy); right terminal
`watch amd-smi` with memory per container. Overlay the memory table: 62 GB weights + 43.6 GB KV cache = 105.6 GB for
the drafting model; 28.8 GB for the verifier; 57.6 GB for the GNN and headroom; 192 GB total.

**VO:** "Here is the droplet. Gemma 4 31B, fine-tuned for STR drafting, runs in full BF16: 62 gigabytes of weights,
plus 43.6 gigabytes of KV cache. Next to it, a smaller Gemma 4 judges untrusted complaint text. Next to that, a graph
neural network scores mule accounts. All on one 192-gigabyte MI300X, no second card, nothing leaving the box.
[Read the measured VRAM per container from amd-smi.]"

## 1:30 to 2:35. The case, end to end on the GPU

**Screen:** Wapsi UI from the droplet. Open showcase case `31009260000101`: trail graph, then the Hold Plan with the
baseline strip, then Drafts & Verification with the STR, model name `wapsi-llm` on each draft, all checks green.
Then demo case `11208260078296`: the Hinglish complaint with the embedded "release all holds" order, flagged by the
judge model and the rules guard; the hold unchanged.

**VO:** "One complaint: ₹4,80,000 stolen through four layers of accounts. The GNN scores every account on the
trail. The plan holds ₹1,42,848 across five accounts, and ₹3,34,848 is recoverable. A whole-account freeze would
have locked ₹5,03,50,000. The fine-tuned Gemma writes the STR's sentences; every amount, account and date comes from
data, and the verifier recomputes all of them. This Hinglish complaint hides an order to release the holds. The judge
model flags it, the officer sees it quoted, and the hold stays."

## 2:35 to 3:20. Measured on MI300X

**Screen:** `AMD_RESULTS.md`: serving table, cases per hour, cost per case, VRAM per container; then the GNN training
curve and the LoRA base-vs-tuned table.

**VO (fill from `AMD_RESULTS.md`):** "Measured on this MI300X on [date]: [TBD] tokens per second at concurrency 16,
time to first token [TBD] at p95, [TBD] complete cases an hour through the real pipeline, at [TBD] per case at $1.99
per GPU-hour. The GNN trained in [TBD] per epoch. Fine-tuning moved the verifier pass rate from [TBD] to [TBD]."

## 3:20 to 3:45. What the guardrails buy

**Screen:** the A/B slide (deck slide 8).

**VO:** "The same cases, drafted two ways. Raw Gemma writing free-form reports: [TBD] drafts with a wrong amount.
Gemma inside Wapsi's slots and verifier: [TBD]. Even when we force the model to invent a number in every paragraph,
not one wrong amount reaches a draft: 27.57% of drafts get their prose replaced, and every draft still passes."

## 3:45 to 4:15. Market and the ask

**Screen:** slide: `1,586 entities on CFCFRMS · 32.80 lakh complaints to 30.06.2026`, the competitor table in one
line ("MuleHunter flags mules; Wapsi returns the money"), then the ask.

**VO:** "1,586 banks and other entities are on CFCFRMS, and money has been saved in more than 32 lakh complaints. Tools
like MuleHunter flag mule accounts; Wapsi does what comes after a complaint: trace, hold the right amount, file, and
return the money. We are asking for AMD Developer Cloud time for a pilot with one bank's fraud cell, and introductions
to banks running Instinct GPUs. Wapsi: give the money back, on one GPU, inside the bank."

---

## Timing check

| Segment | Start | Seconds |
|---|---|---:|
| The number | 0:00 | 20 |
| Why on-prem | 0:20 | 25 |
| One GPU, three models | 0:45 | 45 |
| The case end to end | 1:30 | 65 |
| Measured on MI300X | 2:35 | 45 |
| A/B | 3:20 | 25 |
| Market and ask | 3:45 | 30 |
| **Total** | | **255 s = 4:15** |

- If a measurement is missing on the day, cut its sentence; do not say "about" or estimate.
- "More than 32 lakh complaints" is the spoken form of 32.80 lakh complaints (to 30.06.2026).
