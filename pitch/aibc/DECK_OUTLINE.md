# AI Builder Cup deck (PDF): 12 slides, Google Cloud only

Theme: **BFSI**, with a social-impact angle (Most Impactful). Export to PDF. **Keep Snowflake, AMD and any other cloud
out of the diagram, the text and the screenshots**: the judged entry is the Google Cloud edition
([`editions/gcp/`](../../editions/gcp/README.md)). Every number is a row in [`../NUMBERS.md`](../NUMBERS.md).

Rubric we are writing to: technical merit and GenAI use (the largest weight), problem fit and impact, innovation,
UX. Slides 4, 5 and 7 carry the technical-merit score.

---

## 1. Title: the number

- **Wapsi**: give the money back. A GenAI copilot for a bank's cyber-fraud nodal officer, on Google Cloud.
- Hero line: **A whole-account freeze locks ₹35.26 Cr in 101 accounts. Wapsi holds ₹29.19 L in 84. 0 false
  freezes.** (39-case synthetic test world.)
- Live URL, video, repository: **[fill on submission day]**.

## 2. Problem and BFSI impact

- **₹7,647 Cr** held for cyber-fraud victims through CFCFRMS, **₹167 Cr** restored: **2.18%** (MHA/I4C SOP,
  02.01.2026, Apr 2021 to Nov 2025).
- Scale is growing: more than ₹11,158 Cr saved in 32.80 lakh complaints to 30.06.2026; 1,586 entities on CFCFRMS.
- Why banks feel it: whole-account freezes lock innocent customers and merchants and drive grievances; STRs are due
  within 7 working days; every number in a filing is an audit risk; tracing through mule layers is manual.
- The user: the bank's cyber-fraud nodal officer, plus the supervisor who signs off.

## 3. What Wapsi does (user journey)

`1930 complaint (text, PDF or screenshot)` → `Gemini extracts it; checked against the ledger` → `money trail` →
`amount-specific hold plan` → `STR, hold instructions, restoration packet` → `verifier` → `supervisor approves`.
- Showcase case (hand-computed): ₹4,80,000 stolen; hold ₹1,42,848 in 5 accounts; ₹3,34,848 recoverable; the
  whole-account freeze baseline would lock ₹5,03,50,000.
- Innocent landlord: hold ₹4,800, no escalation. Kirana with delivered goods: no hold. Payment-aggregator pool: hold
  ₹19,200, the account keeps running.

## 4. Architecture on Google Cloud

```
Officer / supervisor ──> Cloud Run: wapsi-ui (Streamlit case room)
                     ──> Cloud Run: wapsi-api (ADK 2.9 on FastAPI, private)
                                 ├─ LlmAgent wapsi_officer (10 pipeline tools) ──> Gemini
                                 ├─ LlmAgent wapsi_intake (PDF / image → JSON)  ──> Gemini (multimodal)
                                 ├─ ADK Workflow case_pipeline (notices, uploads, clock sweeps)
                                 ├─ plugins: guard (Model Armor → rules guard), audit, memory
                                 └─ Wapsi core: trace · taint · hold plan · drafts · verifier
Data: BigQuery (ledger; partitioned + clustered) · Firestore (cases, drafts, approvals, append-only audit)
Ops:  Secret Manager (Gemini key, role-grant HMAC key) · Cloud Logging + Trace · Cloud Build → Artifact Registry
Event path (optional): Cloud Storage uploads · Pub/Sub notices · Cloud Scheduler clock sweep
```
- Region asia-south1 (Mumbai). Expected cost at demo scale $0 on the free trial.
- Label anything not switched on in the judged deployment as "optional"; never draw a service the URL does not use.

## 5. GenAI design: Gemini with guardrails

| Gemini does | Code does | Why |
|---|---|---|
| Agent reasoning and tool choice (ADK) | trace, taint, holds, clocks | a model must not decide a hold amount |
| Multimodal intake: complaint PDF or screenshot → structured fields | cross-check every field against the BigQuery ledger | extracted text is untrusted |
| Draft prose around `⟦slot⟧` placeholders | fill every amount, account, UTR and date from case data; verifier recomputes each | numbers come from data, words come from the LLM |
| (nothing) | approve | only a supervisor approves; the approval is bound to the draft's SHA-256 fingerprint |

