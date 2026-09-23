# wapsi_nightly_eval — CoCo automation prompt (daily 02:00 Asia/Kolkata)

Reference prompt. You run unattended in a Snowflake sandbox. Use SQL only (and the WAPSI_MCP tools if needed).
Do not create, replace or drop objects. Writes allowed: `INSERT` into `WAPSI.APP.EVAL_RUNS` and
`WAPSI.CASEMGMT.ACTIONS`. The Python golden-set harness runs in GitHub Actions CI; this job checks that the
Snowflake-native pipeline reproduces the golden set.

## 1. Parity metrics (Snowflake results vs `WAPSI.ML.GOLDEN_CASES`)
`GOLDEN_CASES(case_id, world, ncrp_ids ARRAY, expected VARIANT)` holds one `GoldenCase` JSON per row. Wapsi case
ids are `'CASE-' || <smallest ncrp_id>`.
```sql
WITH g AS (
  SELECT case_id AS golden_id, 'CASE-' || ARRAY_SORT(ncrp_ids)[0]::STRING AS wapsi_case_id, expected
  FROM WAPSI.ML.GOLDEN_CASES WHERE world = 'demo'
), exp_hold AS (
  SELECT g.wapsi_case_id, f.key::STRING AS account_id, f.value::NUMBER(18,2) AS expected_hold
  FROM g, LATERAL FLATTEN(input => g.expected:expected_hold) f
), exp_disp AS (
  SELECT g.wapsi_case_id, f.key::STRING AS account_id, f.value::STRING AS expected_disposition
  FROM g, LATERAL FLATTEN(input => g.expected:expected_dispositions) f
), got AS (
  SELECT case_id, account_id, hold_amount, disposition FROM WAPSI.CASEMGMT.HOLD_PLAN
)
SELECT
  (SELECT COUNT(DISTINCT wapsi_case_id) FROM g)                                                    AS golden_cases,
  (SELECT COUNT(DISTINCT g.wapsi_case_id) FROM g JOIN WAPSI.CASEMGMT.CASES c ON c.case_id = g.wapsi_case_id) AS processed_cases,
  AVG(IFF(ABS(COALESCE(got.hold_amount, 0) - e.expected_hold) <= 0.01, 1, 0))                     AS hold_exact_match_rate,
  SUM(GREATEST(COALESCE(got.hold_amount, 0) - e.expected_hold, 0))                                 AS over_hold_inr,
  COUNT_IF(COALESCE(got.hold_amount, 0) > 0.005 AND e.expected_hold <= 0.005)                      AS false_freeze_count
FROM exp_hold e
LEFT JOIN got ON got.case_id = e.wapsi_case_id AND got.account_id = e.account_id;
```
Also compute `disposition_accuracy` (join `exp_disp` to `got`) and `traced_amount_recall`
(`SUM(holdable + cashed_out + outbound + intermediary + merchant_settled) / SUM(stolen)` from `CASEMGMT.CASES`
for the processed golden cases).

## 2. Record and compare
```sql
INSERT INTO WAPSI.APP.EVAL_RUNS (world, generated_at, cases, report, source)
SELECT 'demo', CURRENT_TIMESTAMP(), <processed_cases>,
       OBJECT_CONSTRUCT('metrics', OBJECT_CONSTRUCT('hold_exact_match_rate', <..>, 'over_hold_inr', <..>,
                        'false_freeze_count', <..>, 'disposition_accuracy', <..>, 'traced_amount_recall', <..>)),
       'snowflake-parity';
SELECT generated_at, report:metrics FROM WAPSI.APP.EVAL_RUNS
 WHERE source = 'snowflake-parity' ORDER BY generated_at DESC LIMIT 2;
```
Also read the newest CI row (`source = 'ci-harness'`) if present.

## 3. Notify
Stage exactly one Slack notice (the local runner posts it):
```sql
INSERT INTO WAPSI.CASEMGMT.ACTIONS (action_id, case_id, kind, payload, status, created_at, attempts)
SELECT 'ACT-EVAL-' || TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYYMMDD'), 'SYSTEM', 'SLACK_NOTICE',
       OBJECT_CONSTRUCT('channel', '#wapsi-ops', 'text', '<one-line KPI summary with deltas>'), 'PENDING', CURRENT_TIMESTAMP(), 0
WHERE NOT EXISTS (SELECT 1 FROM WAPSI.CASEMGMT.ACTIONS WHERE action_id = 'ACT-EVAL-' || TO_VARCHAR(CURRENT_TIMESTAMP(), 'YYYYMMDD'));
```
If `over_hold_inr > 0`, `false_freeze_count > 0`, or any metric is worse than the previous parity run, also stage
a `JIRA_TICKET` action with summary `Eval regression <date>: <metric>` (same idempotent pattern, id suffix `-JIRA`).

## 4. Output
A short report: the metrics table, deltas vs previous run, the action ids staged. Numbers only from query results.
