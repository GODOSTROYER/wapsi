-- Wapsi on Snowflake · 30 · Snowpark Python procedures (run as WAPSI_ADMIN after the zip is on the stage:
--   uv run python -m editions.snowflake.deploy --step package   # uploads @WAPSI.APP.CODE/wapsi.zip
-- ). Handlers live in editions/snowflake/snowpark/wapsi_sf/procs.py and are thin wrappers over the portable
-- core in src/wapsi. Python 3.12, Snowflake Anaconda channel packages only (keep in sync with build_zip.py).
-- Owner's rights for the case pipeline (callers need no access to RAW); caller's rights for APPROVE
-- (maker-checker checks the caller's own roles). APPROVE hands an approved ACCOUNT_LEVEL_HOLD_REQUEST to the
-- owner's-rights APPROVE_ACCOUNT_LEVEL (it needs the ledger) with the approver it established.
-- Every audit event is sealed into the case's hash chain by the Python case store (INSERT-only AUDIT_LOG).
-- Deliberately not exposed to the agent: APPROVE, APPROVE_ACCOUNT_LEVEL, REQUEST_ACCOUNT_LEVEL_HOLD, APPEND_AUDIT.

USE ROLE WAPSI_ADMIN;
USE WAREHOUSE WAPSI_WH;

CREATE OR REPLACE PROCEDURE WAPSI.APP.WAPSI_VERSION()
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Smoke test: imports the core from the zip and traces the showcase fixture (no table access)'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import version_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.TRACE_CASE(P_NCRP_ID VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Trace the money trail for an NCRP id and its case (pro-rata, SOP 5(xiii)); writes CASEMGMT'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import trace_case_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.BUILD_HOLD_PLAN(P_CASE_ID VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Amount-specific hold plan for a traced case, with the whole-account freeze baseline (comparison only); re-checked by the verifier'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import build_hold_plan_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.CASE_SUMMARY(P_CASE_ID VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Compact case status (<= 2 KB) for the agent'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import case_summary_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.DRAFT(P_CASE_ID VARCHAR, P_KIND VARCHAR DEFAULT 'ALL')
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Draft case documents: templates + slots; Cortex AI_COMPLETE writes prose only; every draft verified'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import draft_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.VERIFY(P_DRAFT_ID VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Re-verify a draft: numbers, citations, policy, injection, format'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import verify_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.APPROVE(
    P_DRAFT_ID VARCHAR, P_DECISION VARCHAR, P_REASON_CODE VARCHAR, P_COMMENT VARCHAR, P_ACTOR VARCHAR DEFAULT NULL)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Maker-checker approval (supervisor only; checker != maker); stages ACTIONS and simulated LIENS'
  EXECUTE AS CALLER
AS
$$
from wapsi_sf.procs import approve_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.REQUEST_ACCOUNT_LEVEL_HOLD(
    P_CASE_ID VARCHAR, P_ACCOUNT_ID VARCHAR, P_REASONS VARCHAR, P_NOTE VARCHAR DEFAULT '', P_ACTOR VARCHAR DEFAULT NULL)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Officer request for an exceptional account-level hold (>= 1 recorded reason); applied only after a different supervisor approves it'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import request_account_level_hold_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.APPROVE_ACCOUNT_LEVEL(
    P_DRAFT_ID VARCHAR, P_REASON_CODE VARCHAR, P_COMMENT VARCHAR, P_APPROVER VARCHAR)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Ledger step of APPROVE for an account-level request: applies the exception, redrafts hold instruction and CFCFRMS update for fresh approval'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import approve_account_level_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.CHECK_LEA(P_CASE_ID VARCHAR, P_REQUEST VARCHAR, P_ACTOR VARCHAR DEFAULT NULL)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Police / LEA hold request vs the 8-field checklist; hold limited to the identifiable amount; clarification letter; never changes the plan'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import check_lea_proc as run
$$;

CREATE OR REPLACE PROCEDURE WAPSI.CASEMGMT.APPEND_AUDIT(
    P_CASE_ID VARCHAR, P_ACTION VARCHAR, P_OBJECT_TYPE VARCHAR, P_OBJECT_ID VARCHAR, P_DETAILS VARCHAR DEFAULT NULL)
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  PACKAGES = ('snowflake-snowpark-python', 'pydantic', 'pandas', 'numpy', 'jinja2', 'pyarrow')
  IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')
  HANDLER = 'run'
  COMMENT = 'Seal one audit event onto a case hash chain (the actions runner writes the audit trail only through this)'
  EXECUTE AS OWNER
AS
$$
from wapsi_sf.procs import append_audit_proc as run
$$;

SHOW PROCEDURES IN SCHEMA WAPSI.CASEMGMT;
