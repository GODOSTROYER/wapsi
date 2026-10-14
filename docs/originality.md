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

Five branches were built during the event week and merged into `main` on 2026-10-14 with `--no-ff`, so each
merge commit and each branch stays visible in `git log --graph`. Every commit named here is dated 2026-10-14.

| Work | Branch | Commits | Builds on (pre-event) |
|---|---|---|---|
| The Track 3 "Reinvent Commerce" product story: README, these docs, the pitch pack (deck, video script, form fields, track fit, partner-prize pages), the open-slot rule for unmeasured AMD figures | `wip/docs-features` | `b103532`, `ef62a66` | the earlier docs and pitch pack |
| A live smoke run against the AMD endpoint with a health probe (`pitch/amd/evidence/token_factory_smoke.py`) | `wip/docs-features` | `b103532`; it refused to write a result while the model was answering in garbage, so no results file is committed | `OpenAICompatLLM`, the pipeline, the showcase fixture |
| Gemma 4 on AMD GPUs as the drafting model in endpoint mode: the endpoint read from `WAPSI_LLM_BASE_URL` / `WAPSI_LLM_API_KEY` / `WAPSI_LLM_MODEL` (`wapsi.adapters.llm_env`), endpoint-neutral rate limiting, `Retry-After` and response cache, `evals.harness --llm openai-compat`, and the check that rejects degenerate model output as prose (`validate_prose`: `not_prose`) | `feat/gcp-one-command-deploy` | `1d73278` | `OpenAICompatLLM`, `ResilientLLM`, the evaluation harness |
| The public demo: an edition-neutral container, a Hugging Face Space builder, a Streamlit Community Cloud entry point, the hosting guide, the recorded-run replay with its banner and status chip, and prose recorded from an open model served on AMD (`data/recordings/demo_prose.json`: first from Qwen3.8-27B while Gemma 4 was degraded, then re-recorded from Gemma-4-31B-IT on the AMD Token Factory, 2026-10-14; both runs are listed in its `meta.runs`) | `feat/gcp-one-command-deploy`, then `main` | `1d73278`; the Gemma 4 recording in the fix-up commit after the merges | the Dockerfiles and the officer app |
| Kickoff automation, finished: three ways to run (endpoint, notebook, droplet), the metered endpoint client, the model plan, the golden-set stage, one evidence bundle, the pitch filler's wording rules | `feat/amd-kickoff-automation` | `589014e`, `0793528` | its pre-event commits listed above |
| Open-model voice intake: the speech-to-text port and its adapters (OpenAI-compatible transcription endpoint, local Whisper through transformers, a probed chat-audio adapter), transcript extraction on an open text model (JSON repair, schema validation, numbers checked against the transcript), the model chain with garbage detection and failover, `WAPSI_VOICE_BACKEND`, the Whisper server, the voice benchmark | `feat/open-voice` | `fb43b54`, `418a1f3`; results in `editions/amd/bench/results/voice-tokenfactory-20261014/` (AMD Token Factory, text model only, run 2026-10-14) | `wapsi.intake` (schema, masking, reconciliation), the six bundled samples |
| The ADK officer agent on Gemma 4 served on AMD: the OpenAI-compatible endpoint model for ADK (`LiteLlm`), the capability probe, native tool calls with a strict JSON tool protocol as the fallback, reply grounding, `POST /agent/run`, the CLI, "Ask the agent" in the Case Room | `feat/adk-on-amd` | `623bf3a`, `d2f62e2`; results in `editions/amd/AGENT.md` (AMD Token Factory, Gemma-4-31B-IT, run 2026-10-14) | the ADK agent app and the agent trace. Moved, not new: the officer tools, the guard / audit / trace plugins, the role grants and the offline planner were under `editions/gcp/app/` at the baseline and now live in `editions/shared/` |
| Fix-ups after the integration review: the health probe is never answered from a cache and a replayed smoke run is labelled as one; the AMD app paces prose and agent on one shared limiter, announces rate-limit waits and fails fast when no endpoint is configured; agent model turns timed at the HTTP request; reply grounding also checks figures written without a rupee sign; judgments cited by URL no longer warn; the harness splits the freeze baseline by account group and names the model that wrote each answer; a recorded live Gemma 4 agent session replayed in the Agent trace tab; "on AMD" for a self-hosted server only when confirmed | `main` | the commit after the integration commit | everything above |
| Integration: the five merges; one set of environment names across the pipeline, harness, agent, voice intake, demo and kickoff scripts (`tests/test_env_names_agree.py`); the pitch's `TBD(slot_id)` notation as the one way `fill_pitch.py` finds a slot; a draft named by what really served it; the prose validator's freeze-wording rule (a live model wrote of "freezing" funds, the draft failed verification); the tested commands in the README | `main` | the merge commits after `amd-baseline` and the commit that follows them | everything above |
| Every measurement with an AMD-served model | as named in the rows above | results files: `editions/amd/bench/results/agent-tokenfactory-20261014/` (the agent session, draft prose on Gemma 4 too), `evals/reports/amd/showcase-gemma4-20261014.json` (the showcase world), `editions/amd/bench/results/voice-tokenfactory-20261014/` (voice extraction), `data/recordings/demo_prose.json` (the demo's recorded prose). Evidence bundles of `kickoff/run_all.sh` go to `editions/amd/evidence/` when a run is committed; none is committed yet | the evaluation harness, the benchmark suite |
| Any measurement on AMD hardware we operate ourselves (a GPU notebook or an MI300X droplet: GNN training, LoRA, vLLM serving, benchmarks) | none yet | no such run has been made; nothing is reported | the GNN and LoRA code, the serving scripts |

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
