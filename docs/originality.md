# Originality and build timeline

Wapsi's two current hackathon entries are the Google Cloud edition (AI Builder Cup) and the AMD edition (lablab.ai
AMD Developer Hackathon ACT III). This file records what was built when, so each event's originality rules can be
checked against the git history. Commit dates are never rewritten.

| Dates (IST) | What | Where |
|---|---|---|
| 2026-09-16 | Repository created; shared data contracts, showcase fixtures, AGENTS.md | `src/wapsi/{models,ports,config,money,timeutil,citations}.py`, `src/wapsi/fixtures/` |
| 2026-09-16 → 2026-09-23 | Portable core: tracing, taint apportionment, hold plan, classifier (heuristic + XGBoost), drafter, verifier, clocks, legal corpus search, synthetic world and golden sets, pipeline, evaluation harness, Streamlit UI, agent skills, Google Cloud edition scaffold | `src/wapsi/`, `ui/`, `evals/`, `.claude/skills/`, `editions/gcp/` |
| 2026-09-23 → 2026-10-04 | An edition for a separate, earlier event, built on the same core. It was submitted to that event, removed from the tree on 2026-10-07 and is preserved in the git history (last at commit `13ee458`). It is not part of either current entry. | (removed; was under `editions/`) |
| 2026-10-04 → 2026-10-18 | Google Cloud edition completed and deployed (ADK agents on Cloud Run, Gemini) | `editions/gcp/` |
| 2026-09-23 → 2026-10-11 | **AMD edition code prepared before the event** (disclosed): vLLM-ROCm serving scripts and docker-compose, benchmark suite, pure-PyTorch GraphSAGE mule scorer (trained and evaluated on CPU only), Gemma LoRA fine-tuning / merge / evaluation pipeline (tested on tiny CPU models only) | `editions/amd/` |
| 2026-10-07 | **Written before the event, merged after kickoff** (disclosed): the one-command Cloud Run deploy, the `ResilientLLM` wrapper (rate limit, retry, response cache), the docs-index bake step and the shared-demo reset with its seeding lease. These commits (`6fc3624` to `afcd574`, all dated 2026-10-07) sat on the branch `feat/gcp-one-command-deploy`, so they are **not** inside the `amd-baseline` tag although they predate the event. | `editions/gcp/deploy/`, `editions/gcp/app/seed.py`, `src/wapsi/adapters/llm_resilience.py`, `src/wapsi/search/bake.py` |
| 2026-10-14 | **Built during the event**: the edition-neutral public demo (container, Hugging Face Space builder, Streamlit Community Cloud entry point, hosting guide), the env-configured OpenAI-compatible endpoint (AMD Token Factory / vLLM on AMD) with the generic resilience path and `--llm openai-compat` evaluation, the recorded-run replay with its banner and status chip, the guard against degenerate model output, and the prose recorded from an open model served on AMD (`data/recordings/demo_prose.json`, recorded 2026-10-14 through the AMD Token Factory). | `deploy/`, `scripts/make_space.py`, `scripts/record_demo_prose.py`, `src/wapsi/adapters/llm_env.py`, `src/wapsi/adapters/llm_replay.py`, `ui/components/llm_status.py`, `data/recordings/` |
| 2026-10-12 → 2026-10-18 | **AMD work during the event window**: all runs on AMD Instinct MI300X — GNN training, LoRA fine-tuning, merged model, vLLM-ROCm serving, benchmarks and every reported GPU number; plus any new features built during the event. Baseline tag `amd-baseline` is created at kickoff so judges can diff exactly what was added during the event. | `editions/amd/` |

The removed edition's files appear in the git history from the first commit up to their removal on 2026-10-07, so a
diff across that range (for example up to the `amd-baseline` tag) shows them being added and deleted. They belong to
neither current entry.

## Rules this satisfies
- **AI Builder Cup:** no code predates the programme launch (2026-09-01); the first commit is dated 2026-09-23 and
  work started 2026-09-16.
- **lablab.ai AMD ACT III:** the pre-event AMD code is disclosed above and is identifiable in the git history
  before the `amd-baseline` tag. No model training, fine-tuning, serving or benchmark on AMD hardware happens before
  12 Oct, and no GPU results are reported from before the event. CPU development numbers in the repo are labelled as
  such.

All data is synthetic. Public legal texts are fetched at build time and are not redistributed beyond short
attributed quotes.
