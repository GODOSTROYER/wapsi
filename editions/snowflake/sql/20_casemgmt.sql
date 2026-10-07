-- Wapsi on Snowflake · 20 · case management (CASEMGMT) and app (APP) tables, views, settings.
-- Run as WAPSI_ADMIN. CREATE TABLE IF NOT EXISTS only: case history is never dropped by a re-run (the SQL
-- guard hook blocks DROP / TRUNCATE of CASEMGMT tables and any UPDATE / DELETE of AUDIT_LOG, AUDIT_ANCHOR and
-- APPROVALS). Columns added after the first deploy come with ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so a
-- re-run upgrades an existing account in place.
-- Column lists must match editions/snowflake/snowpark/wapsi_sf/casestore.py COLUMNS (tested).

USE ROLE WAPSI_ADMIN;

-- One row per case (complaints sharing first-layer accounts). Flattened from CASE_BUNDLES.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.CASES (
  CASE_ID                  VARCHAR NOT NULL,
  WORLD                    VARCHAR,
  RAW_SCHEMA               VARCHAR,
  NCRP_IDS                 ARRAY,
  OPENED_AT                TIMESTAMP_NTZ,
  STATUS                   VARCHAR,
  ASSIGNED_TO              VARCHAR,
  PRIORITY_AMOUNT          NUMBER(18,2),
  RULE                     VARCHAR,
  STOLEN                   NUMBER(18,2),
  HOLDABLE                 NUMBER(18,2),
  CASHED_OUT               NUMBER(18,2),
  OUTBOUND                 NUMBER(18,2),
  INTERMEDIARY             NUMBER(18,2),
  MERCHANT_SETTLED         NUMBER(18,2),
  UNLOCATED                NUMBER(18,2),
  TOTAL_HOLD               NUMBER(18,2),
  TOTAL_FORWARD_HOLD       NUMBER(18,2),
  TOTAL_RECOVERABLE        NUMBER(18,2),
  OVER_HOLD_AMOUNT         NUMBER(18,2),
  BASELINE_FREEZE_AMOUNT   NUMBER(18,2),
  BASELINE_FREEZE_ACCOUNTS NUMBER,
  RESTORATION_PROCESS      VARCHAR,
  STR_GROUND               VARCHAR,
  N_ACCOUNTS               NUMBER,
  N_EDGES                  NUMBER,
  UPDATED_AT               TIMESTAMP_NTZ
);

-- Full CaseBundle JSON: the source of truth for the Python core.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.CASE_BUNDLES (
  CASE_ID    VARCHAR NOT NULL,
  BUNDLE     VARIANT,
  VERSION    NUMBER,
  UPDATED_AT TIMESTAMP_NTZ
);

-- Tainted transfers of the money trail (depth = layer of the receiving side).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.TRAIL_EDGES (
  CASE_ID        VARCHAR NOT NULL,
  TXN_ID         VARCHAR,
  TS             TIMESTAMP_NTZ,
  FROM_ACCOUNT   VARCHAR,
  TO_ACCOUNT     VARCHAR,
  FROM_BANK_ID   VARCHAR,
  TO_BANK_ID     VARCHAR,
  CHANNEL        VARCHAR,
  UTR            VARCHAR,
  AMOUNT         NUMBER(18,2),
  DEPTH          NUMBER,
  TAINTED        VARIANT,
  TAINTED_TOTAL  NUMBER(18,2),
  VIA_CONSORTIUM BOOLEAN
);

-- Every account on the trail: layer, balance and tainted money now (branch_city = holder city, a proxy).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.ACCOUNT_STATE (
  CASE_ID                VARCHAR NOT NULL,
  ACCOUNT_ID             VARCHAR,
  BANK_ID                VARCHAR,
  ACCOUNT_TYPE           VARCHAR,
  IS_HOME_BANK           BOOLEAN,
  LAYER                  NUMBER,
  FIRST_TAINTED_AT       TIMESTAMP_NTZ,
  BALANCE_NOW            NUMBER(18,2),
  TAINTED_NOW            VARIANT,
  TAINTED_NOW_TOTAL      NUMBER(18,2),
  TAINTED_RECEIVED_TOTAL NUMBER(18,2),
  TAINTED_SENT_TOTAL     NUMBER(18,2),
  TERMINAL               VARCHAR,
  PARENTS                ARRAY,
  BRANCH_CITY            VARCHAR
);

