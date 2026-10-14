# Editions and run modes

Wapsi is entered in one hackathon: the lablab.ai **AMD Developer Hackathon ACT III** (12 to 18 October 2026),
Track 3 "Reinvent Commerce". **The AMD edition is the entry.** It runs in three modes that share one codebase: an
AMD-hosted model endpoint, a ROCm notebook and a single MI300X machine.

The repository also holds a second platform layer, `editions/gcp`. It contains the officer's agent, built with
Google's open-source Agent Development Kit (ADK), and a set of **optional managed-cloud adapters** for Google
Cloud. The agent is part of the entry, with Gemma 4 on AMD as its model. The managed-cloud adapters are not: they
are kept for a bank that wants a managed cloud, and nothing in the entry depends on them.

Every edition is a thin layer under `editions/<platform>/` on top of the same portable core in `src/wapsi`. The
core decides every number, disposition, draft and citation. A platform layer decides where the data lives, which
model writes the prose, how officers reach the system and how it is served.

Why one core: [ADR 0005](adr/0005-one-core-two-editions.md). How the adapters plug in:
[architecture.md](architecture.md#how-editions-plug-in). What was built when: [originality.md](originality.md).

## Summary

Status as of 2026-10-14, checked against the repository.

| | AMD (the entry) | Optional managed-cloud adapters |
|---|---|---|
| Purpose | lablab.ai AMD Developer Hackathon ACT III, Track 3 | not a hackathon entry; adapters for a managed cloud |
| Folder | [`editions/amd`](../editions/amd/README.md), plus the ADK agent app in `editions/gcp/app` | [`editions/gcp`](../editions/gcp/README.md) (`deploy/`, and the adapters in `src/wapsi/adapters`) |
| Prose and extraction model | Gemma 4 31B served on AMD GPUs, through `OpenAICompatLLM` | `GeminiLLM`, or Gemma through `OpenAICompatLLM` |
| Agent | Google ADK agent with Gemma 4 on AMD as its model (LiteLLM connector) | the same ADK agent with a hosted model |
| Speech to text | Whisper (open weights), local by default | the hosted model reads the audio |
| Mule scorer | heuristic by default; GraphSAGE GNN in PyTorch on ROCm | heuristic |
| Ledger | `ParquetLedger` | `BigQueryLedger`, or `ParquetLedger` |
| Case store and audit chain | `FileCaseStore`: JSON files with atomic writes, chained on write | `FirestoreCaseStore`: one transaction per audit event, anchor in `audit_anchors/{case_id}` |
| Guard | `RulesGuard`, plus an LLM judge when a second model is configured | `RulesGuard`, plus `ModelArmorGuard` when a template is configured |
| Status | Core and AMD layer code existed before the event (tag `amd-baseline`). Endpoint mode is wired; AMD measurements are published only from committed results files. No MI300X run is reported. | Built and unit-tested offline with fakes. Deploy scripts are dry-run in tests with a fake `gcloud`. Nothing is deployed. |

## AMD: what runs where

The hackathon asks that a meaningful part of the workload runs on AMD and that the AMD component is part of the
working product. In Wapsi it is the language side of every case, and the learned mule scorer.

| Workload | Model | Where it runs | Decides a number? |
|---|---|---|---|
| The three prose fields of a case's drafts | Gemma 4 31B | AMD GPUs, through the model endpoint | no: placeholders only, the verifier recomputes |
| Field extraction and translation of complaints and voice-note transcripts | Gemma 4 31B | the same endpoint | no: every extracted value is a claim, reconciled with the ledger |
| The officer's agent and its tool calls | Gemma 4 31B through Google ADK | the same endpoint | no: figures come from tools |
| Second opinion on untrusted text (judge) | a small open model, configurable | the same endpoint, or a second model on the same GPU | no: it can add a flag, never clear one |
| Speech to text | Whisper, open weights | local by default; any GPU is faster | no |
| Mule scoring | GraphSAGE GNN, pure PyTorch | ROCm on an AMD GPU (notebook or droplet) | it scores a role; amounts stay arithmetic |
| Tracing, per-victim split, hold plan, verifier | no model | plain Python, any CPU | yes, all of them |

### Three modes, one codebase

The model endpoint is a setting. Nothing in the core knows which mode it is in.

| | Endpoint mode | Notebook mode | Droplet mode |
|---|---|---|---|
| What it is | an OpenAI-compatible API that serves open models on AMD GPUs (the AMD Token Factory that the event gives every team) | a ROCm JupyterLab on an AMD GPU; Docker may not be available | one AMD Instinct MI300X with Docker Compose |
| Used for | everything on the language side: prose, extraction, the agent | GNN training on an AMD GPU, benchmarks | serving both models, the GNN and the app on one machine, so no request leaves it |
| You need | a base URL, a key and a model name | a notebook instance from the AMD portal | a droplet from the AMD Developer Cloud |
| Role in the entry | **primary**: the demo and the measurements run here | secondary | secondary |
| Status (2026-10-14) | wired and exercised live with synthetic data; published figures are open slots until results files are committed | code prepared and CPU-tested; no AMD GPU run reported | stack, runbook and benchmark harness prepared and tested offline; not run on an MI300X |

**Endpoint mode settings.** All of them come from the environment; none is ever committed.

| Variable | Meaning |
|---|---|
| `WAPSI_LLM_BASE_URL` | OpenAI-style base URL ending in `/v1` |
| `WAPSI_LLM_API_KEY` | bearer key, read by `OpenAICompatLLM`; never printed or logged |
| `WAPSI_LLM_MODEL` | model id as the endpoint lists it, for example `Gemma-4-31B-IT` |
| `WAPSI_LLM_JSON_MODE` | `json_object` for a shared endpoint; `json_schema` only where strict schemas are served (`editions/amd/app/settings.py`) |
| `WAPSI_VERIFIER_BASE_URL`, `WAPSI_VERIFIER_API_KEY` | switch on the second-opinion judge model |
| `WAPSI_GNN_URL` | switch on the GNN scoring service; empty means the heuristic scorer |

**What we observed on the shared endpoint on 2026-10-14**, and what Wapsi does about each point:

| Observation | Consequence in Wapsi |
|---|---|
| OpenAI-compatible chat completions; the model list changes over time | the model is a setting, and the smoke run reads the model list first |
| `json_object` output works; strict `json_schema` output is not accepted | `json_object` mode, then Wapsi validates the answer against its own schema and repairs or rejects it |
| `seed` and `stop` are not honoured | temperature 0, and the live smoke run caches good responses on disk; no claim of bit-for-bit repeatability with a live model |
| No audio transcription and no audio input in chat; no embeddings | speech to text runs locally with Whisper; legal search is the repository's own pure-Python BM25, which needs no embeddings |
| Per-account request and concurrency limits, a daily usage cap, HTTP 429 with `Retry-After` | live runs are sequential and spaced; the client retries 429 and 5xx with backoff and honours `Retry-After` |
| A trial service with no SLA, and a rule not to send sensitive data | only synthetic data is ever sent; a case never blocks on the model, because a failed or rejected answer is replaced by a fixed paragraph |
| Reply quality can degrade for a while | the smoke run starts with a health probe and refuses to record a result from a degraded endpoint (`pitch/amd/evidence/token_factory_smoke.py`) |

**Notebook mode.** The GNN is pure PyTorch with standard operators only, so the same code runs on CPU and on ROCm
(`--device auto`). Training, evaluation and the checkpoint format are described in `editions/amd/gnn/README.md`.
A notebook has no Compose stack, so the app keeps using endpoint mode for the language side and loads the trained
checkpoint for scoring.

**Droplet mode.** `editions/amd/docker-compose.yml` serves the drafting model and, with the `verifier` profile, a
second smaller model on the same GPU with vLLM on ROCm, next to the GNN service and the app; everything binds to
`127.0.0.1`. The memory plan for one MI300X (192 GB) is arithmetic over the configuration, not a measurement:
about 55% of GPU memory for Gemma 4 31B in BF16 and its KV cache, about 15% for the verifier model, the rest for
the GNN and headroom. Steps: [`RUNBOOK.md`](../editions/amd/RUNBOOK.md). A LoRA fine-tune of the open model for
the prose fields is prepared in `editions/amd/lora/` and is optional.

### Two features built during the event

Both are being built in their own branches during the event window. This page describes them at the level that
is settled; **exact commands: see the README section after integration.**

**Open-model voice intake.** A victim's Hinglish, Hindi or English voice note is transcribed by a Whisper-class
open speech model, and Gemma 4 on AMD extracts the complaint fields and the English translation from the
transcript in `json_object` mode. Everything after that is the existing shared code, unchanged: schema
validation, phone masking in code, guard screening, and the reconciliation of every claimed amount, UTR, date
and account digit with the ledger. Only a ledger match opens a case. The speech model is local by default
because the shared endpoint serves no audio; an OpenAI-compatible transcription endpoint is an option, and a
chat-audio adapter is probed at run time so that it switches on if the endpoint starts accepting audio. Details:
[Voice intake](voice-intake.md).

**The ADK agent on Gemma 4 served on AMD.** The officer's agent keeps its ten pipeline tools, its intake
sub-agent and its guard and trace plugins. Its model becomes Gemma 4 on the AMD endpoint, reached through ADK's
LiteLLM connector as an OpenAI-compatible provider. Gemma 4 calls the tools natively; a small JSON-protocol
fallback, in which the model names the tool in one JSON object, is kept as a safety net. Every run is recorded by
the existing agent trace (redacted arguments, result summaries taken from the tools' own results, guard and
verifier verdicts, reply grounding) and sealed into each case's audit chain
([User guide](user-guide.md#agent-trace-tab), [Trust and safety](trust-and-safety.md#agent-traces)). Before the
event the agent already had a LiteLLM path to an OpenAI-compatible server (`build_model` in
`editions/gcp/app/agent.py`) and ran offline with a scripted model.

### Status, honestly

- The portable core, the officer app, the ADK agent app and the prepared AMD layer (`docker-compose.yml`,
  `serving/`, `app/`, `bench/`, `gnn/`, `lora/`) existed before the event and are under the git tag
  `amd-baseline` (commit `1d500f6`). [originality.md](originality.md) lists what was added during the event.
- A figure with a real model in the loop is published only from a results file committed to the repository, and
  it says where it ran: AMD Token Factory, an AMD GPU notebook or an MI300X droplet. A result from the shared
  endpoint is never presented as a benchmark of a dedicated MI300X. Open figures are listed as slots at the end
  of `pitch/NUMBERS.md`.
- The only GNN results committed at the baseline are labelled CPU-development checks
  (`gnn/results/cpu-dev-2026-09-23.json`). `editions/amd/bench/fixtures/` holds hand-written samples of the
  `amd-smi` output shape for parser tests; they are not measurements.
- The app's extra dependencies are the `amd-app` extra (FastAPI, uvicorn); training uses the `amd` extra.

## The ADK agent app and the optional managed-cloud adapters

**Folder:** [`editions/gcp`](../editions/gcp/README.md). Diagram and service-by-service table:
[`editions/gcp/architecture.md`](../editions/gcp/architecture.md).

**The agent (used by the entry)**

- Two ADK apps: `wapsi_officer` (an `LlmAgent` with ten pipeline tools, such as `trace_money`, `hold_plan`,
  `draft_documents` and `approve_draft`, and an intake sub-agent) and `case_pipeline` (a deterministic ADK
  workflow for hold notices, uploads and clock sweeps).
- **Agent trace.** `ToolTracePlugin` is the app's first plugin, so it records every model turn and tool call
  (redacted arguments, a result summary taken from the tool's own result, latency, status) before any other
  plugin can answer a callback; `WapsiGuardPlugin` reports its verdicts into the same recorder. When a run ends,
  its trace is appended to the audit log of every case it touched (an `agent_trace` event, so the hash chain
  covers it). The UI's Case Room shows the runs in its **Agent trace** tab.
- **Voice-note tool.** The `wapsi_intake` sub-agent has the `intake_voice_note` tool. It returns the transcript
  sanitised inside untrusted-text markers, marks every claim as untrusted, and names only the ledger-matched NCRP
  ids as the next step for `open_case`, whose amounts come from the ledger.
- Role grants are signed with an HMAC key; a supervisor role is needed to approve.
- A demo mode with no network access: showcase ledger, in-memory case store, `FakeLLM`, `RulesGuard` and a
  scripted ADK model. In demo mode the UI runs the offline agent script once at start-up, so the showcase cases'
  audit chains hold a real (scripted-model) agent run.

**The managed-cloud adapters (optional, not part of the entry)**

- Adapters that live in `src/wapsi/adapters` because they are general-purpose: `BigQueryLedger` (parameterised
  SQL, partitioned and clustered tables, a bytes-billed cap), `FirestoreCaseStore`, `ModelArmorGuard` and
  `GeminiLLM`.
- **Audit chain sealed in Firestore.** `FirestoreCaseStore.append_audit` runs one Firestore transaction: it reads
  the case's anchor (`audit_anchors/{case_id}`, outside the log), seals the event as the next link, creates
  `cases/{case_id}/audit/{seq:010d}` and advances the anchor in the same commit. Concurrent writers conflict on
  the anchor and Firestore re-runs one of them, so the chain stays gap-free. `verify_audit(case_id)` checks the
  stored log against the anchor.
- Two Cloud Run services from one image (`wapsi-api`, `wapsi-ui`), deploy scripts, a Pub/Sub trigger route and
  an IAP-scoped `GET /wapsi/traces`.

**Status, honestly.** Nothing has been deployed. The code is built and unit-tested locally with fakes
(`tests/gcp`: a SQLite-backed BigQuery fake, an in-memory Firestore, fake Model Armor and a fake hosted model).
The deploy scripts are dry-run in tests with a fake `gcloud` and have not been run against a cloud project. The
folder's README has the latest state.

## What is shared and what is not

```mermaid
flowchart TB
    subgraph SHARED["Shared by every platform layer"]
        CORE["src/wapsi<br/>trace, taint, hold plan, classify,<br/>drafts, verifier, clocks, search"]
        UI["ui/app.py<br/>Streamlit officer app"]
        GOLD["data/golden + evals/<br/>golden set, harness, guard corpus"]
        DOCS["data/docs<br/>citations.json, facts.json"]
        SK[".claude/skills<br/>6 skills, mirrored to .agents/skills"]
    end
    AM["editions/amd (the entry)<br/>Gemma 4 on AMD, GNN on ROCm,<br/>endpoint / notebook / droplet"] --> CORE
    AG["editions/gcp/app<br/>Google ADK agent"] --> CORE
    AG --> AM
    GC["optional managed-cloud adapters<br/>BigQuery, Firestore, Model Armor"] --> CORE
```

| Shared (one copy, same behaviour everywhere) | Platform-specific |
|---|---|
| Money-trail tracing and exact per-victim taint (`wapsi.graph`) | Where the ledger lives and how a case's slice of it is loaded |
| Amount-specific hold plan and every SOP rule (`wapsi.graph.holdplan`) | Which model writes the three prose fields, and where it is served |
| Account classification and reasons (`wapsi.classify`) | Optional extra scorers (a GNN) |
| Eight draft kinds, the LEA clarification letter and the placeholder-only prose contract (`wapsi.draft`) | Where case bundles, approvals and audit events are stored |
| The verifier, the LEA request checklist, the account-level exception and the approval fingerprint (`wapsi.verify`, `wapsi.pipeline.run`) | How officers and supervisors sign in and reach the app |
| SOP clocks, the bank working-day calendar and the regulatory radar (`wapsi.domain`); the hash-chained audit log (`wapsi.audit`) | Extra guards on top of `RulesGuard` (an LLM judge, Model Armor) |
| Voice-note intake after the transcript: the response schema, phone masking, guard screening and ledger reconciliation (`wapsi.intake`) | Which model turns audio into text and which model extracts the fields |
| The agent-trace model, recorder, redaction and the `agent_trace` audit event (`wapsi.agenttrace`) | Which agent loop drives the recorder (ADK plugin callbacks) and which model the agent uses |
| Citation ids and verbatim quotes (`wapsi.citations`, `data/docs/`) | Deployment, cost controls and platform benchmarks |
| The Streamlit officer app (`ui/`), the golden set, the evaluation harness, the showcase fixtures | Evidence files for the judges |

The showcase numbers are the parity test for every layer: ₹1,42,848.00 held across 5 accounts and
₹3,34,848.00 recoverable for the victim in showcase case 1, against ₹5,03,50,000.00 that a whole-account freeze
would immobilise (`src/wapsi/fixtures/showcase.py`).

## How the layers stay consistent

| Mechanism | Where |
|---|---|
| One implementation of every rule, imported by all layers | `src/wapsi` |
| Hand-computed showcase numbers every adapter must reproduce | `src/wapsi/fixtures/showcase.py`, `tests/test_contracts.py` |
| Same golden set and harness whatever the model | `data/golden/`, `evals/harness.py` (`--llm`) |
| Adapter parity tests | `tests/amd` (offline, with fakes); `tests/gcp` (BigQuery SQL against `InMemoryLedger` on SQLite) |
| Same skills in every coding agent | `.claude/skills/`, mirrored to `.agents/skills/` by `scripts/sync_skills.py` |
| Same conventions for every contributor and agent | `AGENTS.md` |

## Related documents

- [architecture.md](architecture.md): ports, adapters and the pipeline
- [ADR 0005](adr/0005-one-core-two-editions.md): why one core and thin platform layers
- [originality.md](originality.md): the baseline tag and what was built during the event
- `pitch/TRACK_FIT.md` in the repository: the Track 3 brief against the evidence
