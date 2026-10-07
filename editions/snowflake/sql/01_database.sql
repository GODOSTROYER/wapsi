-- Wapsi on Snowflake · 01 · database, schemas and stages (run as WAPSI_ADMIN, which owns everything below).
-- Idempotent. All data in WAPSI is synthetic.

USE ROLE WAPSI_ADMIN;
USE WAREHOUSE WAPSI_WH;

CREATE DATABASE IF NOT EXISTS WAPSI COMMENT = 'Wapsi: cyber-fraud money-trail copilot (synthetic data only)';

CREATE SCHEMA IF NOT EXISTS WAPSI.RAW COMMENT = 'Synthetic bank world (demo): ledger, complaints, signals';
CREATE SCHEMA IF NOT EXISTS WAPSI.RAW_SHOWCASE COMMENT = 'Hand-checked showcase world (same tables as RAW)';
CREATE SCHEMA IF NOT EXISTS WAPSI.ML COMMENT = 'Ground truth + golden set; never granted to app roles';
CREATE SCHEMA IF NOT EXISTS WAPSI.CORE COMMENT = 'Derived features (dynamic tables; deferred)';
CREATE SCHEMA IF NOT EXISTS WAPSI.CASEMGMT COMMENT = 'Cases, trails, hold plans, drafts, approvals, audit';
CREATE SCHEMA IF NOT EXISTS WAPSI.DOCS COMMENT = 'Legal corpus chunks, citation registry, Cortex Search';
CREATE SCHEMA IF NOT EXISTS WAPSI.APP COMMENT = 'Semantic view, agent, Streamlit app, settings, evals';

CREATE STAGE IF NOT EXISTS WAPSI.RAW.WORLD_STAGE
  FILE_FORMAT = (TYPE = PARQUET USE_LOGICAL_TYPE = TRUE)
  COMMENT = 'Generated Parquet worlds (synthetic)';
CREATE STAGE IF NOT EXISTS WAPSI.APP.CODE
  COMMENT = 'Python zips imported by the stored procedures (wapsi.zip)';
CREATE STAGE IF NOT EXISTS WAPSI.APP.SIS_STAGE
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Streamlit in Snowflake app files';
CREATE STAGE IF NOT EXISTS WAPSI.DOCS.DOCS_STAGE
  DIRECTORY = (ENABLE = TRUE)
  ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
  COMMENT = 'Public legal / regulatory documents';

SHOW SCHEMAS IN DATABASE WAPSI;