-- Role per trail account: MULE / INNOCENT_HOLDER (= bona fide recipient, SOP 5(xii)) / UNWITTING_CONDUIT /
-- MERCHANT / INTERMEDIARY / LEGIT / UNKNOWN.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.CLASSIFICATIONS (
  CASE_ID    VARCHAR NOT NULL,
  ACCOUNT_ID VARCHAR,
  ROLE       VARCHAR,
  SCORE      FLOAT,
  REASONS    ARRAY,
  MODEL      VARCHAR
);

-- Hold decisions: amount-specific by default (SOP 5(ix), 9.1). EXCEPTIONAL_ACCOUNT_LEVEL_HOLD appears only after
-- an officer's REQUEST_ACCOUNT_LEVEL_HOLD was approved by a different supervisor (reasons in REASON_CODES); its
-- HOLD_AMOUNT stays the amount-specific figure. Rebuild with CALL BUILD_HOLD_PLAN, never UPDATE by hand.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.HOLD_PLAN (
  CASE_ID             VARCHAR NOT NULL,
  ACCOUNT_ID          VARCHAR,
  BANK_ID             VARCHAR,
  ACCOUNT_TYPE        VARCHAR,
  BRANCH_CITY         VARCHAR,
  DISPOSITION         VARCHAR,
  HOLD_AMOUNT         NUMBER(18,2),
  FORWARD_HOLD_AMOUNT NUMBER(18,2),
  TAINTED_AMOUNT      NUMBER(18,2),
  BALANCE_NOW         NUMBER(18,2),
  LAYER               NUMBER,
  ROLE                VARCHAR,
  PER_VICTIM          VARIANT,
  CITATION_IDS        ARRAY,
  REASON_CODES        ARRAY,
  REQUIRES_HUMAN      BOOLEAN,
  NOTES               VARCHAR,
  CREATED_AT          TIMESTAMP_NTZ
);

-- Drafts (numbers are slots from case tables; the LLM writes prose only). KIND includes
-- ACCOUNT_LEVEL_HOLD_REQUEST (officer's exception request: STRUCTURED carries reasons and requested_by).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.DRAFTS (
  DRAFT_ID     VARCHAR NOT NULL,
  CASE_ID      VARCHAR NOT NULL,
  KIND         VARCHAR,
  ACCOUNT_ID   VARCHAR,
  TITLE        VARCHAR,
  BODY         VARCHAR,
  SLOTS        VARIANT,
  STRUCTURED   VARIANT,
  CITATION_IDS ARRAY,
  LLM_MODEL    VARCHAR,
  CREATED_AT   TIMESTAMP_NTZ,
  VERSION      NUMBER,
  CREATED_BY   VARCHAR,
  DRAFT_SHA256 VARCHAR
);

-- Verifier report per draft (numeric, citations, policy, injection, format).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.VERIFICATIONS (
  DRAFT_ID    VARCHAR NOT NULL,
  CASE_ID     VARCHAR NOT NULL,
  PASSED      BOOLEAN,
  CHECKS      VARIANT,
  VERIFIED_AT TIMESTAMP_NTZ
);

-- APPEND-ONLY maker-checker decisions.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.APPROVALS (
  APPROVAL_ID      VARCHAR NOT NULL,
  CASE_ID          VARCHAR NOT NULL,
  DRAFT_ID         VARCHAR,
  DECISION         VARCHAR,
  APPROVER         VARCHAR,
  APPROVER_ROLE    VARCHAR,
  REASON_CODE      VARCHAR,
  APPROVAL_COMMENT VARCHAR,
  DECIDED_AT       TIMESTAMP_NTZ,
  DRAFT_SHA256     VARCHAR
);

-- Outbound work staged for runners (hold instruction, CFCFRMS update, Jira, Slack). CFCFRMS_UPDATE rows carry
-- PAYLOAD:interface = 'SIMULATED_CFCFRMS': Wapsi is not connected to the CFCFRMS portal.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.ACTIONS (
  ACTION_ID   VARCHAR NOT NULL,
  CASE_ID     VARCHAR NOT NULL,
  KIND        VARCHAR,
  PAYLOAD     VARIANT,
  STATUS      VARCHAR,
  CREATED_AT  TIMESTAMP_NTZ,
  EXECUTED_AT TIMESTAMP_NTZ,
  RESULT      VARCHAR,
  ATTEMPTS    NUMBER DEFAULT 0,
  CLAIMED_AT  TIMESTAMP_NTZ,
  CLAIMED_BY  VARCHAR
);

