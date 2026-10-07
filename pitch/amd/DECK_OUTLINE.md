# AMD Developer Hackathon ACT III deck: 12 slides

lablab scores application of technology, presentation, business value and originality, 1 to 5 each. This deck
leads with **why this needs one big AMD GPU inside a bank**, shows dated MI300X evidence, and is honest about what was
prepared before 12 Oct. Every number is a row in [`../NUMBERS.md`](../NUMBERS.md). **TBD** cells are filled only
from `editions/amd/bench/results/<stamp>/` and `editions/amd/AMD_RESULTS.md` produced on the MI300X between 12 and
18 Oct 2026; never from memory, never from a CPU run.

---

## 1. Title

- **Wapsi: the whole cyber-fraud restoration copilot on one AMD Instinct MI300X, inside the bank.**
- Hero line: a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84, with 0 false
  freezes (39-case synthetic test world). No byte of customer data leaves the machine.

## 2. The problem

- ₹7,647 Cr held for cyber-fraud victims through CFCFRMS, ₹167 Cr restored: 2.18% (MHA/I4C SOP, 02.01.2026).
- Banks freeze whole accounts; innocent holders, merchants and pool accounts get locked; restoration stalls.
- The nodal officer traces mule layers by hand and must file an STR within 7 working days.
- And the data is the most sensitive a bank has: victim identities, account trails, and STRs that must stay
  confidential from the account holder. Sending it to an external LLM API is a non-starter for most banks.

## 3. What Wapsi does

Showcase case (hand-computed, synthetic): ₹4,80,000 stolen through four layers of accounts.
- Hold only the victim's money: A ₹43,200 · B ₹27,648 · D ₹48,000 · L ₹4,800 · P ₹19,200 = **₹1,42,848** in 5
  accounts, plus ₹1,92,000 requested from the other bank: **₹3,34,848 recoverable**.
- The whole-account freeze baseline would lock ₹5,03,50,000, 352× more.
- Drafts the STR, hold instructions and the restoration packet; the LLM writes prose only; a verifier recomputes every
  number and citation; a supervisor approves.

## 4. Why AMD, part 1: it has to run on-prem

- **One machine holds everything:** ledger, case store, two language models, a graph neural network, the app.
  No request leaves the box; there is no external LLM API.
- **Open weights, bank-controlled:** Gemma 4 31B with an STR-drafting LoRA merged in, served by vLLM on ROCm; a second,
  smaller Gemma 4 (E4B) as an independent judge; a GraphSAGE mule scorer in pure PyTorch on ROCm.
- **Why one GPU matters:** a bank's fraud cell will not run a GPU cluster. One MI300X in the bank's own rack is a
  procurement conversation it can have.

## 5. Why AMD, part 2: the memory arithmetic

| On one MI300X, 192 GB HBM3 | Share | Memory |
|---|---:|---:|
| Gemma 4 31B drafting model, BF16 weights (31B parameters × 2 bytes) | | 62 GB |
| its KV cache, 16384-token context | | 43.6 GB |
| **drafting model total** (`--gpu-memory-utilization 0.55`) | 0.55 | 105.6 GB |
| Gemma 4 E4B verifier / injection judge (`0.15`) | 0.15 | 28.8 GB |
| GraphSAGE mule scorer (78k parameters), app, headroom | rest | 57.6 GB |

- The 31B model fits unquantised in BF16, with a long KV cache, next to a second model and the GNN, **on one GPU**, with
  no tensor parallelism and no second card.
- Measured VRAM per container from `amd-smi` replaces the "Memory" column: **[TBD Oct 12 to 18]**.

## 6. Architecture on the droplet

```
AMD Developer Cloud droplet: 1× MI300X (192 GB), ROCm, docker compose, everything bound to 127.0.0.1
  llm           vLLM-ROCm · Gemma 4 31B + STR LoRA (merged) · 0.55 of VRAM · prose fields only
  verifier-llm  vLLM-ROCm · Gemma 4 E4B · 0.15 · judge for untrusted complaint text
  gnn           PyTorch ROCm · GraphSAGE mule scorer · POST /score (heuristic fallback if down)
  app           FastAPI + Streamlit · wapsi.pipeline: trace → classify → hold plan → draft → verify
  data          synthetic Parquet ledger · file case store with a hash-chained audit log
  telemetry     bench/gpu_telemetry.py on the host (amd-smi), SR-IOV aware
```

## 7. Benchmarks on MI300X (all TBD, 12 to 18 Oct)

