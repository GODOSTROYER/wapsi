# Paused state: feat/voice-intake (adversarial review)

Paused on 2026-10-07 at the orchestrator's request. Nothing is uncommitted. No merge is in progress.

## Done

- Reviewed the builder's commit c89aa00 and fixed the problems on this branch in commit a035c86:
  - UI crash: `st.audio(format=None)` broke the page for every non-WAV upload (MP3, OGG/Opus, M4A, WebM),
    including WhatsApp notes. The playback type now comes from the file's bytes, and a file that is not audio
    is refused before playback.
  - Reconciliation: one reported transfer now backs at most one claimed payment. OTHER amounts (a balance or a
    fee) are never shown as a red mismatch and never count as matching evidence. The complaint-total row shows
    the ledger figure, not the caller's figure. The "sent to" account is checked against the receiving
    account. The index fetches transfers in parallel (BigQuery).
  - Privacy and guard: the phone is masked in every free-text field and the trunk prefix 0 is handled. The
    name, bank and beneficiary fields are screened by the injection guard.
  - Gemini `max_output_tokens` raised from 4096 to 8192, because thinking tokens count against the limit.
  - `UTILITY_BILL_SCAM` added to the fraud-type enum.
  - Three demo-world samples added (synth demo seed 7, the ledger Cloud Run loads), so voice intake reconciles
    on the deployed app. The UI lists the samples the active ledger holds first.
  - The TTS generator now reports readable errors.
  - Tests added for each fix. The OGG regression test fails on the old code.
- Merged main (ec2cb2e, the Snowflake removal) as merge commit 3f3d2d1. The merge was clean and needed no
  conflict resolution. No Snowflake, Cortex or CoCo text is in the feature files. `uv sync --all-extras` ran.

## Test status

- Before the merge (on a035c86):
  - Focused voice suites passed: tests/intake (49), tests/ui/test_ui_voice_intake.py (15),
    tests/gcp/test_gcp_voice.py and test_gcp_agents.py, and tests/ui/test_ui_app.py.
  - An earlier full run on c89aa00 (without tests/amd and tests/snowflake) gave 1563 passed, 11 skipped.
- `uv run ruff check`: all checks passed (after the merge too).
- Full suite after the merge (3f3d2d1): **not finished**. It was stopped at about 67% for the pause, with no
  failures up to that point.
- Checked by hand in a browser:
  - Meera: 9/9 matched. Arjun: 8 matched, 2 mismatched, 1 not found. Fatima: the injection warning appears.
  - Open case went to CASE-31109260000102.
  - An OGG upload plays and shows the offline message. A PDF renamed to .wav is refused.

## Left (exact next commands)

```
cd Z:/Projects/Hakathon/wapsi-wt/voice-intake
uv sync --all-extras
uv run ruff check
uv run pytest -q --basetemp=.pytest_tmp_review-voice -p no:cacheprovider     # full suite on the merge
```

If it is green, the branch is ready. The merge commit's message lacks the Co-Authored-By trailer. That is
cosmetic, so leave it unless the orchestrator wants the message reworded.