-- SOP / PML clocks (STR due, 90-day hold lapse, forward-hold review, P1 notices, ...). SUBJECT is the identifier
-- only; NOTES says how the clock was counted (provisional start, lapse start point, forward-hold review).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.CLOCKS (
  CLOCK_ID      VARCHAR NOT NULL,
  CASE_ID       VARCHAR NOT NULL,
  KIND          VARCHAR,
  SUBJECT       VARCHAR,
  START_TS      TIMESTAMP_NTZ,
  DUE           TIMESTAMP_NTZ,
  WARN_AT       TIMESTAMP_NTZ,
  BUSINESS_DAYS BOOLEAN,
  STATUS        VARCHAR,
  CITATION_ID   VARCHAR,
  NOTES         VARCHAR
);

-- APPEND-ONLY, hash-chained audit trail (wapsi.audit): INSERT only, never UPDATE / DELETE. Each row is one link:
-- HASH = SHA-256 over canonical JSON of (seq, ts, actor, role, event_type, case_id, payload_digest, prev_hash);
-- PREV_HASH = the previous row's HASH (the genesis hash for SEQ 0). Rows are written by the Python case store only
-- (procedures, APPEND_AUDIT), so every event is sealed. Verify: CALL WAPSI.CASEMGMT.CASE_SUMMARY(...) -> audit_chain.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.AUDIT_LOG (
  EVENT_ID    VARCHAR NOT NULL,
  CASE_ID     VARCHAR NOT NULL,
  EVENT_AT    TIMESTAMP_NTZ,
  ACTOR       VARCHAR,
  ACTOR_ROLE  VARCHAR,
  ACTION      VARCHAR,
  OBJECT_TYPE VARCHAR,
  OBJECT_ID   VARCHAR,
  DETAILS     VARIANT,
  SEQ         NUMBER,
  PREV_HASH   VARCHAR,
  HASH        VARCHAR
);

-- APPEND-ONLY chain checkpoints (event count + head hash per case, one row per append; the latest = most events).
-- Outside the log, so events removed from the end and a fully re-hashed log are detected.
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.AUDIT_ANCHOR (
  CASE_ID     VARCHAR NOT NULL,
  EVENT_COUNT NUMBER,
  HEAD_HASH   VARCHAR,
  ANCHORED_AT TIMESTAMP_NTZ
);

-- Simulated core-banking lien register (written when a hold instruction is approved). SCOPE = AMOUNT (the
-- amount-specific lien) or ACCOUNT_LEVEL_EXCEPTION (the approved exception's restriction, amount 0: the amount
-- lien stays the recoverable money).
CREATE TABLE IF NOT EXISTS WAPSI.CASEMGMT.LIENS (
  LIEN_ID          VARCHAR NOT NULL,
  CASE_ID          VARCHAR NOT NULL,
  ACCOUNT_ID       VARCHAR,
  AMOUNT           NUMBER(18,2),
  PLACED_AT        TIMESTAMP_NTZ,
  RELEASED_AT      TIMESTAMP_NTZ,
  SOURCE_ACTION_ID VARCHAR,
  SCOPE            VARCHAR
);

-- In-place upgrade of an account deployed before the audit chain, clock notes and lien scope.
ALTER TABLE WAPSI.CASEMGMT.AUDIT_LOG ADD COLUMN IF NOT EXISTS SEQ NUMBER;
ALTER TABLE WAPSI.CASEMGMT.AUDIT_LOG ADD COLUMN IF NOT EXISTS PREV_HASH VARCHAR;
ALTER TABLE WAPSI.CASEMGMT.AUDIT_LOG ADD COLUMN IF NOT EXISTS HASH VARCHAR;
ALTER TABLE WAPSI.CASEMGMT.CLOCKS ADD COLUMN IF NOT EXISTS NOTES VARCHAR;
ALTER TABLE WAPSI.CASEMGMT.LIENS ADD COLUMN IF NOT EXISTS SCOPE VARCHAR;

-- ------------------------------------------------------------------------------------------ APP tables
CREATE TABLE IF NOT EXISTS WAPSI.APP.USER_ROLES (
  USER_NAME VARCHAR NOT NULL,
  APP_ROLE  VARCHAR NOT NULL
) COMMENT = 'Streamlit viewer -> app role (ANALYST / SUPERVISOR / AUDITOR); the app runs with owner''s rights';

CREATE TABLE IF NOT EXISTS WAPSI.APP.EVAL_RUNS (
  WORLD        VARCHAR,
  GENERATED_AT TIMESTAMP_TZ,
  CASES        NUMBER,
  REPORT       VARIANT,
  SOURCE       VARCHAR DEFAULT 'ci-harness'
) COMMENT = 'Golden-set evaluation reports (wapsi-eval, parity, Cortex runs)';

