# Wapsi (वापसी)

**Hold the fraud, not the shop.** Wapsi is a copilot for a bank's cyber-fraud desk. It traces stolen money through
mule accounts, holds only the victim's money in each account, keeps honest merchants and customers trading, and
hands a supervisor verified paperwork to approve. The words come from Gemma 4 served on AMD GPUs; every number
comes from data.

> **Judges: start here → [pitch/START_HERE_FOR_JUDGES.md](pitch/START_HERE_FOR_JUDGES.md).** The 60-second hook,
> every headline number with its source file, and a 2-minute tour you can run from this checkout.
> **Entry:** lablab.ai AMD Developer Hackathon ACT III, **Track 3 "Reinvent Commerce"**; the track brief against
> the evidence is in [pitch/TRACK_FIT.md](pitch/TRACK_FIT.md). Every number in the pitch is re-checked by
> `uv run python pitch/check_numbers.py` ([pitch/NUMBERS.md](pitch/NUMBERS.md)).

*Wapsi* means "return". It follows the MHA/I4C *SOP for NCRP-CFCFRMS, Custody, Restoration of money and Grievance
Redressal* (02.01.2026). All banking data in this repository is synthetic.

## An honest shop, one tainted payment, a frozen account

A kirana shop sells ₹1,200.00 of groceries. The buyer was a mule spending stolen money. The next morning the
shop's whole account, ₹51,200.00, is frozen, and it cannot pay its suppliers. The landlord who was paid
₹5,000.00 of rent by the same mule finds ₹1,55,000.00 frozen. The payment aggregator whose pool account settles
other merchants finds ₹5,00,20,000.00 frozen. None of them did anything wrong.

This is what a whole-account freeze does, and it does not even return the money. From April 2021 to November
2025, CFCFRMS kept **₹7,647 crore** of the **₹52,969 crore** reported from reaching cyber criminals, but only
**₹167 crore (2.18% of the money held)** had been restored to victims (SOP Para 2; `data/docs/facts.json`).
Money goes back only when someone can prove whose money is in which account after it has been split and layered.
The SOP itself says: "Unwarranted orders for freezing accounts shall be discouraged, and accountability measures
shall be established." (`SOP-5-IX`)

Wapsi does that proof from the bank's own transaction data and proposes a better action for every account:

| | Whole-account freeze baseline | Wapsi |
|---|---|---|
| Showcase case, ₹4,80,000.00 stolen | ₹5,03,50,000.00 locked in 6 accounts | **₹1,42,848.00 held in 5 accounts**, all of it the victim's money |
| The kirana shop, goods delivered | account frozen | **no hold**; the shop keeps trading (SOP 9.2) |
| The payment-aggregator pool | ₹5,00,20,000.00 frozen | ₹19,200.00 held; every other merchant is still settled (SOP 9.1(iii)) |
| Demo world, 39 synthetic cases | ₹35.26 crore locked in 101 accounts | **₹29.19 lakh held in 84**: 0.83% of it |
| Honest accounts wrongly frozen | every one the money touched | **0 false freezes, over-hold ₹0, every hold exact to the paisa** |

Sources: `src/wapsi/fixtures/showcase.py` (hand-computed) and `evals/reports/demo.json`. The freeze figure is a
comparison baseline, never a recommendation.

![Wapsi hold plan for the showcase case: a whole-account freeze would immobilise ₹5,03,50,000 across 6 accounts; Wapsi holds ₹1,42,848 across 5 accounts](docs/assets/screenshots/05-hold-plan-analyst.png)

## What runs on AMD

Every project in this hackathon must run a meaningful part of its workload on AMD. In Wapsi the AMD workload is
inside the working product: each sentence in a draft, each field read from a complaint and each step the officer's
agent takes comes from an open model served on AMD GPUs.

| Workload | Model | Where it runs |
|---|---|---|
| Prose of every draft (placeholder-only fields; numbers are filled from data) | **Gemma 4 31B** | AMD GPUs, through the AMD Token Factory endpoint (OpenAI-compatible) |
| Field extraction and translation of Hinglish complaints and voice notes | Gemma 4 31B | the same endpoint |
| The officer's agent and its tool calls | Gemma 4 31B, driven by Google ADK | the same endpoint |
| Second opinion on untrusted complaint text | a small open model (configurable) | the same endpoint, or a second model on the same GPU |
| Speech to text for voice notes | **Whisper** (open weights) | local by default (a CPU is enough for a short clip); any GPU is faster |
| Mule scoring on the money-trail graph | **GraphSAGE GNN**, pure PyTorch | ROCm on an AMD GPU notebook or an MI300X |

