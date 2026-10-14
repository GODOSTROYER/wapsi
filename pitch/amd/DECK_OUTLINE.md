# AMD Developer Hackathon ACT III deck: 12 slides

**Challenge: Track 3, Reinvent Commerce** ("Help a business understand its customers and take better action").
lablab scores Application of Technology, Presentation, Business Value and Originality. The deck follows the
track's own list of what a good project shows: a real customer or merchant problem (slides 2 and 3), a useful
commercial action (4 and 5), how the prediction is calculated (6), a measurable effect (7), and the AMD workload
that is part of the working product (8 and 9).

Every number is a row in [`../NUMBERS.md`](../NUMBERS.md). A figure not yet measured on AMD is written
`TBD(slot_id)` and listed there as an open slot; it is filled only from a results file in this repository, with
where it ran stated next to it (AMD Token Factory, an AMD GPU notebook or an MI300X droplet). All case data is
synthetic.

---

## 1. Title

- **Wapsi: hold the fraud, not the shop.**
- A copilot for a bank's cyber-fraud desk. It turns transaction data into an amount-specific hold and a plan to
  return the money, so honest merchants and customers keep trading.
- Hero line: a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84, with 0 false
  freezes (39-case synthetic test world).
- Retention line under it: honest merchants and customers: ₹82.99 L would be frozen; Wapsi holds ₹2.50 L, the
  victim money only. (Most of the ₹35.26 Cr sits in three nodal / pool accounts, which SOP 9.1(iii) already says
  must not be frozen whole; this line is the part a bank's own customers feel.)
- Footer: Track 3, Reinvent Commerce · Gemma 4 on AMD GPUs · Google ADK · solo entry.

## 2. The merchant's problem, and the customer's

- A kirana shop sold ₹1,200 of groceries. The buyer was a mule spending stolen money. The next morning the shop's
  whole account is frozen: kirana shop balance ₹51,200, the money it pays suppliers with.
- The same morning: a landlord who received ₹5,000 of rent finds landlord balance ₹1,55,000 frozen. A
  payment-aggregator pool that settles other merchants: ₹5,00,20,000 frozen.
- In this one case, 99.75% of what the freeze locks belongs to people who did nothing wrong: ₹5,02,26,200 of the
  frozen money sits in three honest accounts. The three mule accounts hold ₹1,23,800 between them.
- The government's own SOP says so: "Unwarranted orders for freezing accounts shall be discouraged, and
  accountability measures shall be established." (`SOP-5-IX`)
- Who pays: the merchant loses working capital, the customer loses the account, the bank loses both of them and
  answers the grievance.

## 3. The size of it

- ₹52,969 Cr reported, ₹7,647 Cr held, ₹167 Cr restored: 2.18% of the money held (April 2021 to November 2025,
  MHA/I4C SOP of 02.01.2026).
- To 30.06.2026: more than ₹11,158 Cr saved in 32.80 lakh complaints, across 1,586 entities on CFCFRMS.
- Money goes back only when someone proves whose money is in which account after it has been split and layered.
  Today a nodal officer does that by hand, with an STR due within 7 working days.

## 4. The commercial action: hold the amount, keep the account

Showcase case (hand-computed, synthetic): ₹4,80,000 stolen, moved through four layers of accounts.

| Account | Whole-account freeze | Wapsi | For the business |
|---|---|---|---|
| Kirana shop, goods delivered | ₹51,200 frozen | no hold, a CFCFRMS update only (SOP 9.2) | the shop keeps trading |
| Landlord, rent received | ₹1,55,000 frozen | L ₹4,800 held, no escalation (SOP 5(xii)) | the customer keeps the account |
| Payment-aggregator pool | ₹5,00,20,000 frozen | P ₹19,200 held (SOP 9.1(iii)) | ₹5,00,00,800 stays free; other merchants are settled |
| Three mule accounts | ₹1,23,800 frozen | A ₹43,200 · B ₹27,648 · D ₹48,000 | only the victim's money is held |
| The victim | nothing worked out | ₹3,34,848 recoverable, 69.76% of the stolen money | a restoration packet, drafted |

- Total: ₹5,03,50,000 in 6 accounts against ₹1,42,848 held in 5 accounts. The freeze locks 352× what Wapsi holds.
- ₹1,92,000 of the recoverable money is requested from the other bank; ₹96,000 is a forward hold on the first
  mule and is not counted as recoverable.

## 5. Data in, action out

```
transactions (UPI / IMPS, balances)  ─┐
complaints (NCRP text, voice notes)  ─┼─> screen ─> trace ─> split per victim ─> classify ─> HOLD PLAN
merchant records (invoice, GST,      ─┘     untrusted    the trail    (pro-rata,       accounts     per account
delivery status), device links              text                      SOP 5(xiii))
                                                                              │
        supervisor approves  <─  verifier recomputes every number  <─  drafts ┘
        (maker-checker, SHA-256)    and citation                     (Gemma 4 on AMD writes the words only)
```

- The action is concrete and per account: hold instruction, customer notice, EDD memo, request to the other bank,
  STR, CFCFRMS update, restoration pack.
- Speed: p95 0.35 s from complaint to hold plan; p95 0.72 s per case to verified drafts (laptop, stub LLM).
- A Hinglish voice note is data too: every amount and UTR a victim says is a claim, checked against the ledger.

## 6. How the prediction is calculated (no language model)

The track says: do not use a language model as the only forecasting method. Wapsi uses none for prediction.

- **What is predicted:** the role of each account on the trail: mule, innocent holder, merchant, pool account.
- **What is never predicted:** the amount. A hold is arithmetic, `min(victim money in the account, balance)`.
- **Scorer 1, the default:** a transparent logistic scorer with 27 hand-set terms over 33 behavioural features
  (account age, how fast money was passed on, distinct payers, cash-outs, shared devices, earlier reports). Hard
  rules run first: a merchant needs its own delivered invoice for every disputed credit.
- **Thresholds on screen:** score at or above 0.6 is a mule, at or below 0.35 an innocent holder, anything
  between goes to the officer as UNKNOWN. Every score breaks into per-feature contributions with plain-English
  reasons.
- **Scorer 2, the graph model:** a GraphSAGE GNN in pure PyTorch (78k parameters in the CPU development
  checkpoint), behind the same interface, with the heuristic as its fallback. Trained on an AMD Instinct MI300X
  (AMD AI Notebook, ROCm 7.14, 14-10-2026; 410k parameters): mule ROC-AUC on the hard world <!-- slot:gnn_amd_auc -->0.9202 (MI300X, AMD ROCm notebook)<!-- /slot -->,
  <!-- slot:gnn_amd_epoch -->0.06 s<!-- /slot --> per epoch.

| Hard world, 64 cases, built to fool the classifier | Heuristic | GNN (CPU development checkpoint) | GNN trained on the MI300X |
|---|---:|---:|---:|
| Mule ROC-AUC | 0.9411 | 0.9786 | 0.9202 |
| Mule recall | 0.966 | 0.7415 | 0.7415 |
| Dispositions right | 0.9198 | 0.9151 | 0.9198 |
| Innocent-holder recall | 0.6875 | 0.875 | 0.875 |
| Merchant precision / recall | 1.0 / 1.0 | same hard rules | same hard rules |

Every miss is a label, never money: holds stay exact, over-hold ₹0, 0 false freezes. The MI300X-trained GNN finds
more innocent holders than the heuristic, ranks mules less well, and sends more accounts to the officer as UNKNOWN,
so the transparent heuristic stays the default.

## 7. Measurable effect

| The track asks for an effect on | Wapsi's measure | Demo world: 39 cases, 10 fraud typologies |
|---|---|---|
| Revenue and working capital | money a freeze would lock that stays free | ₹34.97 Cr of non-victim money left free: ₹35.26 Cr frozen becomes ₹29.19 L held, 0.83% of the freeze |
| Retention | honest accounts that are never touched | 0 false freezes; 17 accounts a freeze would lock get no hold at all |
| Service quality | time from complaint to a hold plan | p95 0.35 s to the plan, p95 0.72 s to verified drafts (laptop, stub LLM); with Gemma 4 writing the prose on an AMD Instinct MI300X: p95 11.12 s per case to verified drafts |
| Accuracy of the action | holds against an independent reference | holds exact to the paisa 1.0 (100%), over-hold ₹0 |
| Restoration | victim money identified for return | showcase: ₹3,34,848 recoverable of ₹4,80,000 stolen |

- The harder test: 64 cases and 15 typologies. ₹39.55 Cr frozen becomes ₹70.73 L held, 1.79% of the freeze, with
  0 false freezes.
- Honest framing: these are plans on synthetic data, measured against an independent reference implementation.
  The national 2.18% is money actually restored; ours is money identified and held correctly, which is the step
  that is missing today.

## 8. What runs on AMD

The AMD component is in the working product: with an endpoint configured, the words in every draft, the fields
read from a complaint and every agent tool call come from Gemma 4 served on AMD GPUs. It has run on AMD two ways:
through AMD's Token Factory API, and served by vLLM on an AMD Instinct MI300X (AMD AI Notebook, ROCm 7.14).

| Workload | Model | Where it runs | Status (14-10-2026) |
|---|---|---|---|
| Draft prose (placeholder-only fields) | Gemma 4 31B | AMD Token Factory endpoint, or vLLM on an AMD Instinct MI300X | run live on both: the agent session and the showcase world on the Token Factory; the 39-case demo world end to end on the MI300X |
| Complaint and voice-note field extraction, translation | Gemma 4 31B | AMD Token Factory endpoint | run live on the six sample transcripts |
| The officer's agent and its tool calls | Gemma 4 31B through Google ADK | AMD Token Factory endpoint | run live; its trace is replayed in the Agent trace tab |
| Second opinion on untrusted text | a small open model | AMD Token Factory endpoint | wired, not yet measured |
| Speech to text for voice notes | Whisper large-v3-turbo, open weights | local, or an AMD GPU | timed on the MI300X: the 31-second note in 0.92 s |
| Mule scoring (optional second scorer) | GraphSAGE GNN, PyTorch on ROCm | AMD Instinct MI300X | trained on the MI300X: mule ROC-AUC 0.9202 on the hard world (heuristic 0.9411; the heuristic stays the default) |

- **Measured on an AMD Instinct MI300X (AMD AI Notebook, ROCm 7.14, vLLM), 14-10-2026,** with Gemma 4 31B served
  on the notebook: output <!-- slot:droplet_tokens_s -->55 / 181 / 474 tokens/s at concurrency 1 / 4 / 16<!-- /slot --> (699 at concurrency 64); a real drafting request takes a median of
  3.07 s one at a time; complete cases per hour through the whole pipeline <!-- slot:droplet_cases_hour -->1,590<!-- /slot --> at concurrency
  16, where 9 of 83 model-written prose fields were replaced by fixed text (0.1084) and every draft still passed the
  verifier; a case costs $0.0013 there ($1.25 per 1,000 cases) at the $1.99 per GPU-hour list price; device memory
  peak 97.0 GB of 191.7 GB while serving. In the first of the two notebook sessions, on a different
  MI300X, the 39-case demo world with Gemma 4 writing the prose: drafts passing the verifier 1.0, holds exact 1.0,
  numeric accuracy 1.0, citation validity 1.0, prose replaced by fixed text in 0.0332 of all 301 drafts, injection
  leaks 0.
- **Measured through the AMD Token Factory (Gemma-4-31B-IT, 14-10-2026; AMD-served, not an MI300X benchmark):** on
  the showcase world with Gemma 4 writing the prose, drafts passing the verifier 1.0 and prose replaced by fixed
  text on that run 0.0. In the smoke run one prose request takes a median of 8.29 s (one request at a time, from a
  laptop), and showcase case 1 uses 25,914 prompt and 636 completion tokens for its prose.
- **Whisper on the MI300X:** the six bundled notes at a steady-state real-time factor of 0.0312 (warm-up
  excluded). How many of the six reconcile with Whisper in the loop is still open: TBD(voice_asr_samples).
- **Three modes, one codebase.** Endpoint mode is the default: three settings (`WAPSI_LLM_BASE_URL`,
  `WAPSI_LLM_API_KEY`, `WAPSI_LLM_MODEL`). Notebook mode serves the model with vLLM, trains the GNN and runs the
  benchmarks in a ROCm JupyterLab (the MI300X figures above). Droplet mode serves everything on one MI300X with vLLM
  and docker compose.
- **Why this matters to a bank:** the model is open and the endpoint is the bank's choice. The same code moves
  from AMD's shared API to one AMD GPU in the bank's own rack by changing those three settings.
- **Droplet mode, designed and tested offline:** one MI300X, 192 GB HBM3. Gemma 4 31B in BF16 ≈ 62 GB of weights
  plus 43.6 GB left for its KV cache is 105.6 GB for the drafting model; 28.8 GB for the verifier; 57.6 GB left
  for the GNN and headroom. Not run on a droplet yet: memory per container next to the verifier and the GNN is
  TBD(droplet_vram).

## 9. Google technology: Gemma 4 and the Agent Development Kit

- **Gemma 4** is the primary language model on the product path; other models on the same AMD endpoint answer
  only as a labelled failover, and every draft names the model that wrote it. It never types a number: it writes
  sentences around placeholders, and the verifier recomputes every amount, account, UTR, date and citation.
- **Google ADK** (open source) runs the officer's agent: an ADK agent with 10 pipeline tools, pointed at Gemma 4
  on AMD through ADK's LiteLLM connector. Gemma calls the tools natively; a JSON-protocol fallback is the safety
  net.
- **Voice Intake:** a victim's Hinglish voice note becomes a transcript (Whisper on the open path; the six bundled
  samples come with their scripts), Gemma 4 reads the fields from the transcript, and every amount and UTR she said
  is checked against the ledger before a case opens.