| Metric | Value | Run |
|---|---:|---|
| Drafting model throughput, tokens/s at concurrency 1 / 4 / 16 / 64 | **TBD** | `llm_bench` |
| Time to first token p50 / p95 at concurrency 16 | **TBD** | `llm_bench` |
| Verifier throughput, tokens/s at concurrency 1 / 4 / 16 | **TBD** | `llm_bench` |
| End-to-end cases per hour, demo world, concurrency 1 / 8 / 16 | **TBD** | `e2e_bench` |
| Cost per case at $1.99 per GPU-hour (and ₹ at the day's rate) | **TBD** | `e2e_bench` |
| VRAM per container (llm, verifier, gnn) | **TBD** | `gpu_telemetry` |
| GNN training on MI300X: time per epoch, validation AUC | **TBD** | `gnn/train.py` |
| LoRA: base vs tuned, verifier pass rate and prose fallback rate | **TBD** | `lora/` evaluate |

Every row carries its UTC stamp and results folder. On an SR-IOV virtual function, sensors that read nothing are
shown as "n/a (SR-IOV VF)", never as 0.

## 8. A/B: what the guardrails buy

**Slot 1, drafting (TBD on MI300X):** the same golden cases drafted two ways by Gemma 4 31B.

| | Raw Gemma, free-form STR | Wapsi-bound Gemma (slots + verifier) |
|---|---:|---:|
| Drafts with a wrong amount | **TBD** | **TBD** (target 0) |
| Invented account numbers | **TBD** | **TBD** |
| Unknown or repealed citations | **TBD** | **TBD** |
| Drafts passing the verifier | **TBD** | **TBD** |

What we already know from the stub-LLM stress test: a model that writes a wrong rupee amount into every prose field
gets 27.57% of drafts' prose replaced, and drafts passing the verifier stay 1.0 (100%).

**Slot 2, classification on the hard world (baseline to beat):** heuristic dispositions 0.9198, mule ROC-AUC 0.9411,
innocent-holder recall 0.6875. A **CPU development** GNN checkpoint, prepared before the event: dispositions 0.9151,
mule ROC-AUC 0.9786, innocent-holder recall 0.875. The MI300X-trained GNN replaces that column: **TBD**.

## 9. Honest accuracy

- Holds exact to the paisa in every world, including the hard world (64 cases, 15 typologies): over-hold ₹0,
  0 false freezes. Dispositions there: 0.9198; every miss is a label, never money.
- Ground truth comes from an independent reference implementation; the SOP's own worked example (₹2,06,543 over
  five victims) is reproduced to the paisa.
- Injection guard on a blind holdout by a separate author: precision 0.9661, recall 0.75.
- Measured vs designed: everything on slides 5 to 8 marked TBD is designed and tested offline, and measured only on
  the MI300X during the event.

## 10. Market and revenue

- **Who:** every regulated entity on CFCFRMS: 1,586 entities, including 40 foreign banks, to 30.06.2026.
- **Volume:** more than ₹11,158 Cr saved in 32.80 lakh complaints to 30.06.2026; the Suspect Registry has shared
  32.08 lakh Layer 1 mule accounts. Each complaint that lands at a bank is a trace, a hold plan, an STR decision and a
  restoration packet.
- **Model:** an on-prem appliance licence per regulated entity (software plus the model weights and LoRA), with
  support; the bank supplies the GPU or buys the box. Unit cost per case = $1.99 per GPU-hour ÷ cases per hour
  (**TBD** from slide 7).
- **Price:** **[set after 2 to 3 bank validation calls; no number before then]**.

## 11. Competitors and partners

| Player | What it does | Wapsi's relationship |
|---|---|---|
| MuleHunter.AI (RBI Innovation Hub) | flags likely mule accounts from transaction patterns | **complementary**: Wapsi reads a MuleHunter-style score as one input feature and acts after the complaint |
| I4C Suspect Registry | shares known mule identifiers across entities | upstream signal; Wapsi turns a hit into a traced, amount-specific hold |
| CFCFRMS (I4C) | the government platform banks receive hold requests from | Wapsi is the bank-side copilot for it (a **simulated CFCFRMS interface** today) |
| Transaction-monitoring and AML suites | alerts on suspicious activity | they alert; Wapsi does the SOP work after a complaint: trace, pro-rata split, holds, verified drafts |
| General LLM copilots on external APIs | drafting and summaries | customer data leaves the bank; nothing checks their numbers |

## 12. The ask, and what we built here

- **Ask:** AMD Developer Cloud time for a pilot with one bank's nodal cell; introductions to BFSI partners running
  Instinct GPUs; feedback on packaging it as an on-prem appliance.
- **Built during 12 to 18 Oct, on the MI300X:** GNN training, the Gemma 4 LoRA fine-tune and merge, vLLM-ROCm
  serving of both models next to the GNN, and every benchmark on slides 5, 7 and 8.
- **Prepared before the event and disclosed:** the portable core, the compose stack, benchmark harness and runbook,
  tested offline with fakes. Tag `amd-baseline` at kickoff; details in `docs/originality.md`.
