# AI Builder Cup: submission form drafts

Paste-ready text for the Hack2Skill form. The form's own limits were not published when this was written, so each
block has a **target** limit that `uv run python pitch/check_numbers.py` enforces; if the form allows less, cut from
the end of the block. Google Cloud only: no AMD or other clouds in any field. Every number is a row in
[`../NUMBERS.md`](../NUMBERS.md).

## Project name

Wapsi

## Tagline (target 100 characters)

<!-- limit:100 -->
GenAI copilot that traces stolen money and holds only the victim's share, not the whole account.
<!-- /limit -->

Length: 96 / 100 characters

## Theme

BFSI (social impact: returning stolen money to cyber-fraud victims).

## Problem statement (target 600 characters)

<!-- limit:600 -->
Of ₹7,647 Cr held for cyber-fraud victims through CFCFRMS from April 2021 to November 2025, only ₹167 Cr, 2.18%, was restored (MHA/I4C SOP, 2026). One reason: banks freeze whole accounts. Innocent landlords, merchants and payment-aggregator pools get locked, grievances pile up and restoration stalls. A bank's nodal officer must trace money through layers of mule accounts by hand, decide what to hold, and file an STR within 7 working days, where one wrong number is an audit finding.
<!-- /limit -->

Length: 486 / 600 characters

## Solution (target 1,000 characters)

<!-- limit:1000 -->
Wapsi is a copilot for the nodal officer. A Gemini agent built with ADK on Cloud Run opens a 1930 complaint (text, PDF or screenshot), traces the money through mule layers from the BigQuery ledger, and splits every account's balance pro-rata between victims, as the SOP's own worked example does. It then holds only the victim's money in each account: an innocent landlord keeps his account, a merchant that delivered goods gets no hold, a payment-aggregator pool keeps running. Account-level action happens only as a recorded exception a supervisor approves. Gemini drafts the STR, hold instructions and restoration packet, but every amount, account, UTR and date is filled from data and recomputed by a verifier before a supervisor can approve. On a 39-case synthetic test world, a whole-account freeze locks ₹35.26 Cr in 101 accounts; Wapsi holds ₹29.19 L in 84, with 0 false freezes and every hold exact to the paisa.
<!-- /limit -->

Length: 921 / 1,000 characters

## How Google Cloud is used (target 700 characters)

<!-- limit:700 -->
Cloud Run runs two services from one image: the ADK 2.9 agent API on FastAPI and the Streamlit case room. Gemini powers agent reasoning, multimodal complaint intake and draft prose. BigQuery holds the ledger (partitioned and clustered, parameterized queries). Firestore stores cases, drafts, approvals and an append-only audit trail. Model Armor and a rules guard screen complaint text. Secret Manager holds the Gemini key and the role-signing key; Cloud Logging and Trace record every tool call. Cloud Build and Artifact Registry build the image. Optional event path: Cloud Storage uploads, Pub/Sub notices and a Cloud Scheduler clock sweep.
<!-- /limit -->

Length: 642 / 700 characters

## GenAI usage and guardrails (target 700 characters)

<!-- limit:700 -->
Gemini is the agent's brain and its reader, never its calculator. The ADK agent picks among 10 pipeline tools; every figure it states comes from a tool. Gemini extracts complaints from PDFs and screenshots, and each field is cross-checked against the ledger. Drafts are templates: Gemini writes prose around slot placeholders, prose with raw digits is rejected, and a verifier recomputes every number and citation. Complaint text is untrusted: instruction-like text is quoted back to the officer, never followed. Stress test: with a model that writes a wrong amount into every prose field, 27.57% of drafts had prose replaced and drafts passing the verifier stayed at 100%.
<!-- /limit -->

Length: 673 / 700 characters

## Impact (target 500 characters)

<!-- limit:500 -->
Victims: held money starts moving back; in our hand-computed case, ₹3,34,848 of ₹4,80,000 is recoverable, with the restoration packet drafted. Innocent customers and merchants: in the 39-case test world, ₹34.97 Cr of non-victim money stays free. Banks: fewer grievances, STRs on time, and every number checked before sign-off. 1,586 entities are already on CFCFRMS, so the workflow exists at every one of them.
<!-- /limit -->

Length: 410 / 500 characters

## What is innovative (target 500 characters)

<!-- limit:500 -->
The SOP as code: amount-specific holds by default, with the whole-account freeze kept as a comparison figure. Account-level holds only as a recorded, supervisor-approved exception. An innocent-holder split (bona fide recipient vs unwitting conduit). A police-request checker. A regulatory change radar that compares the RBI draft money-mule SOP with the MHA SOP, quote by quote. A tamper-evident, hash-chained audit trail.
<!-- /limit -->

Length: 422 / 500 characters

## Links (fill on submission day)

| Field | Value |
|---|---|
| Live URL | **[Cloud Run URL of wapsi-ui; warm with `judging-on`]** |
| Video | **[public YouTube link, 2:45]** |
| Repository | **[public GitHub URL pointing at `editions/gcp/`, on the AI Builder Cup tag]** |
| Deck | **[PDF exported from `DECK_OUTLINE.md`]** |

## Team

**[names, roles, one line each; every member registered and approved on the event dashboard]**