There are three ways to run it, and one codebase. The model endpoint is a setting, never a code change:

| Mode | What it is | Status on 2026-10-14 |
|---|---|---|
| **Endpoint** (default) | `WAPSI_LLM_BASE_URL`, `WAPSI_LLM_API_KEY`, `WAPSI_LLM_MODEL` point at AMD's Token Factory API. No GPU and no Docker needed on your side. | Wired through `OpenAICompatLLM` in `json_object` mode. Figures measured with Gemma 4 on AMD are published only from committed results files; until then they are open slots in [pitch/NUMBERS.md](pitch/NUMBERS.md). |
| **Notebook** | A ROCm JupyterLab on an AMD GPU: GNN training and the benchmarks, without Docker. | Code prepared and CPU-tested (`editions/amd/gnn/`). No AMD GPU run is reported yet. |
| **Droplet** | One AMD Instinct MI300X with Docker Compose: vLLM serves Gemma 4 and a verifier model next to the GNN and the app, so no request leaves the machine. | Stack, runbook and benchmark harness prepared and tested offline (`editions/amd/`). Not run on an MI300X yet. |

Why this matters to a bank: the model is open and the endpoint is the bank's choice, so the same build moves from
AMD's shared API to one AMD GPU in the bank's own rack by changing three settings. A result from the shared
endpoint is labelled "AMD Token Factory" and is never presented as a benchmark of a dedicated MI300X. Modes in
detail: [docs/editions.md](docs/editions.md).

## Google technology: Gemma 4 and the Agent Development Kit

- **Gemma 4** (Google's open model) is the only language model on the product path. It never types a number: it
  writes sentences around placeholders, and a separate verifier recomputes every amount, account number, UTR,
  date and citation. If its prose fails a check, a fixed paragraph replaces it.
- **Google ADK** (open source) runs the officer's agent: ten pipeline tools (trace the money, build the hold plan,
  draft, verify, approve as a supervisor), an intake sub-agent, and plugins that screen input and record every
  tool call. The agent reaches Gemma 4 on AMD through ADK's LiteLLM connector with native tool calls; a small
  JSON-protocol fallback is the safety net.
- **Agent trace.** The Case Room's **Agent trace** tab lists every tool call the agent made on a case, with
  redacted arguments, a result summary taken from the tool's own result, latency, and the guard, maker-checker and
  verifier verdicts around it. Each run is sealed into the case's hash-chained audit log.

Plan and evidence for the Google partner prize: [pitch/GOOGLE_PRIZE.md](pitch/GOOGLE_PRIZE.md).

## Quick start

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or later. No API key, no GPU and no generated data:

```bash
uv sync --extra ui --extra ml --extra docs   # core + officer app + XGBoost option + PDF reader
uv run streamlit run ui/app.py               # officer app, opens on the Inbox
```

The app runs the real pipeline on the showcase world with an offline model. Its **Voice Intake** page reads six
bundled synthetic voice notes offline, and the Case Room's **Agent trace** tab shows a recorded offline agent run
on the showcase case.

**With Gemma 4 on AMD (endpoint mode).** Set three variables in your shell (never in a committed file), then run
the live smoke check. It sends synthetic data only, makes about ten spaced requests, and refuses to write a
result if the endpoint is not answering in plain language:

```bash
export WAPSI_LLM_BASE_URL="<OpenAI-style base URL of the AMD Token Factory API, ending in /v1>"
export WAPSI_LLM_API_KEY="<your key>"
export WAPSI_LLM_MODEL="Gemma-4-31B-IT"
uv run python pitch/amd/evidence/token_factory_smoke.py --probe-only   # one request: is the model healthy now?
uv run python pitch/amd/evidence/token_factory_smoke.py                # showcase case 1 end to end on the endpoint
```

The shared endpoint does not accept strict `json_schema` output, so Wapsi uses `json_object` mode and validates
the answer itself (`WAPSI_LLM_JSON_MODE=json_object` for the app in `editions/amd/app/`). Commands for the officer
app on the endpoint, for Whisper voice intake and for the ADK agent on Gemma 4 are added to this section when
those event-week branches are integrated; notebook and droplet steps are in
[editions/amd/README.md](editions/amd/README.md) and [editions/amd/RUNBOOK.md](editions/amd/RUNBOOK.md).

