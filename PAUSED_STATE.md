# PAUSED: adversarial review of feat/gcp-one-command-deploy

Paused on the orchestrator's request on 2026-10-07 at about 20:50 IST. Builder commit: 6fc3624. Review work is
committed as "WIP: paused" (76c3d80). No merge is in progress, and main (ec2cb2e) is **not merged yet**.

## Done (verified by running things)

- **Dry runs.** Windows PowerShell 5.1, PowerShell 7 and bash produce identical 12-step plans; only the teardown
  hint differs.
- **Live runs against native fakes.** A real `gcloud.cmd` and `uv.cmd` on PATH, not shell functions, under
  PowerShell 5.1 and 7.
  - The secret file reaches gcloud byte-exact.
  - A failing `gcloud builds submit` stops the script with exit code 1.
- **Judging world.** Full golden sets: showcase 2/2 and demo 39/39. Hold and disposition are 1.0, drafts verify at
  1.0, and the metrics are identical to each world run on its own.
- **Local Docker build.** The image built through the judging-world bake step. The final `chown` layer was still
  running at the pause, so `docker run` was never tried.
- **Fixed** (in 76c3d80, each with tests):
  1. `.gcloudignore`: the unanchored `docs/` (and `tests/`, `scripts/`, ...) also matched `data/docs`. Cloud Build
     would get no citation corpus, so `COPY data/docs` would fail or the image would lack citations.json. Every
     top-level directory is now anchored with `/`. A test emulates the upload with `git ls-files -ci --exclude-from`.
  2. `deploy.sh`: `${billing,,}` is bash 4 only and fails on macOS bash 3.2, which DEPLOY.md offers. Replaced, and
     a test bans bash-4-only syntax.
  3. Both scripts: `--quiet` on the billing describe and budgets list captures. Their output is hidden, so a gcloud
     prompt would have hung the script silently.
  4. Both scripts print the judged URL as the case-room deep link
     (`/?page=case-room&case=CASE-31009260000101`). The UI opens on the Inbox otherwise.
  5. Stale fallback prose. If the first deploy had a bad key or model, the seeded cases kept fallback prose
     forever, because seeding never overwrites and the smoke test still passed.
     - `POST /demo/seed?redo_fallback=1` re-processes only cases whose drafts have `llm_fallback`. The new run goes
       onto the case history: one audit chain via `chain_extend`, earlier approvals kept.
     - The smoke step uses it and warns "N of M drafts use the deterministic fallback paragraph".
  6. Windows PowerShell 5.1: TLS 1.2 opt-in for the smoke calls.
  7. ResilientLLM no longer prices self-hosted `openai:` models (the AMD vLLM eval) at Gemini rates. The cost block
     is n/a unless WAPSI_LLM_PRICE_IN/_OUT are set, and the harness markdown handles that.
  8. Kept from the duplicate agent: `GeminiLLM` chains its give-up error to the SDK error, and `_RETRY_DELAY` also
     parses "retry in Ns". Without these, the real 429 message was truncated to 500 characters and RetryInfo was
     lost under `max_retries=0`. The tests pass.

## Test status at pause

- `uv run ruff check src tests evals editions/gcp`: all checks passed.
- `uv run pytest -q tests/gcp/test_gcp_one_command_deploy.py tests/gcp/test_gcp_seed_demo.py
  tests/gcp/test_gcp_eval_gemini.py tests/test_llm_resilience.py`: 43 passed and 1 failed. The failure was my new
  bash-4 test matching a comment; the comment was reworded, and the rerun of the deploy subset gave 5/5.
- **Not yet run after the fixes:** the full suite. The builder reported 1539 passed; tests/amd needs torch.

## Left to do (exact next commands, in the worktree)

```bash
cd Z:/Projects/Hakathon/wapsi-wt/gcp-one-command-deploy
uv run pytest -q --basetemp=.pytest_tmp_review-deploy tests/gcp tests/test_llm_resilience.py   # confirm green
git merge main      # accept every deletion of editions/snowflake/**, tests/snowflake/**, tests/test_coco_hooks.py,
                    # src/wapsi/adapters/{snowflake_ledger,cortex_llm}.py, pitch/coco/**; add no Snowflake/Cortex/CoCo
                    # text; for docs and tests/gcp/test_gcp_deploy_assets.py keep main's version + this feature's adds.
                    # .gcloudignore: drop the /editions/snowflake/ line if main removed that tree.
uv sync --all-extras
uv run pytest -q --basetemp=.pytest_tmp_review-deploy
uv run ruff check
git commit          # the merge, ending with the Co-Authored-By line
```

Optional: `docker build -f editions/gcp/Dockerfile -t wapsi-gcp-review .`, then
`docker run -p 8080:8080 -e WAPSI_DEMO_MODE=1 -e WAPSI_WORLD=/app/data/world -e WAPSI_SEED_CASES=showcase
wapsi-gcp-review`, then `curl -X POST localhost:8080/demo/seed`, then `curl localhost:8080/demo/cases/CASE-31009260000101`.

## Review findings still open (not fixed)

- **Docs index not in the image.** `data/docs/index` is generated from raw/, which is gitignored and not uploaded,
  so the image serves a citations-only corpus (`search_sop` is weaker). This predates the branch.
- **Approvals carry over between judges.** The deployment is public, and judge approvals persist in Firestore, so
  the next judge sees an already-approved case. A "reset showcase" path is not built.
- **Concurrent-seeding race.** During a redeploy, the old and new revisions could both seed at once; the last write
  wins. This is harmless on synthetic data.
- **Untested on real systems.** The scripts have not run against real Google Cloud, and Cloud Build has not been
  tried with BuildKit and the `--mount=type=cache` Dockerfile. If buildx is missing in
  `gcr.io/cloud-builders/docker`, the build fails; both of those predate the branch.
- **Cache key masks only `opened_at`.** The demo-world rerun cache-hit rate with real Gemini is unverified, since
  only showcase was tested with the mock.