CREATE TABLE IF NOT EXISTS WAPSI.APP.SETTINGS (
  NAME  VARCHAR NOT NULL,
  VALUE VARCHAR
) COMMENT = 'Runtime knobs read by the procedures (model, ledger mode, guardrails)';

CREATE TABLE IF NOT EXISTS WAPSI.APP.DEMO_CLOCK (
  NOW_TS TIMESTAMP_NTZ
) COMMENT = 'Optional fixed "now" (UTC) for demo clock questions; empty = the real time';

MERGE INTO WAPSI.APP.SETTINGS s
USING (
  SELECT column1 AS name, column2 AS value FROM VALUES
    ('llm_model', 'claude-sonnet-5'),
    ('llm_mode', 'ai_complete'),
    ('guardrails', 'false'),
    ('ledger_mode', 'sql'),
    ('default_officer', 'nodal.officer@hb01'),
    ('search_service', 'WAPSI.DOCS.SOP_SEARCH')
) v ON s.name = v.name
WHEN NOT MATCHED THEN INSERT (name, value) VALUES (v.name, v.value);

-- Demo personas used by the shared UI's role switcher (synthetic identities, not real users).
MERGE INTO WAPSI.APP.USER_ROLES u
USING (
  SELECT column1 AS user_name, column2 AS app_role FROM VALUES
    ('nodal.officer@hb01', 'ANALYST'),
    ('supervisor@hb01', 'SUPERVISOR'),
    ('auditor@hb01', 'AUDITOR')
) v ON u.user_name = v.user_name
WHEN NOT MATCHED THEN INSERT (user_name, app_role) VALUES (v.user_name, v.app_role);

-- ------------------------------------------------------------------------------------------ views
-- Clocks with IST due dates and days-to-due (APP.DEMO_CLOCK overrides "now" for demos).
CREATE OR REPLACE VIEW WAPSI.CASEMGMT.V_CLOCKS
  COMMENT = 'Clocks with IST due date and days to due; keeps non-deterministic time out of the semantic view'
AS
SELECT c.clock_id,
       c.case_id,
       c.kind,
       c.subject,
       c.start_ts,
       c.due,
       DATE(CONVERT_TIMEZONE('UTC', 'Asia/Kolkata', c.due)) AS due_date_ist,
       DATEDIFF(day, COALESCE((SELECT MAX(now_ts) FROM WAPSI.APP.DEMO_CLOCK), SYSDATE()), c.due) AS days_to_due,
       c.status,
       c.citation_id,
       c.notes
FROM WAPSI.CASEMGMT.CLOCKS c;

-- Account -> branch city per case. PROXY: the data contract has no branch column; the holder's city is used.
CREATE OR REPLACE VIEW WAPSI.APP.V_ACCOUNT_BRANCH
  COMMENT = 'Trail account -> branch city (proxy: account holder city). No name, mobile or PAN.'
AS
SELECT case_id,
       account_id,
       bank_id,
       COALESCE(NULLIF(branch_city, ''), 'Unknown') AS branch_city
FROM WAPSI.CASEMGMT.ACCOUNT_STATE;

-- Police / LEA hold-request checks (CALL CHECK_LEA): the eight-field checklist result per request, from the
-- sealed audit trail. The hold is limited to the identifiable amount; the check never changes the plan.
CREATE OR REPLACE VIEW WAPSI.CASEMGMT.V_LEA_CHECKS
  COMMENT = 'LEA hold-request checks: complete?, missing fields, requested vs identifiable amount (from AUDIT_LOG)'
AS
SELECT a.case_id,
       a.object_id AS request_id,
       a.event_at AS checked_at,
       a.actor AS checked_by,
       a.details:complete::BOOLEAN AS complete,
       a.details:missing AS missing_fields,
       a.details:account_id::VARCHAR AS account_id,
       a.details:requested_amount::NUMBER(18,2) AS requested_amount,
       a.details:identifiable_amount::NUMBER(18,2) AS identifiable_amount,
       a.details:hold_limited::BOOLEAN AS hold_limited,
       a.details:asks_account_level::BOOLEAN AS asks_account_level
FROM WAPSI.CASEMGMT.AUDIT_LOG a
WHERE a.action = 'lea_request_checked';

SHOW TABLES IN SCHEMA WAPSI.CASEMGMT;
