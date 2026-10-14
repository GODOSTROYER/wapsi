# Originality and build timeline

Wapsi is entered in one hackathon: the lablab.ai **AMD Developer Hackathon ACT III**. Its build window runs from
12 October 2026, 15:00 UTC, to the submission deadline on 18 October 2026, 15:00 UTC (20:30 IST). This page
records what existed before the window opened and what was built inside it, so the event's originality rule can
be checked against the git history. Commit dates are never rewritten, and history is never rebased.

## How to check it yourself

| Question | Command |
|---|---|
| What existed before the event? | `git show amd-baseline --stat` and `git log amd-baseline` |
| What was added after the baseline? | `git diff amd-baseline --stat` and `git log amd-baseline..HEAD --date=iso` |
| Which of those commits are event-week work? | the ones dated on or after 2026-10-12 15:00 UTC: `git log amd-baseline..HEAD --since="2026-10-12T15:00:00Z"` |

**The baseline.** The git tag `amd-baseline` points at commit `1d500f6`, dated 2026-10-07. It is the last
pre-event commit on `main`. Everything under the tag is pre-event work and is disclosed below.

## Before the event (disclosed)

| Dates (IST) | What | Where |
|---|---|---|
| 2026-09-16 | Work starts: shared data contracts, showcase fixtures, AGENTS.md. The first commit is dated 2026-09-23. | `src/wapsi/{models,ports,config,money,timeutil,citations}.py`, `src/wapsi/fixtures/` |
| 2026-09-16 → 2026-09-23 | Portable core: tracing, taint apportionment, hold plan, classifier (heuristic and XGBoost), drafter, verifier, clocks, legal corpus search, synthetic world and golden sets, pipeline, evaluation harness, Streamlit officer app, agent skills, the ADK agent app and managed-cloud adapters scaffold | `src/wapsi/`, `ui/`, `evals/`, `.claude/skills/`, `editions/gcp/` |
| 2026-09-23 → 2026-10-07 | **AMD layer code prepared before the event**: vLLM-ROCm serving scripts and docker-compose, the app on an OpenAI-compatible endpoint, a benchmark suite, a pure-PyTorch GraphSAGE mule scorer (trained and evaluated on CPU only), a LoRA fine-tuning, merge and evaluation pipeline (tested on tiny CPU models only), a runbook | `editions/amd/` |
| 2026-09-23 → 2026-10-07 | Legal robustness work, the hash-chained audit log, the regulatory radar, the hard world, the documentation site and a first pitch pack | `src/wapsi/`, `docs/`, `pitch/` |
| 2026-09-23 → 2026-10-04 | An edition for a separate, earlier event, built on the same core. It was submitted to that event, removed from the tree on 2026-10-07 and is preserved in the git history (last at commit `13ee458`). It is not part of this entry. | (removed; was under `editions/`) |
| 2026-10-04 → 2026-10-07 | The ADK agent's tool trace, Hinglish voice-note intake with a hosted multimodal model and an offline model, the Firestore-sealed audit chain | `src/wapsi/agenttrace/`, `src/wapsi/intake/`, `src/wapsi/adapters/firestore_store.py`, `editions/gcp/` |

The removed edition's files appear in the git history from the first commit up to their removal on 2026-10-07,
so a diff across that range shows them being added and deleted. They are not part of this entry.

**Pre-event commits that are not under the tag.** Three branches were paused on 2026-10-07, after the baseline
commit, and are merged during the event. Their commits dated 2026-10-07 are pre-event work, not event-week work:

| Branch | Pre-event commits (all dated 2026-10-07) | What they hold |
|---|---|---|
| `wip/docs-features` | `f51cb10` | documentation of voice intake, the agent trace and the sealed audit chain; a pitch refresh |
| `feat/amd-kickoff-automation` | `5d260b7`, `b10721e`, `c1aa65d`, `88e62b5`, `69bfd96`, `d826db4`, `d743cbe` | a one-command run for a day on an AMD droplet: preflight, a resumable runner, an evidence bundle, a pitch filler |
| `feat/gcp-one-command-deploy` | `6fc3624`, `76c3d80`, `42cd831`, `b74892b`, `afcd574` | a one-command deploy script for the optional managed-cloud adapters and resilience for hosted-model evaluation |

Before the event, nothing in the repository had run on AMD hardware or against an AMD-served model. The only
model results committed at the baseline are labelled CPU-development checks.

## Built during the event (12 to 18 October 2026)

This section is completed when the event-week branches are integrated. Each row gets its commit range and, where
it reports a figure, the results file and the date of the run.

| Work | Branch | Commits | Builds on (pre-event) |
|---|---|---|---|
| The Track 3 "Reinvent Commerce" product story: README, these docs, the pitch pack (deck, video script, form fields, track fit, partner-prize pages), the open-slot rule for unmeasured AMD figures | `wip/docs-features` | `b103532` (2026-10-14) and the commits after it on this branch | the earlier docs and pitch pack |
| A live smoke run against the AMD endpoint with a health probe (`pitch/amd/evidence/token_factory_smoke.py`) | `wip/docs-features` | `b103532` (2026-10-14); no results file committed yet | `OpenAICompatLLM`, the pipeline, the showcase fixture |
| Gemma 4 on AMD GPUs as the drafting and extraction model (endpoint mode: `json_object` output, validation and repair, rate-limit handling) | to be filled at integration | to be filled | `OpenAICompatLLM`, the AMD app settings |
| Open-model voice intake: a Whisper-class speech model plus Gemma 4 on AMD for the fields | `feat/open-voice` | to be filled | `wapsi.intake` (schema, masking, reconciliation), the six bundled samples |
| The ADK agent on Gemma 4 served on AMD: LiteLLM connector, native tool calls, JSON-protocol fallback | `feat/adk-on-amd` | to be filled | the ADK agent app and the agent trace |
| Kickoff automation, finished and verified | `feat/amd-kickoff-automation` | event-week commits to be filled | its pre-event commits listed above |
| Hosting of the demo application | to be filled at integration | to be filled | the Dockerfiles and the officer app |
| Every measurement with an AMD-served model or on AMD hardware | to be filled at integration | results files and run dates to be filled | the evaluation harness, the benchmark suite, the GNN code |

Rules for this section:

- A figure with a real model in the loop is reported only from a results file committed during the event, and it
  says where it ran: AMD Token Factory (models served on AMD GPUs by AMD's API), an AMD GPU notebook, or an
  MI300X droplet. A result from the shared endpoint is never presented as a benchmark of a dedicated MI300X.
- CPU and rehearsal numbers are labelled as such and are never reported as AMD results.
- No model training, fine-tuning, serving or benchmark on AMD hardware happened before 12 October 2026.

## Rules this satisfies

- **Original work, MIT-licensed.** The code is the entrant's own, under the MIT licence (`LICENSE` in the
  repository root).
- **Pre-event code is disclosed.** It is identifiable in the git history as everything under the tag
  `amd-baseline`, plus the pre-event branch commits listed above.
- **Training data.** Outputs of hosted proprietary models are never used to train another model. Any fine-tune
  uses the open model's own verifier-filtered outputs.
- **Data.** All banking data is synthetic. Public legal texts are fetched at build time and are not redistributed
  beyond short attributed quotes.