To reproduce the offline results:

```bash
uv run pytest -q                                                              # full test suite
uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo   # synthetic world + golden set
uv run python -m evals.harness --world demo --report-name demo                # writes evals/reports/demo.json
uv run python -m wapsi.synth --size hard --seed 7 --out data/generated/hard   # hard world (15 typologies)
uv run python -m evals.harness --world hard --scorer heuristic --scorer xgb:models/xgb_hard202.json
uv run python -m wapsi.search.fetch                                           # downloads the public legal texts
uv run python -m wapsi.search.radar fetch                                     # radar documents (not indexed)
uv run python -m wapsi.search.build                                           # builds the local search index
uv run python -m wapsi.search.check_citations                                 # checks every quote and figure
uv run python -m wapsi.audit drill                                            # audit-chain tamper drill
```

`models/` is gitignored: train the XGBoost scorer first with `wapsi.classify.train` (see
[Evaluation](docs/evaluation.md#scorers-and-harness-options)). On Windows, set `PYTHONIOENCODING=utf-8` (or
`PYTHONUTF8=1`) if you pipe or redirect the output: the reports contain the ₹ sign. `uv sync --all-extras` also
installs the agent, cloud-adapter and AMD training dependencies (PyTorch among them), which is a much larger
download. To browse these docs as a site: `uv run --with mkdocs-material mkdocs serve`.

## What Wapsi does

- **Traces the trail.** It follows the victim's transfer through every mule layer at the home bank. Money that
  has moved to another bank becomes an outbound CFCFRMS request.
- **Splits mixed money exactly per victim** (pro-rata by default, SOP Para 5(xiii); FIFO and LIFO on request) and
  plans **amount-specific holds by default**: a hold is `min(victim money in the account, balance)`. Account-level
  action exists only as a **recorded, supervisor-approved exception**: an officer requests it with at least one
  reason from a closed list of four, a different supervisor approves it, and the hold plan never emits it on its
  own. The whole-account freeze figure Wapsi shows is a comparison baseline, never a recommendation.
- **Classifies each account** as a mule, a merchant (SOP 9.2), a nodal / pool / escrow account (SOP 9.1(iii)) or
  an innocent holder (SOP 5(xii)), split into a **bona fide recipient** (kept the money as rent, salary or payment)
  and an **unwitting conduit** (used as a pass-through by someone else). Innocent holders get an amount hold, an
  EDD memo and no escalation; there is **no automatic STR: the officer decides, and an STR is recommended where the
  account was used as a conduit**. Unclear accounts go to the officer.
- **Keeps merchants trading.** A merchant with a delivered invoice for every disputed credit gets no hold, only a
  CFCFRMS update. A pool or escrow account has the disputed amount held and keeps running.
- **Checks police / LEA hold requests** against an 8-field checklist, limits the hold to the amount identifiable
  on the trail, and drafts a template-bound clarification letter for whatever is missing.
- **Takes a victim's voice note.** A frightened victim's WhatsApp-style voice note in Hinglish, Hindi or English
  becomes a verbatim transcript, an English translation and structured complaint fields. The open path built for
  this entry is Whisper for the speech and Gemma 4 on AMD for the fields. Every number in the note is a **claim**:
  the phone is masked in code, spoken instructions are quoted and never followed, and each UTR, amount, date and
  account digit is reconciled with the ledger (matched, mismatch or not in ledger). Only a ledger match opens a
  case, and the case's amounts come from the ledger. Works offline on six bundled synthetic samples.
- **Shows exactly what the AI agent did.** Every tool call the ADK agent makes on a case is listed in order with
  its checks, plus a check that every amount and account number in the agent's reply came from a tool.
- **Drafts the paperwork from templates**: hold instruction, customer notice, EDD memo, outbound CFCFRMS request,
  STR (modelled on FINnet 2.0), CFCFRMS update, restoration pack (Process 1 or 2) and the officer's account-level
  hold request. The LLM writes prose only, through placeholders.
- **Verifies, then waits for a supervisor.** A separate verifier re-checks every amount, account number, UTR, date,
  citation and policy rule. Each approval is bound to the SHA-256 fingerprint of the exact draft, every event is
  sealed into a **hash-chained, append-only audit log**, and the SOP and STR clocks run on an Indian bank-holiday
  calendar.
- **Watches the rules.** A read-only **regulatory change radar** sets new and pending rules (the RBI draft SOP on
  money-mule accounts of 11 Sep 2026, a **DRAFT**; the RBI Fraud Risk Management Directions, 2026) against the
  SOP, with verbatim quotes, and shows what each would change for a case. It flags and suggests; every row waits
  for human review, and it never edits a plan, clock, draft or citation.

Wapsi is not connected to the CFCFRMS portal: it is a **simulated CFCFRMS interface**, and every CFCFRMS action it
prepares says so.

## How the prediction is calculated

Which account is a mule and which is an honest shop is a prediction. No language model makes it.

- **Hard rules first.** Victim, pool / escrow and merchant accounts are decided by rules; a merchant needs a
  delivered invoice of its own for every disputed credit.
- **A transparent scorer by default.** A logistic scorer with 27 hand-set terms over 33 behavioural features
  (account age, how fast money was passed on, distinct payers, cash-outs, shared devices, earlier reports). A
  score of at least 0.60 is a mule, at most 0.35 an innocent holder, and anything between goes to the officer as
  UNKNOWN. Every score is shown as per-feature contributions with plain-English reasons.
- **Optional learned scorers behind the same interface.** XGBoost, and a GraphSAGE graph neural network in pure
  PyTorch that runs on ROCm; the heuristic is their fallback.
- **The amount is never predicted.** A hold is arithmetic over the traced money. A wrong label changes the
  paperwork (an EDD memo instead of an STR ground, or the reverse); it never changes how much is held.

Details and accuracy on worlds built to fool it: [Classification](docs/algorithms/classification.md).

## Showcase: whole-account freeze vs Wapsi

On 10-09-2026 a caller posing as the victim's bank talks her into moving ₹4,80,000.00 by IMPS to a "safe
account". Within about 90 minutes the money passes through three mule accounts at the home bank. The first mule
pays ₹5,000.00 of rent to a landlord, withdraws ₹1,00,000.00 in two ATM withdrawals and sends ₹50,000.00 to
another bank; the second pays ₹1,200.00 to a kirana shop and ₹20,000.00 into a payment-aggregator pool account;
the third sends ₹2,00,000.00 to the other bank. Every number below is hand-computed in
`src/wapsi/fixtures/showcase.py`, and the pipeline reproduces it exactly.

| | Whole-account freeze baseline | Wapsi |
|---|---|---|
| Money immobilised at the home bank | ₹5,03,50,000.00 in 6 accounts | **₹1,42,848.00 in 5 accounts**, only the victim's money |
| Three mule accounts | whole balances frozen (₹1,23,800.00) | ₹43,200.00 + ₹27,648.00 + ₹48,000.00 held |
| Landlord who received ₹5,000.00 rent from a mule | whole account frozen (₹1,55,000.00) | ₹4,800.00 held, no escalation, EDD memo (bona fide recipient, SOP 5(xii)) |
| Kirana shop, goods delivered | whole account frozen (₹51,200.00) | no hold; CFCFRMS update only (SOP 9.2) |
| Payment-aggregator pool account | ₹5,00,20,000.00 frozen | ₹19,200.00 held; the account keeps running (SOP 9.1(iii)) |
| Money now at the other bank | outside this comparison | outbound CFCFRMS requests for ₹48,000.00 and ₹1,44,000.00 |
| Recoverable for the victim | not worked out | **₹3,34,848.00** (69.76% of the stolen money) |

The freeze locks 352× what Wapsi holds, and 99.75% of what it locks sits in the three honest accounts. Wapsi also
marks a forward hold of ₹96,000.00 on the first-layer mule account (SOP 9.1(i)); a forward hold is not money in
hand, so it is not counted as recoverable, and it gets its own 90-day review clock.

![Case Room: the showcase money trail, layer by layer, with the hold or request on each account](docs/assets/screenshots/02-case-room-trail-analyst.png)

## Results

Unless a row says otherwise, results come from synthetic data with the deterministic stub LLM.
[Evaluation](docs/evaluation.md) explains the method; every figure here is also in
[pitch/NUMBERS.md](pitch/NUMBERS.md). Figures measured with Gemma 4 on AMD are added to that file, with where they
ran, when their results files are committed.

| Check | Result | Source |
|---|---|---|
| The SOP's own worked example (Annexure-V, Illustration 2: five victims, ₹2,06,543 left) | all five pro-rata shares match the SOP to the paisa | `tests/test_sop_annexure_v.py` |
| Demo world: 39 cases, 10 fraud typologies, 3 with injected instructions | holds exact 1.0, over-hold ₹0, false freezes 0, dispositions 1.0, drafts passing the verifier 1.0, injection leaks 0 | `evals/reports/demo.json` |
| Demo world: money immobilised | Wapsi ₹29,19,245.69 in 84 accounts; whole-account freeze baseline ₹35,25,90,069.78 in 101 accounts (0.83%); 17 of those accounts get no hold at all | `evals/reports/demo.json` (`baseline`) |
| Demo world: time from complaint to hold plan | p95 0.35 s (laptop; no language model on that path); p95 0.72 s per case to verified drafts with the stub LLM | `evals/reports/demo.json` |
| **Hard world**: 64 cases, 15 typologies incl. 5 realistic confounders (seed 7) | holds exact 1.0, over-hold ₹0, false freezes 0, drafts passing the verifier 1.0; dispositions 0.9198, mule P / R 0.8987 / 0.966, innocent-holder P / R 0.9429 / 0.6875, merchant P / R 1.0 / 1.0 | `evals/reports/hard.json` |
| Hard world, held-out seed 101 (62 cases) | holds exact 1.0, over-hold ₹0, false freezes 0; dispositions 0.9139 (heuristic), 0.9569 with XGBoost trained on seed 202 | `evals/reports/hard-101.json` |
| Five held-out demo-size worlds, 202 cases on unseen seeds | hold exact-match 100% and 0 false freezes in every world; innocent-holder recall 75% to 100%; disposition accuracy 98.4% to 100% | `evals/reports/classify-after-heldout-*.json` |
| Hallucination stress test: the stub LLM writes a wrong rupee amount into every prose field | 27.57% of drafts had their prose rejected and replaced by fixed text; drafts passing the verifier still 1.0, holds still exact | `evals/reports/baseline/demo-fake-hallucinate.json` |
| Legal quotes, pinned figures and radar quotes | 121 of 121 checks pass: 35 citation ids (32 verbatim quotes, 3 judgments cited by URL), 33 pinned facts, 52 regulatory-radar quotes and the radar registry | `uv run python -m wapsi.search.check_citations` (after `wapsi.search.fetch` and `wapsi.search.radar fetch`) |
| Tamper-evident audit log | 6 of 6 tampering attempts on a 40-event case log detected (edit, re-hash one event, delete, reorder, truncate, re-hash the whole tail), plus an edit inside the store | `uv run python -m wapsi.audit drill` |
| Prompt-injection guard, blind holdout of 151 texts | precision 0.9661, recall 0.75 | `evals/reports/guard-latest.json` |
| Voice intake, 6 synthetic Hinglish voice notes (offline model) | every sample reconciles as documented: the showcase victim's note matches 9 of 9 claims; two notes with spoken "release the hold" instructions get them quoted, never followed; claimed figures the ledger lacks show as mismatch or not in ledger | `tests/intake`, `data/samples/voice/manifest.json` |
| Test suite at the pre-event baseline | 1,765 tests pass, 0 fail (full suite, 07-10-2026) | `uv run pytest -q` |

**Every miss is a label, never money.** The hard world adds sleeper mules, task-scam victim chains, a labour
contractor's salary pool, a festival-season home seller and same-second relays. Dispositions drop to 0.9198, but
holds stay exact, over-hold stays ₹0 and false freezes stay 0: a wrong label changes the paperwork (an EDD memo
instead of an STR ground, or the reverse), and a supervisor approves every draft. The guard misses a quarter of
unseen attacks; it only warns the officer, and the template-bound drafts and the verifier are the real defence
([Trust and safety](docs/trust-and-safety.md), [Limitations](docs/limitations-and-roadmap.md)).

## How it works

```mermaid
flowchart TB
    A["CFCFRMS notice<br/>(NCRP / 1930 complaint)"] --> B["Screen untrusted text"]
    B --> C["Trace the trail<br/>and per-victim taint"]
    C --> D["Classify accounts<br/>(rules, scorer, GNN)"]
    D --> E["Amount-specific<br/>hold plan"]
    E --> F["Template drafts<br/>(Gemma 4 on AMD: prose only)"]
    F --> G["Independent verifier"]
    G --> H["Supervisor approval<br/>(SHA-256 bound)"]
    H --> I["Actions, SOP clocks,<br/>hash-chained audit log"]
```

| Path | What |
|---|---|
| `src/wapsi/graph` | trail expansion, exact taint replay (pro-rata / FIFO / LIFO), hold plan, account-level exception |
| `src/wapsi/classify` | account features, transparent heuristic scorer, optional XGBoost, bona fide recipient vs unwitting conduit, plain-English reasons |
| `src/wapsi/draft` | fact sheets, document templates (incl. the LEA clarification letter), LLM prose through placeholders |
| `src/wapsi/verify` | numeric, citation, policy and prompt-injection checks; police / LEA request checklist |
| `src/wapsi/domain` | India bank working-day calendar, SOP clocks, regulatory change radar (read-only) |
| `src/wapsi/audit` | hash-chained, append-only audit log per case and the tamper drill |
| `src/wapsi/intake` | voice-note intake: audio checks, the schema-bound extraction, phone masking, guard screening, reconciliation of every claim with the ledger, the bundled synthetic samples |
| `src/wapsi/agenttrace` | agent trace: every tool call an agent makes, with redacted arguments, result, latency and the guard and verifier verdicts around it, sealed into the case's audit chain |
| `src/wapsi/adapters` | ledgers, case stores and LLM clients, among them `OpenAICompatLLM` for any OpenAI-compatible endpoint |
| `src/wapsi/search` | legal corpus fetcher, section-aware chunking, pure-Python BM25, citation and radar checks |
| `src/wapsi/synth` | synthetic Indian banking world (demo, full and hard sizes), independent reference implementation, golden sets |
| `src/wapsi/pipeline` | end-to-end case processing, maker-checker approval, account-level requests, LEA checks |
| `ui/` | Streamlit officer app: Inbox, Voice Intake, Case Room (money trail and agent trace), Hold Plan, Drafts & Verification, Approvals, Clocks, Regulatory Radar, KPIs, Audit |
| `evals/` | golden-set harness (`--llm`, `--scorer`, `--world-dir`, `--report-name`), guard corpus and reports |
| `editions/amd/` | the AMD layer: the app on an OpenAI-compatible endpoint, the GraphSAGE scorer, vLLM serving, LoRA pipeline, benchmarks, runbook |
| `editions/gcp/` | the Google ADK agent app, plus optional managed-cloud adapters (below) |
| `.claude/skills` | 6 skills shared by coding agents (mirrored to `.agents/skills`) |

More: [Architecture](docs/architecture.md) and the [decision records](docs/adr/README.md).

## One core, thin platform layers

The Python core (`src/wapsi`) and the officer app (`ui/`) decide every number, disposition, draft and citation.
A platform layer only chooses where the data lives, which model writes the prose and how the app is served; the
showcase numbers are the parity test for all of them. Details: [Editions](docs/editions.md).

| Layer | What it is | Status (2026-10-14) |
|---|---|---|
| [AMD](editions/amd/README.md) | **The hackathon entry.** Gemma 4 on AMD GPUs for prose, extraction and the agent; a GraphSAGE mule scorer on ROCm; endpoint, notebook and droplet modes. | Core and AMD layer code existed before the event and are tagged `amd-baseline`. Event-week work and every AMD measurement are listed in [docs/originality.md](docs/originality.md). |
| [Google ADK agent](editions/gcp/README.md) | The officer's agent, built with Google's open-source ADK. In this entry its model is Gemma 4 on AMD. | Built and tested offline with a scripted model before the event; Gemma 4 on AMD as its model is event-week work. |
| [Optional managed-cloud adapters](editions/gcp/README.md) | BigQuery ledger, Firestore case store with a transaction-sealed audit chain, Model Armor guard, Cloud Run deploy scripts. Not part of the hackathon entry. | Built and unit-tested offline with fakes. Nothing is deployed. |

## Documentation

| If you want to... | Read |
|---|---|
| Judge the entry fast | [pitch/START_HERE_FOR_JUDGES.md](pitch/START_HERE_FOR_JUDGES.md), [pitch/TRACK_FIT.md](pitch/TRACK_FIT.md), [pitch/NUMBERS.md](pitch/NUMBERS.md) |
| Get the whole picture in a few minutes | [Home](docs/index.md), [Overview](docs/overview.md) |
| See what runs on AMD and in which mode | [Editions](docs/editions.md), [editions/amd/README.md](editions/amd/README.md) |
| Use the officer app screen by screen | [User guide](docs/user-guide.md), [Voice intake](docs/voice-intake.md) |
| See how the pieces fit | [Architecture](docs/architecture.md), [Decision records](docs/adr/README.md) |
| Check the legal basis of every rule | [Legal spine](docs/legal-spine.md), [Glossary](docs/glossary.md) |
| Follow the algorithms | [Money trail](docs/algorithms/money-trail.md), [Hold plan](docs/algorithms/hold-plan.md), [Classification](docs/algorithms/classification.md) |
| Judge whether it can be trusted | [Trust and safety](docs/trust-and-safety.md), [Evaluation](docs/evaluation.md), [Synthetic data](docs/synthetic-data.md) |
| Build on it | [Developer guide](docs/developer-guide.md), [API reference](docs/api-reference.md), [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) |
| Read the contracts | [Data contract](docs/data-contract.md), [Draft contract](docs/draft-contract.md), [Eval report format](docs/eval-report.md) |
| Check what was built when | [Originality](docs/originality.md) |
| Get straight answers | [FAQ](docs/faq.md), [Limitations and roadmap](docs/limitations-and-roadmap.md) |

## Trust and safety

- **Numbers come from data, words come from the LLM.** Every amount, account number, UTR and date in a draft is a
  slot filled from case tables. The verifier recomputes each one and checks that it sits in the right place; if
  the LLM's prose fails a check, a fixed paragraph replaces it.
- **No invented law.** Every rule cites an id from `src/wapsi/citations.py`; stored quotes are verbatim, at most
  25 words, and checked against the downloaded texts ([Legal spine](docs/legal-spine.md)). RBI fraud-risk
  citations point to the (Commercial Banks – Fraud Risk Management) Directions, 2026; the RBI mule-account SOP of
  11 Sep 2026 is labelled DRAFT everywhere and never treated as in force.
- **Amount-specific by default; account-level only by recorded exception.** The verifier fails any plan or draft
  with an account-level hold that lacks a recorded reason or a valid supervisor approval, and any draft that
  instructs a whole-account or debit freeze outside such an exception.
- **Complaint text cannot change a hold.** Narratives never feed tracing, classification or the hold plan. The LLM
  sees them only inside `UNTRUSTED` markers, and the guard flags instruction-like text for the officer.
- **A voice note is a claim, not a fact.** Every value heard is reconciled with the ledger, the ledger's figure is
  the one used, an amount alone never identifies a complaint, and spoken instructions are quoted, never followed.
- **The agent is accountable.** Every tool call it makes is recorded with its checks, redacted (account numbers,
  phones, e-mails, secrets, complaint text) and sealed into the case's audit chain.
- **Nothing happens without a supervisor, and nothing is forgotten.** An approval applies only to the exact
  verified draft (SHA-256 fingerprint); the audit log is hash-chained and append-only; auditors see names and
  mobile numbers masked. See [SECURITY.md](SECURITY.md).
- **Only synthetic data leaves the machine.** A shared model endpoint is a trial service; nothing real is ever
  sent to one, and no key is ever committed.

## Data and licence

All banking data is synthetic, generated by `wapsi.synth`; no real customer, victim or employer data is used.
Public legal texts are downloaded at build time and are not redistributed beyond short attributed quotes
(`data/docs/MANIFEST.md`). Wapsi is decision support for trained officers: it is not legal advice, and it does not
act on its own. The code is MIT-licensed ([LICENSE](LICENSE)).

## Originality

Wapsi is entered in the lablab.ai AMD Developer Hackathon ACT III (build window 12 to 18 October 2026). The
portable core, the officer app and the prepared AMD layer existed before the event; that state is the git tag
**`amd-baseline`** (commit `1d500f6`, last pre-event commit dated 2026-10-07). Everything committed after the
event opened is event-week work, and every figure that names AMD was measured during the event.
[docs/originality.md](docs/originality.md) lists both, so the rules can be checked against the git history with
`git diff amd-baseline`. Commit dates are never rewritten.
