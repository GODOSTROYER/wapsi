# wapsi_clock_digest — CoCo automation prompt (daily at 10am Asia/Kolkata)

Reference prompt. The Snowflake task `WAPSI.CASEMGMT.T_RUN_CLOCKS` calls `RUN_CLOCKS()` at 09:00 IST and updates
`CASEMGMT.CLOCKS`. You explain the result and stage notifications. SQL only; writes allowed: `INSERT` into
`WAPSI.CASEMGMT.ACTIONS`. Never change clocks, holds or drafts.

1. Confirm today's run happened:
   ```sql
   SELECT name, state, scheduled_time, completed_time, error_message
   FROM TABLE(WAPSI.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'T_RUN_CLOCKS', RESULT_LIMIT => 3));
   ```
   If it failed or did not run, run `CALL WAPSI.CASEMGMT.RUN_CLOCKS();` once (it is idempotent) and say so.
2. Due and overdue items (days are computed against `WAPSI.APP.DEMO_CLOCK` when set, else now):
   ```sql
   SELECT kind, case_id, subject, due, days_to_due, status, citation_id
   FROM WAPSI.CASEMGMT.V_CLOCKS
   WHERE status IN ('WARNING', 'OVERDUE')
      OR (kind = 'STR_DUE' AND days_to_due <= 3)
      OR (kind IN ('HOLD_LAPSE', 'HOLD_LAPSE_WARNING') AND days_to_due <= 15)
   ORDER BY days_to_due, kind;
   ```
   Rules to state: hold lapses after 90 calendar days; the bank intimates the LEA at day 75 [SOP-10-1]; STR is due
   within seven working days of confirmed suspicion per the FIU-IND FAQ (PML Rule 8 says "promptly") [PMLR-8];
   grievance timelines 7 / 15 / 15 days [SOP-10-1].
3. Stage notifications, idempotent per clock and day:
   - `SLACK_NOTICE` (one per day, `#wapsi-ops`): counts by kind and the three most urgent case ids with due dates.
   - `JIRA_TICKET` for each `OVERDUE` clock and each `HOLD_LAPSE_WARNING` reached today (summary
     `<kind> <case_id> due <dd-mm-yyyy>`; action id `ACT-CLK-<clock_id>`; skip if that id exists).
4. Output: a table of due items and the action ids staged. Dates in IST (`CONVERT_TIMEZONE('UTC','Asia/Kolkata', due)`).
Never include victim names, mobiles or complaint text.
