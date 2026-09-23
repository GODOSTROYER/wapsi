# wapsi_actions_digest — CoCo automation prompt (every 60 minutes)

Reference prompt. You run unattended in a Snowflake sandbox. Read-only SQL, plus `INSERT` into
`WAPSI.APP.OPS_DIGEST`. Do not call Jira or Slack yourself unless an external Atlassian MCP server is attached to
this automation (paid accounts only); on trial accounts only report.

1. Queue health:
   ```sql
   SELECT kind, status, COUNT(*) AS n,
          MIN(created_at) AS oldest, MAX(attempts) AS max_attempts
   FROM WAPSI.CASEMGMT.ACTIONS
   WHERE created_at >= DATEADD(day, -2, CURRENT_TIMESTAMP())
   GROUP BY kind, status ORDER BY kind, status;
   ```
2. Stuck work: `PENDING` Jira/Slack rows older than 10 minutes mean the local drain runner is down; `PENDING`
   `HOLD_INSTRUCTION` / `CFCFRMS_UPDATE` rows older than 5 minutes mean task `CASEMGMT.T_APPLY_ACTIONS` is
   suspended or failing (`SELECT * FROM TABLE(WAPSI.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'T_APPLY_ACTIONS', RESULT_LIMIT => 5));`).
3. Failed rows in the last 24 hours with their `result` text (truncate to 120 chars).
4. Record:
   ```sql
   INSERT INTO WAPSI.APP.OPS_DIGEST (at, source, status, summary, details)
   SELECT CURRENT_TIMESTAMP(), 'wapsi_actions_digest', '<OK|DEGRADED>', '<one line>', PARSE_JSON('<small json>');
   ```
5. If an external Atlassian MCP server is attached **and** there are `FAILED` Jira actions older than one hour,
   you may retry each once using the payload rules in `actions-drain.md` (project `WAPSI` only), then update the row.

Output: one status line (`OK` / `DEGRADED: <reason>`) and the queue table. Never include PII or complaint text.
