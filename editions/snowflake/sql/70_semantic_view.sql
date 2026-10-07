-- Wapsi on Snowflake · 70 · semantic view WAPSI.APP.WAPSI_FRAUD_SV (run as WAPSI_ADMIN).
-- Source: editions/snowflake/templates/semantic_view_metrics.md (logical tables, dimensions, facts, metrics).
-- This is the deployable base. The CoCo session C2 (COCO_SPRINT.md) refines it with CoCo's semantic-view skill:
-- the six verified queries, custom Analyst instructions, the derived hold_vs_freeze_ratio (held vs the
-- whole-account freeze baseline), and Cortex Analyst
-- validation; it then writes the final DDL (GET_DDL) back to this file.
-- No PII and no ground truth: nothing here reads RAW customers / complaints or WAPSI.ML.

USE ROLE WAPSI_ADMIN;

CREATE OR REPLACE SEMANTIC VIEW WAPSI.APP.WAPSI_FRAUD_SV
  TABLES (
    cases AS WAPSI.CASEMGMT.CASES
      PRIMARY KEY (case_id)
      WITH SYNONYMS ('case', 'ring', 'complaint group')
      COMMENT = 'One case = complaints whose money reached the same first-layer accounts',
    holds AS WAPSI.CASEMGMT.HOLD_PLAN
      PRIMARY KEY (case_id, account_id)
      WITH SYNONYMS ('hold plan', 'lien', 'disposition')
      COMMENT = 'Hold decision per trail account: amount-specific by default; account-level only as a supervisor-approved exception',
    edges AS WAPSI.CASEMGMT.TRAIL_EDGES
      PRIMARY KEY (case_id, txn_id)
      WITH SYNONYMS ('money trail', 'transfers', 'hops')
      COMMENT = 'Tainted transfers on the money trail',
    clocks AS WAPSI.CASEMGMT.V_CLOCKS
      PRIMARY KEY (clock_id)
      WITH SYNONYMS ('deadline', 'SLA', 'timer')
      COMMENT = 'SOP / PML clocks with IST due dates',
    account_branch AS WAPSI.APP.V_ACCOUNT_BRANCH
      PRIMARY KEY (case_id, account_id)
      WITH SYNONYMS ('branch', 'account')
      COMMENT = 'Trail account to branch city (proxy: account holder city)'
  )
  RELATIONSHIPS (
    holds_to_cases AS holds (case_id) REFERENCES cases,
    holds_to_branch AS holds (case_id, account_id) REFERENCES account_branch,
    edges_to_cases AS edges (case_id) REFERENCES cases,
    clocks_to_cases AS clocks (case_id) REFERENCES cases,
    clocks_to_branch AS clocks (case_id, subject) REFERENCES account_branch
  )
  FACTS (
    holds.hold_amt AS holds.hold_amount COMMENT = 'Rupees held in this account (amount-specific)',
    holds.tainted_amt AS holds.tainted_amount COMMENT = 'Tainted rupees attributed to this account',
    holds.forward_amt AS holds.forward_hold_amount COMMENT = 'First-layer forward hold (not money in hand)',
    edges.edge_tainted_amt AS edges.tainted_total COMMENT = 'Tainted rupees carried by this transfer',
    cases.baseline_amt AS cases.baseline_freeze_amount COMMENT = 'Whole-account freeze baseline (comparison only, never a recommendation)'
  )
  DIMENSIONS (
    cases.case_ref AS cases.case_id WITH SYNONYMS = ('ring', 'case number') COMMENT = 'Case id (CASE-<NCRP id>)',
    cases.case_status AS cases.status COMMENT = 'NEW, TRACED, PLANNED, DRAFTED, VERIFIED, APPROVED, CLOSED; open = not CLOSED',
    cases.officer AS cases.assigned_to WITH SYNONYMS = ('nodal officer', 'analyst', 'owner') COMMENT = 'Assigned nodal officer',
    cases.process_type AS cases.restoration_process COMMENT = 'P1 single victim, P2 multiple victims (SOP Para 11)',
    cases.world_name AS cases.world COMMENT = 'Synthetic world: showcase or demo',
    cases.opened_date AS DATE(CONVERT_TIMEZONE('UTC', 'Asia/Kolkata', cases.opened_at)) COMMENT = 'Case opened (IST date)',
    holds.disposition_type AS holds.disposition
      COMMENT = 'HOLD_AMOUNT, HOLD_AMOUNT_NO_ESCALATION, HOLD_AMOUNT_INTERMEDIARY (nodal/pool/escrow, SOP 9.1(iii)), NO_HOLD_UPDATE_ONLY, HOLD_IN_POOL, OUTBOUND_CFCFRMS_REQUEST, FORWARD_HOLD, EXCEPTIONAL_ACCOUNT_LEVEL_HOLD (officer request + supervisor approval only)',
    holds.hold_layer AS holds.layer COMMENT = 'Layer 1 = first-layer account (SOP definition)',
    holds.holder_role AS holds.role COMMENT = 'MULE, INNOCENT_HOLDER (bona fide recipient, SOP 5(xii)), UNWITTING_CONDUIT, MERCHANT, INTERMEDIARY, LEGIT, UNKNOWN',
    holds.holding_bank AS holds.bank_id COMMENT = 'Bank of the account',
    holds.needs_human AS holds.requires_human COMMENT = 'Human review needed (s.106 candidate flag, unwitting conduit, account-level exception)',
    holds.hold_account AS holds.account_id COMMENT = 'Account number',
    account_branch.branch AS account_branch.branch_city WITH SYNONYMS = ('branch city', 'city') COMMENT = 'Branch city (proxy: holder city)',
    edges.channel_type AS edges.channel COMMENT = 'UPI, IMPS, NEFT, RTGS, ATM, CRYPTO, POS',
    edges.hop_depth AS edges.depth COMMENT = 'Layer of the receiving side',
    edges.transfer_date AS DATE(CONVERT_TIMEZONE('UTC', 'Asia/Kolkata', edges.ts)) COMMENT = 'Transfer date (IST)',
    clocks.clock_kind AS clocks.kind COMMENT = 'STR_DUE, HOLD_LAPSE, HOLD_LAPSE_WARNING, FORWARD_HOLD_REVIEW, GRIEVANCE_FILING, IO_DECISION, P1_NOTICE, ...',
    clocks.clock_notes AS clocks.notes COMMENT = 'How the clock was counted (provisional start, lapse start point, forward-hold review)',
    clocks.clock_status AS clocks.status COMMENT = 'OPEN, WARNING, OVERDUE, DONE',
    clocks.due_date AS clocks.due_date_ist COMMENT = 'Due date (IST)',
    clocks.days_left AS clocks.days_to_due COMMENT = 'Days until due (negative = overdue)'
  )
  METRICS (
    holds.held_amount AS SUM(holds.hold_amount)
      WITH SYNONYMS = ('amount on hold', 'held') COMMENT = 'Rupees on hold now (amount-specific)',
    holds.recoverable_amount AS SUM(IFF(holds.disposition = 'OUTBOUND_CFCFRMS_REQUEST', holds.tainted_amount, holds.hold_amount))
      COMMENT = 'Holds plus outbound CFCFRMS request amounts; forward holds excluded',
    holds.over_hold_amount AS SUM(GREATEST(holds.hold_amount - holds.tainted_amount, 0))
      COMMENT = 'Held beyond tainted money; must be 0',
    holds.forward_hold_total AS SUM(holds.forward_hold_amount)
      COMMENT = 'First-layer forward holds (not money in hand)',
    holds.accounts_held AS COUNT_IF(holds.hold_amount > 0) COMMENT = 'Accounts with a hold',
    holds.review_flags AS COUNT_IF(holds.requires_human) COMMENT = 'Decisions that need a human (s.106 candidates etc.)',
    holds.account_level_exceptions AS COUNT_IF(holds.disposition = 'EXCEPTIONAL_ACCOUNT_LEVEL_HOLD')
      COMMENT = 'Supervisor-approved account-level exceptions (recorded reasons, maker-checker)',
    holds.str_recommended_conduits AS COUNT_IF(ARRAY_CONTAINS('STR_RECOMMENDED_OFFICER_DECISION'::VARIANT, holds.reason_codes))
      COMMENT = 'Unwitting conduits: STR recommended, the officer decides (no automatic STR)',
    cases.cases_open AS COUNT_IF(cases.status <> 'CLOSED') COMMENT = 'Open cases',
    cases.stolen_amount AS SUM(cases.stolen) COMMENT = 'Reported victim transfers',
    cases.freeze_baseline_amount AS SUM(cases.baseline_freeze_amount)
      COMMENT = 'Whole-account freeze baseline of the touched accounts (comparison only)',
    clocks.lapsing_holds_15d AS COUNT_IF(clocks.kind = 'HOLD_LAPSE' AND clocks.status IN ('OPEN', 'WARNING') AND clocks.days_to_due BETWEEN 0 AND 15)
      COMMENT = '90-day hold lapses within 15 days [SOP-10-1]',
    clocks.str_due_3d AS COUNT_IF(clocks.kind = 'STR_DUE' AND clocks.status IN ('OPEN', 'WARNING') AND clocks.days_to_due BETWEEN 0 AND 3)
      COMMENT = 'STRs due within 3 days [PMLR-8]',
    clocks.forward_reviews_15d AS COUNT_IF(clocks.kind = 'FORWARD_HOLD_REVIEW' AND clocks.status IN ('OPEN', 'WARNING') AND clocks.days_to_due BETWEEN 0 AND 15)
      COMMENT = 'Forward holds due for their 90-day review within 15 days [SOP-9-1-I]',
    edges.cashed_out_amount AS SUM(IFF(edges.channel IN ('ATM', 'CRYPTO') AND edges.to_account IS NULL, edges.tainted_total, 0))
      COMMENT = 'Tainted money that left through ATM or crypto exits'
  )
  COMMENT = 'Wapsi fraud operations: cases, amount-specific holds, money trail, clocks, branches (synthetic data). Amounts are Indian rupees; timestamps are UTC, dates shown in IST. Recoverable = holds + outbound CFCFRMS requests. Holds are amount-specific; an account-level hold exists only as a supervisor-approved exception; the whole-account freeze baseline is a comparison figure only.';

SHOW SEMANTIC VIEWS IN SCHEMA WAPSI.APP;