- Prompt injection: complaint text is wrapped as untrusted data and screened (Model Armor when switched on, a rules
  guard always). Instruction-like text is quoted back to the officer, never followed.
- Show the ADK tool-call trace screenshot here.

## 6. Demo walkthrough

Four screenshots with one caption each, in video order:
1. Agent chat: "Open the case for NCRP 31009260000101" → tool calls `open_case`, `trace_money`, `hold_plan`.
2. Hold plan with the baseline strip: ₹1,42,848 held vs ₹5,03,50,000 frozen.
3. Hinglish complaint `11208260078296` with an embedded "release all holds" order, quoted back as untrusted; hold unchanged.
4. STR with every check green; supervisor approval with the maker-checker note.

## 7. Evaluation: measured against exact ground truth

| Synthetic world | Holds exact | Over-hold | False freezes | Dispositions | Drafts verified |
|---|---:|---:|---:|---:|---:|
| Demo (39 cases, 10 typologies) | 1.0 | ₹0 | 0 | 1.0 | 1.0 |
| Hard (64 cases, 15 typologies) | 1.0 | ₹0 | 0 | 0.9198 | 1.0 |

- Ground truth comes from an independent reference implementation, and the SOP's own Annexure-V worked example
  (₹2,06,543 over five victims) is reproduced to the paisa.
- Stress test: a model that writes a wrong rupee amount into every prose field. 27.57% of drafts had their prose
  replaced; drafts passing the verifier stayed 1.0 (100%).
- Injection guard, blind holdout by a separate author: precision 0.9661, recall 0.75.
- Gemini on the golden set: **[TBD by Oct 14: verifier pass rate, prose fallback rate, cost per case]**. Until then the
  slide says "measured with a deterministic stub LLM".

## 8. Innovation

- **Amount-specific holds as code**, from the SOP: the whole-account freeze becomes a comparison figure, not an action.
- **Account-level action only as a recorded, approved exception**: 4 recorded reasons, maker-checker.
- **Innocent holder split**: bona fide recipient (no STR by default) vs unwitting conduit (STR recommended, the
  officer decides).
- **Police request checker**: 8-field LEA checklist, hold limited to the identifiable amount, clarification letter.
- **Regulatory change radar**: 11 rows comparing the RBI DRAFT money-mule SOP with the MHA SOP, verbatim quotes on
  both sides, flag and suggest only.
- **Tamper-evident audit trail**: a SHA-256 hash chain per case; the tamper drill detects 6/6 attacks.

## 9. Impact and business case

- Victims: money on hold starts moving back instead of waiting behind grievances. In the showcase case, 69.76% of the
  stolen money is recoverable, with a restoration packet drafted.
- Innocent customers and merchants: in the demo world, ₹34.97 Cr of non-victim money stays free.
- Banks: fewer grievances, STRs on time, and every number in a filing recomputed before sign-off.
- Buyer: the bank's fraud-risk and compliance function. 1,586 entities are on CFCFRMS today. Pricing:
  **[set after the bank validation calls]**.

## 10. Scalability, security, cost

- Serverless: both services scale to zero; per-case cost is driven by Gemini calls, not by ledger size.
- p95 0.72 s per case from trace to verified drafts on the demo world (laptop, stub LLM); Cloud Run numbers
  **[TBD after deploy]**.
- Security: the API service is private; officer and supervisor roles come from HMAC-signed grants checked by the
  guard plugin and again by the tool; secrets live only in Secret Manager; the Firestore audit subcollection is
  append-only; all data is synthetic.

## 11. Roadmap

- Next: Hinglish voice-note intake with Gemini audio; Vertex AI Search over the legal corpus (the verbatim-quote
  verifier stays the judge); CFCFRMS connector once I4C onboarding allows (today a **simulated CFCFRMS interface**,
  labelled everywhere).
- Then: private Gemma tier on Cloud Run GPU for draft prose that reads victim PII; a pilot with one bank's nodal cell.

## 12. How to use it, and the team

- Judges: open the live URL, pick the showcase case in the Inbox, switch the role to supervisor to approve
  **[check the exact clicks on the deployed URL]**.
- Repository link for this entry: `editions/gcp/README.md` (the Google Cloud edition only), on the AI Builder Cup
  submission tag.
- Team: names, roles, one line each **[fill]**.