- **Agent trace:** every tool call is recorded with its guard and verifier verdicts, redacted and sealed into the
  case's audit log. The tab replays a live run on Gemma 4 on AMD, labelled as a recording.
- **Live on AMD (AMD Token Factory, Gemma-4-31B-IT, 2026-10-14):** 5 tool calls behind "Open case
  31009260000101", Gemma 4 driving the same agent through native tool calls; that live run ended VERIFIED: 16 of 16
  drafts verified, with Gemma 4 on AMD writing the draft prose too. Reply grounding of the live run: 24 of 24 values
  in the agent's reply (17 amounts, 7 account numbers) came from a tool; an analyst's approval refused by
  maker-checker in the live session.
- **Voice-note fields, live on AMD:** Gemma 4 answered for all 6 sample transcripts in a median of 7.5 s per
  note, and 98.3% of the extracted values agree with the expected answers (the samples' scripts stood in for the
  recogniser; a smoke run on synthetic notes).

## 10. Why a bank can trust it

- **Numbers come from data, words come from the model.** A stub model that writes a wrong rupee amount into every
  prose field: 27.57% of drafts had their prose rejected and replaced by fixed text, and drafts passing the
  verifier still 1.0 (100%).
- **The same test with the real model** (Gemma 4 31B on an AMD Instinct MI300X, 14-10-2026): asked for the plan
  in free text, raw Gemma 4 wrote a wrong amount in 19 of 39 cases (0.4872), while 0 of the same run's 301 Wapsi
  drafts had a numeric error. In a separate A/B, 4 of 20 free-form STRs had a wrong amount against 0 of 17
  Wapsi-bound STRs; across every bound draft of that A/B, <!-- slot:ab_bound_wrong -->0 (of 155 drafts)<!-- /slot --> wrong amounts reach a draft.
