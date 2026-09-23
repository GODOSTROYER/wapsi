# CoCo automations and the ACTIONS drain — reference (to be created and validated in CoCo)

## What the docs say (checked 2026-09-16)
Source: [CoCo automations](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code-automations) (Preview).
- An automation is a prompt that runs on a schedule in a **Snowflake-managed sandbox** (your laptop can be off).
  It is stored as an `AGENT TASK` object in `USER$.PUBLIC` (names prefixed `COCO_ROUTINE_`), **not** as a local
  file. The files in this folder are the prompt files passed with `--prompt-file`.
- **Minimum frequency is once per hour.** A 1-minute drain is therefore *not* possible as an automation.
- The sandbox **cannot use locally configured MCP servers** (`~/.snowflake/cortex/mcp.json`) or your local files.
  Only MCP servers attached with `--mcp <database.schema.name>` (Snowflake-managed or registered servers) are
  available. Egress beyond that is not documented (a `--github` secret enables authenticated GitHub egress).
- Runs use the creator's **default role and default secondary roles**, not the session role. Set the default role
  of the building user to `WAPSI_ADMIN` (or grant what the prompt needs) before relying on a run.
- Needs the `EXECUTE AGENT TASK` account privilege (granted to PUBLIC by default); no warehouse is required.
  Billing: task billing + CoCo tokens per run. Threads and history are kept for two months.

## Design that follows from this
| Job | Where it runs | Cadence | Why |
|---|---|---|---|
| Apply hold / CFCFRMS actions (simulated core banking) | Snowflake task `CASEMGMT.T_APPLY_ACTIONS` (SQL only) | 1 min, `WHEN SYSTEM$STREAM_HAS_DATA` | no egress, no LLM needed |
| Jira / Slack actions | **local runner** `drain_actions.ps1` → `cortex -p` with Jira + Slack MCP from `mcp.json` | 1 min, and only when `PENDING > 0` | trial has no egress; local MCP servers are only visible to local CoCo |
| `wapsi_actions_digest` | CoCo automation | every 60 minutes | reports stuck / failed actions; with a paid account and an external Atlassian MCP server object it may create the Jira tickets itself |
| `RUN_CLOCKS()` | Snowflake task `CASEMGMT.T_RUN_CLOCKS` | daily 09:00 Asia/Kolkata (`USING CRON 0 9 * * * Asia/Kolkata`) | deterministic |
| `wapsi_clock_digest` | CoCo automation | daily at 10am Asia/Kolkata | explains overdue / lapsing items, stages Slack + Jira actions |
| `wapsi_nightly_eval` | CoCo automation | daily at 2am Asia/Kolkata | Snowflake-side parity eval against `WAPSI.ML.GOLDEN_CASES`; full Python harness stays in GitHub Actions CI |

## Create (in a CoCo session, from the repo root; capture the transcript as evidence)
```
cortex automation create --name wapsi_nightly_eval --prompt-file editions/snowflake/templates/automations/nightly-eval.md --schedule "daily at 2am" --timezone Asia/Kolkata --mcp WAPSI.APP.WAPSI_MCP
cortex automation create --name wapsi_actions_digest --prompt-file editions/snowflake/templates/automations/actions-digest.md --schedule "every 60 minutes" --mcp WAPSI.APP.WAPSI_MCP
cortex automation create --name wapsi_clock_digest --prompt-file editions/snowflake/templates/automations/daily-clocks.md --schedule "daily at 10am" --timezone Asia/Kolkata --mcp WAPSI.APP.WAPSI_MCP
```
Optional flags: `--model <id>`, `--no-email` (CLI ≥ 1.1.87), `--pre-run-hook "<bash>"` / `--post-run-hook "<bash>"`
(max 300 s). Inside a session you can also use `/automation`.

Operate:
```
cortex automation list
cortex automation describe wapsi_nightly_eval
cortex automation execute wapsi_nightly_eval --wait      # run now (use for evidence)
cortex automation doctor wapsi_nightly_eval --limit 10   # runs and thread ids
cortex conversations transcript <thread_id> > %TEMP%\nightly.txt
cortex automation suspend|resume|drop <name>
```
Save each run as evidence:
`uv run python editions/snowflake/coco-sessions/save_session.py --source %TEMP%\nightly.txt --step A01 --slug nightly-eval --gate G2`

## Local 1-minute drain (Jira / Slack)
`drain_actions.ps1` checks `PENDING` Jira/Slack actions with one cheap `snow sql` count and calls CoCo only when
there is work (token guardrail). Run it in a spare terminal during demos:
```
pwsh -File editions/snowflake/templates/automations/drain_actions.ps1 -Connection wapsi
```
or register it with Windows Task Scheduler (every minute, one pass per run):
```
schtasks /Create /SC MINUTE /MO 1 /TN "Wapsi ACTIONS drain" /TR "pwsh -NoProfile -File Z:\Projects\Hakathon\wapsi\editions\snowflake\templates\automations\drain_actions.ps1 -Once"
```
Headless fallback on Linux/CI: `cortex -p "<same one-line prompt>" -c wapsi` in a cron job or GitHub Actions
schedule (GitHub's minimum schedule interval is 5 minutes).

## Unverified — confirm in the first automation session
- Exact `--mcp` semantics (Snowflake-managed `WAPSI.APP.WAPSI_MCP` vs external MCP server objects).
- Whether `--allowed-tools` takes a comma-separated list and accepts `mcp__jira__*` wildcards.
- `snow sql --format JSON` row shape for a single statement (the runner expects `[{"N": 0}]`).
- Schedule strings with minutes (`daily at 10:30am`) — the docs show `daily at 9am` and `every Tuesday at 1:15pm`.
