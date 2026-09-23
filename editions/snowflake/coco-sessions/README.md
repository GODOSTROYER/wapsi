# CoCo session evidence

The CoCo GCC hackathon asks for a solution built natively on Snowflake **with CoCo CLI across the whole
lifecycle**, and judges look for evidence at every stage. This folder is that evidence. Keep it honest: if
something was edited by hand, the note says so.

## Layout
```
coco-sessions/
  README.md          this file
  save_session.py    copies a CoCo transcript here with secrets redacted and appends INDEX.md
  INDEX.md           one row per saved session (created by save_session.py)
  transcripts/       <YYYY-MM-DD>_<STEP>_<slug>.<ext>   (or a folder if CoCo stored the session as a folder)
  screenshots/       <STEP>_<slug>_<n>.png
  notes/             <STEP>.md — optional short write-up
```

## Step ids
| Prefix | Meaning | Example |
|---|---|---|
| `S00`–`S22` | playbook steps (`../COCO_PLAYBOOK.md`) | `S06_trace-procs` |
| `A01`… | CoCo automation runs (export with `cortex conversations transcript <thread_id>`) | `A01_nightly-eval` |
| `H01`… | headless runs (`cortex -p` / `cortex exec`, CI) | `H01_ci-eval-summary` |
| `F01`… | finale rehearsals and the live demo | `F02_rehearsal-live` |

Slugs are lowercase kebab-case (`trace-procs`), dates are the save date.

## Saving a session
CoCo prints the session id at start and exit and stores sessions under `%USERPROFILE%\.snowflake\cortex\conversations\`.
```
uv run python editions/snowflake/coco-sessions/save_session.py --session latest --step S06 --slug trace-procs --gate G1 --note "TRACE_CASE parity OK on showcase"
uv run python editions/snowflake/coco-sessions/save_session.py --session <id-fragment> --step S10 --slug draft-verify-approve --gate G1
uv run python editions/snowflake/coco-sessions/save_session.py --source %TEMP%\nightly.txt --step A01 --slug nightly-eval --gate G2
```
The script redacts JWTs, bearer tokens, `password=`/`token=`-style values, Snowflake account host names, e-mail
addresses, Windows/Unix user names in paths, Slack and Atlassian tokens. **Open the saved file and check it before
committing** — redaction is pattern-based. Never save a session in which a secret was pasted in clear text; rotate
that secret instead.

## Screenshots
Name them `<STEP>_<slug>_<n>.png` (for example `S13_sis-app_2.png`). Capture the prompt and the result together
where possible. Minimum set for the deck:
- Plan: `S02_workspace-setup_1` (plan mode output)
- Build: `S04_snowpark-package_1`, `S06_trace-procs_1`, `S11_semantic-view_1`, `S12_cortex-agent_1`, `S13_sis-app_1..5`
- Test: `S06_trace-procs_2` (parity), `S10_draft-verify-approve_2` (tamper caught), `S19_kpis_1`
- Run: `S07_intake-stream-task_1`, `S16_actions-automations_1..3`
- Govern: `S02_workspace-setup_2` (hook blocked DROP), `S14_governance_1..3`, `S17_guardrails_1..3`
No real personal data, account URLs or tokens may be visible; crop or blur before saving.

## Notes template (`notes/<STEP>.md`)
```
# <STEP> <title>
Goal:
Prompt used: (link to transcript line or paste)
What CoCo produced: files / objects
What we changed by hand, and why:
Acceptance output: (paste the key rows)
Surprises / doc mismatches ([verify] items resolved):
```

## Commit rule
Commit the files a session produced together with its transcript and the INDEX row, in one commit whose message
names the step (`S06: TRACE_CASE and BUILD_HOLD_PLAN procedures`). Spread commits across the event window.
