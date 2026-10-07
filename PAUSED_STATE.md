# Paused state: feat/adk-tool-trace (adversarial review)

Paused at the orchestrator's request on 2026-10-07. Nothing is uncommitted and no merge is in progress.

## Done
- Review of the builder's commit 8c7bef7 (Agent trace panel). Fixes are in ac37d18:
  - `GET /wapsi/traces` is scoped behind IAP: an analyst sees only their own runs (new `AgentTrace.user_id`),
    while supervisors and auditors see all runs. Before this, any signed-in user could read every officer's prompts.
  - `trace_event` stores a size-bounded trace (512 KiB). Raw results go first, then middle steps, replaced by a
    `note` step that says how many were left out. Totals still count every call, so a runaway loop can't produce an
    audit event that Firestore rejects (its limit is 1 MiB per document).
  - The recorder anchors wall-clock time per run, not once per process, so timestamps no longer drift on a
    long-lived server.
  - Untrusted `quoted_spans` are counted, never copied into a trace. The showcase recording was re-made.
  - Output-guard verdicts attach to their own reply. Text sent alongside a tool call is no longer traced as the
    reply.
  - UI: a tool's own refusal reads "Blocked by tool rule" (it used to say "Blocked by guard"). Auditor masking now
    looks up case PII once per render instead of once per fragment.
  - The GCP UI in demo mode shows a spinner while its first-start offline agent run seeds the audit chain.
  - The `trace_demo` CLI no longer crashes on a cp1252 console or pipe (Windows PowerShell 5).
- main (ec2cb2e, two editions) is merged: 40f4e4d. The only conflict was AGENTS.md, resolved to main's wording plus
  the agent-trace API entry. The branch adds no Snowflake, Cortex or CoCo text.

## Test status (after the merge, at 40f4e4d)
- `uv sync --all-extras`: OK.
- `uv run ruff check`: All checks passed.
- `uv run pytest -q --basetemp=.pytest_tmp_review-trace -p no:warnings`: 1641 passed, 11 skipped (full suite,
  tests/amd included, torch 2.14.0+cpu).

## Checked by running the demo
- `uv run streamlit run ui/app.py`, then open `?page=case-room&case=CASE-31009260000101`. The "Agent trace · 8" tab
  shows the recording; refused and blocked steps render correctly.
- GCP UI in demo mode (`uv run streamlit run editions/gcp/ui_app.py`; demo mode is on when `WAPSI_PROJECT` is
  unset): the first start takes about 10-30 s, and runs show "In the case's hash-chained audit log". On the Audit
  page, "Verify chain" reports every chain intact.
- `uv run uvicorn editions.gcp.app.server:app --port 8080` plus `/run`, then `/wapsi/traces?case_id=...`: returns
  the run.
- `uv run python -m editions.gcp.app.trace_demo`: prints 5 runs (also when piped).

## Left (optional)
- Nothing is blocking. To refresh the recording after any recorder change, run
  `uv run python -m editions.gcp.app.trace_demo --write`, then
  `uv run pytest -q --basetemp=.pytest_tmp_review-trace tests/test_agenttrace.py tests/gcp/test_gcp_tool_trace.py tests/ui/test_ui_tool_trace.py`.
- No live Gemini run has been recorded (no key on this machine). Token counts show "—" in the demo.
- Delete this file before merging the branch into main.