- **The SOP's own arithmetic:** the SOP Annexure-V worked example, ₹2,06,543 over five victims, is reproduced to
  the paisa.
- **Untrusted text stays untrusted:** a complaint or voice note that says "release the hold" is quoted to the
  officer and never followed. The guard on a blind holdout: blind-holdout precision 0.9661, blind-holdout recall
  0.75; the structure, not the guard, is the defence.
- **Nothing moves without a supervisor:** maker-checker approval bound to the draft's fingerprint; a hash-chained
  audit log, 6/6 tampering attempts detected.

## 11. Business value

- **Buyer:** the fraud desk of every regulated entity on CFCFRMS: 1,586 entities on CFCFRMS, 40 foreign banks
  among them.
- **What it saves:** officer hours per complaint (trace, split, paperwork), wrongful-freeze grievances and court
  orders, and the merchants and customers a bank loses when it freezes the wrong account.
- **Model:** a licence per regulated entity, run on an endpoint the bank controls. Price: set after bank
  validation calls; no number before then.

| Player | What it does | Wapsi's relationship |
|---|---|---|
| MuleHunter.AI (RBI Innovation Hub) | flags likely mule accounts | complementary: its score can be one input feature |
| I4C Suspect Registry | shares known mule identifiers | upstream signal; Wapsi turns a hit into an amount-specific hold |
| CFCFRMS (I4C) | the platform hold requests arrive on | Wapsi is the bank-side copilot (a **simulated CFCFRMS interface** today) |
| Transaction-monitoring suites | alert on suspicious activity | they alert; Wapsi does the work after a complaint |
| General LLM copilots | drafting and summaries | nothing checks their numbers |

## 12. What was built this week, limits, and the ask

- **Built during 12 to 18 October:** the Track 3 product story, Gemma 4 on AMD as the drafting and extraction
  model, the ADK agent on Gemma 4, open-model voice intake, and every measurement in this deck that names AMD.
  The list with commits is in `docs/originality.md`.
- **Existing before the event and disclosed:** the portable core (tracing, hold plan, verifier, synthetic data,
  officer app), tagged `amd-baseline`.
- **Limits we state:** all banking data is synthetic; CFCFRMS is a simulated interface; open slots in this deck
  are not claims.
- **Ask:** a pilot with one bank's fraud desk on an AMD endpoint it controls, and introductions to payment
  aggregators whose pool accounts get frozen.
