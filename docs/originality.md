# Originality and build timeline

Wapsi is being entered in three hackathons. This file records what was built when, so each event's
originality rules can be checked against the git history.

| Date (IST) | What | Where |
|---|---|---|
| 2026-09-16 | Repository created; shared data contracts, showcase fixtures, AGENTS.md | `src/wapsi/{models,ports,config,money,timeutil,citations}.py`, `src/wapsi/fixtures/` |
| 2026-09-16 → | Portable core: tracing, taint apportionment, hold plan, classifier (heuristic + XGBoost), drafter, verifier, clocks, docs search, synthetic world, UI | `src/wapsi/`, `ui/`, `evals/` |
| 2026-09-16 → 2026-10-04 | Snowflake-native edition, built in Snowflake CoCo sessions | `editions/snowflake/` |
| 2026-10-04 → 2026-10-18 | Google Cloud edition (ADK agents on Cloud Run, Gemini, Gemma on Cloud Run GPU) | `editions/gcp/` |
| 2026-10-12 → 2026-10-18 | AMD edition (ROCm GraphSAGE training, Gemma LoRA fine-tune, vLLM-ROCm serving, benchmarks). Baseline tag `amd-baseline` at kickoff. | `editions/amd/` |

## Rules this satisfies
- **AI Builder Cup:** no code predates the programme launch (2026-09-01); the first commit is 2026-09-16.
- **Snowflake CoCo GCC:** Snowflake-native components are built with CoCo; session evidence in
  `editions/snowflake/coco-sessions/`.
- **lablab.ai AMD ACT III:** the AMD-specific AI work (GNN training, fine-tuning, ROCm serving, benchmarks) is
  built during the event window; everything that existed at kickoff is under the `amd-baseline` tag and is
  disclosed here.

All data is synthetic. Public legal texts are fetched at build time and are not redistributed beyond short
attributed quotes.
