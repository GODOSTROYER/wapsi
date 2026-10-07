# Templates — reference only (to be completed and validated in CoCo)

Everything in this folder is a **reference** that a CoCo session copies, adapts and validates. Nothing here has
been run against Snowflake. Copying a template into place is itself a CoCo step (see `../COCO_PLAYBOOK.md`), so the
transcript shows the configuration being built and tested.

| File | Copy to | Playbook step |
|---|---|---|
| `settings.json` | `.cortex/settings.json` (project hooks) | S03 |
| `agents/sql-reviewer.md`, `agents/sop-auditor.md`, `agents/deck-writer.md` | `.cortex/agents/` | S03 |
| `mcp.json.example` | `%USERPROFILE%\.snowflake\cortex\mcp.json` (merge; keep secrets in environment variables) | S03 / S14 / S15 |
| `automations/*.md` | used as `--prompt-file` by `cortex automation create` (automations live in Snowflake, not in files) | S15 |
| `automations/drain_actions.ps1` | run in place (local 1-minute Jira/Slack drain) | S15 |
| `semantic_view_metrics.md` | input to the S10 prompt | S10 |
| `agent_instructions.md` | input to the S11 prompt | S11 |

Hook scripts referenced by `settings.json` live in `../hooks/` and are tested locally (Python, no Snowflake).

## Notes per template
- **settings.json** — CoCo reads project hooks from `.cortex/settings.json` or `.claude/settings.json`
  ([extensibility docs](https://docs.snowflake.com/en/user-guide/cortex-code/extensibility)). CoCo matchers use
  lowercase tool ids (`edit|write`, `bash`, `snowflake_sql_execute`, `mcp__.*`); Claude Code uses `Edit|Write`. Hook
  commands are relative to the repo root; if CoCo runs hooks from another directory, prefix the path with
  `%CORTEX_PROJECT_DIR%` / `$CORTEX_PROJECT_DIR`. Exit code 2 blocks (PreToolUse) or reports back (PostToolUse).
- **agents/** — frontmatter `name`, `description`, `tools`, `model` per the docs. Model ids (`claude-sonnet-5`,
  `claude-haiku-4-5`) are Cortex model names; confirm with `/model` and drop the `model:` line to inherit.
- **mcp.json.example** — `${VAR}` / `${VAR:-default}` expansion is documented. Server packages are community /
  reference servers: `mcp-atlassian` (Jira; env `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`) and
  `@modelcontextprotocol/server-slack` (env `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID`) — check each project's README
  for current names before use. Atlassian's hosted server (`https://mcp.atlassian.com/v1/mcp`, OAuth) is an
  alternative: `"type": "http"` with an `"oauth"` block. The `wapsi` entry points at the Snowflake-managed MCP
  server (`/api/v2/databases/WAPSI/schemas/APP/mcp-servers/WAPSI_MCP`) with a least-privilege PAT; use hyphens, not
  underscores, in the account host name. The PAT header pair is an assumption to verify; Snowflake OAuth via a
  security integration is the documented default.
- **Never commit** a filled `mcp.json`, PATs, Jira or Slack tokens. Set them as user environment variables
  (`setx WAPSI_JIRA_API_TOKEN ...` in a terminal you control) and restart the terminal.
