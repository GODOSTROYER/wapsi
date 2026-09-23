-- Wapsi GCP edition: BigQuery ledger dataset (docs/data-contract.md).
-- Generated from wapsi.adapters.bigquery_ledger.setup_sql(); keep them in sync
--   uv run python -m wapsi.adapters.bigquery_ledger ddl --project PROJECT_ID --dataset wapsi
-- Run:  bq query --use_legacy_sql=false --location=asia-south1 < editions/gcp/deploy/bigquery_setup.sql
-- (replace PROJECT_ID first), then load a generated world:
--   uv run python -m wapsi.adapters.bigquery_ledger load --world data/generated/demo --project PROJECT_ID
-- All data is synthetic.

CREATE SCHEMA IF NOT EXISTS `PROJECT_ID.wapsi` OPTIONS (location = 'asia-south1');

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.banks` (
  bank_id STRING NOT NULL,
  name STRING,
  is_home BOOL
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.customers` (
  customer_id STRING NOT NULL,
  name STRING,
  mobile STRING,
  pan_masked STRING,
  kyc_tier STRING,
  dob DATE,
  city STRING
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.accounts` (
  account_id STRING NOT NULL,
  bank_id STRING NOT NULL,
  customer_id STRING,
  account_type STRING,
  opened_at TIMESTAMP,
  gstin STRING,
  status STRING,
  opening_balance FLOAT64
)
CLUSTER BY bank_id, account_id;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.transactions` (
  txn_id STRING NOT NULL,
  ts TIMESTAMP NOT NULL,
  from_account STRING,
  to_account STRING,
  from_bank_id STRING,
  to_bank_id STRING,
  amount FLOAT64 NOT NULL,
  channel STRING,
  utr STRING,
  remark STRING,
  counterparty_label STRING
)
PARTITION BY DATE(ts)
CLUSTER BY from_account, to_account;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.invoices` (
  invoice_id STRING NOT NULL,
  merchant_account STRING,
  payer_account STRING,
  txn_id STRING,
  amount FLOAT64,
  ordered_at TIMESTAMP,
  delivered_at TIMESTAMP,
  order_status STRING
)
CLUSTER BY txn_id;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.gst_registry` (
  gstin STRING NOT NULL,
  legal_name STRING,
  status STRING,
  registered_at DATE
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.external_signals` (
  account_id STRING NOT NULL,
  fri_risk STRING,
  mulehunter_score FLOAT64,
  ncrp_prior_reports INT64
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.device_links` (
  device_id STRING NOT NULL,
  account_id STRING NOT NULL,
  first_seen TIMESTAMP
)
CLUSTER BY account_id, device_id;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.complaints` (
  ncrp_id STRING NOT NULL,
  reported_at TIMESTAMP,
  victim_account STRING,
  victim_name STRING,
  victim_mobile STRING,
  amount_claimed FLOAT64,
  narrative_text STRING,
  language STRING,
  category STRING,
  fir_number STRING,
  source STRING
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.complaint_txns` (
  ncrp_id STRING NOT NULL,
  txn_id STRING NOT NULL
)
CLUSTER BY ncrp_id;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.meta` (
  key STRING NOT NULL,
  value STRING
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.labels_accounts` (
  account_id STRING NOT NULL,
  role_truth STRING,
  ring_id STRING,
  typology STRING
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.labels_flows` (
  txn_id STRING NOT NULL,
  ncrp_id STRING,
  tainted_amount_truth FLOAT64
);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.wapsi.labels_complaints` (
  ncrp_id STRING NOT NULL,
  case_id STRING,
  ring_id STRING,
  typology STRING,
  is_adversarial BOOL
);

-- ---------------------------------------------------------------------------------------------------------
-- Optional pre-filter used by BigQueryLedger.reachable_accounts(): accounts reachable from the complaint
-- transfers inside the trace window. Depth guard (@max_depth), temporal guard (a hop never precedes the hop that
-- funded it), minimum amount and a cycle guard on the path string. BigQuery stops recursive CTEs after
-- 500 iterations; @max_depth keeps us far below that. Parameters: @ncrp_ids ARRAY<STRING>,
-- @window_end TIMESTAMP, @max_depth INT64, @min_amount FLOAT64.
--
-- bq query --use_legacy_sql=false --   --parameter='ncrp_ids:ARRAY<STRING>:["31009260000101"]' --   --parameter='window_end:TIMESTAMP:2026-09-13 05:32:00+00' --   --parameter=max_depth:INT64:6 --parameter=min_amount:FLOAT64:100 < reachability.sql
WITH RECURSIVE reach AS (
  SELECT
    t.to_account AS account_id,
    1 AS depth,
    t.ts AS reached_at,
    CONCAT('|', COALESCE(t.from_account, ''), '|', t.to_account, '|') AS path
  FROM `PROJECT_ID.wapsi.transactions` AS t
  JOIN `PROJECT_ID.wapsi.complaint_txns` AS ct ON ct.txn_id = t.txn_id
  WHERE ct.ncrp_id IN UNNEST(@ncrp_ids)
    AND t.to_account IS NOT NULL
  UNION ALL
  SELECT
    t.to_account AS account_id,
    r.depth + 1 AS depth,
    t.ts AS reached_at,
    CONCAT(r.path, t.to_account, '|') AS path
  FROM reach AS r
  JOIN `PROJECT_ID.wapsi.transactions` AS t ON t.from_account = r.account_id
  WHERE r.depth < @max_depth
    AND t.to_account IS NOT NULL
    AND t.ts >= r.reached_at
    AND t.ts <= @window_end
    AND t.amount >= @min_amount
    AND STRPOS(r.path, CONCAT('|', t.to_account, '|')) = 0
)
SELECT account_id, MIN(depth) AS layer, MIN(reached_at) AS first_reached_at, COUNT(*) AS paths
FROM reach
GROUP BY account_id
ORDER BY layer, account_id;
