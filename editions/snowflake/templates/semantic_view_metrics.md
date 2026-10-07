# WAPSI_FRAUD_SV — metric, dimension and verified-query definitions

Reference — to be completed and validated in a CoCo session (S10). Syntax source:
[CREATE SEMANTIC VIEW](https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view) and
[semantic views with SQL](https://docs.snowflake.com/en/user-guide/views-semantic/sql). Object:
`WAPSI.APP.WAPSI_FRAUD_SV`. Clause order matters: TABLES, RELATIONSHIPS, FACTS, DIMENSIONS, METRICS, COMMENT,
AI_SQL_GENERATION, AI_QUESTION_CATEGORIZATION, AI_VERIFIED_QUERIES. Dimensions and facts reference a logical table
alias; metrics must aggregate. Logical names below deliberately differ from physical column names.

## Supporting views (create first; no PII, no ground truth)
| View | Columns | Notes |
|---|---|---|
| `WAPSI.APP.DEMO_CLOCK` (table) | `now_ts TIMESTAMP_NTZ` (one row, nullable) | Lets the finale evaluate "next 15 days" against a fixed date. Empty → `CURRENT_TIMESTAMP()`. Say so on screen if used. |
| `WAPSI.CASEMGMT.V_CLOCKS` | `clock_id` (PK), `case_id`, `kind`, `subject`, `start_ts`, `due`, `due_date_ist` (`DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', due))`), `days_to_due` (`DATEDIFF(day, COALESCE((SELECT MAX(now_ts) FROM APP.DEMO_CLOCK), CURRENT_TIMESTAMP()), due)`), `status`, `citation_id`, `notes` | Keeps non-deterministic time out of the semantic view. `notes` says how the clock was counted. |
| `WAPSI.APP.V_ACCOUNT_BRANCH` | `account_id` (PK), `bank_id`, `branch_city` | `branch_city` = account holder's city from `RAW.CUSTOMERS.CITY` (**proxy**: the data contract has no branch column — see open question). No name / mobile / PAN. |

## Logical tables
| Alias | Physical | Primary key | Synonyms |
|---|---|---|---|
| `cases` | `WAPSI.CASEMGMT.CASES` | `case_id` | case, ring, complaint group |
| `holds` | `WAPSI.CASEMGMT.HOLD_PLAN` | `case_id, account_id` | hold plan, lien, disposition |
| `edges` | `WAPSI.CASEMGMT.TRAIL_EDGES` | `case_id, txn_id` | money trail, transfers, hops |
| `clocks` | `WAPSI.CASEMGMT.V_CLOCKS` | `clock_id` | deadline, SLA, timer |
| `account_branch` | `WAPSI.APP.V_ACCOUNT_BRANCH` | `account_id` | branch, account |

Relationships: `holds (case_id) REFERENCES cases`, `holds (account_id) REFERENCES account_branch`,
`edges (case_id) REFERENCES cases`, `clocks (case_id) REFERENCES cases`,
`clocks (subject) REFERENCES account_branch` (only hold clocks carry an account id in `subject`; verify that
non-matching subjects are tolerated).

"Ring" is presented as the **case** (complaints that share first-layer accounts). Never use
`WAPSI.ML.LABELS_*.ring_id` — that is ground truth and must not reach the product.

## Dimensions
| Name | Expression | Synonyms / comment |
|---|---|---|
| `cases.case_ref` | `cases.case_id` | ring, case number |
| `cases.case_status` | `cases.status` | NEW … CLOSED; "open" = not CLOSED |
| `cases.officer` | `cases.assigned_to` | nodal officer, analyst, owner |
| `cases.process_type` | `cases.restoration_process` | P1 single victim, P2 multiple victims |
| `cases.opened_date` | `DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', cases.opened_at))` | IST date |
| `holds.disposition_type` | `holds.disposition` | HOLD_AMOUNT, HOLD_AMOUNT_NO_ESCALATION, HOLD_AMOUNT_INTERMEDIARY (nodal/pool/escrow, SOP 9.1(iii)), NO_HOLD_UPDATE_ONLY, HOLD_IN_POOL, OUTBOUND_CFCFRMS_REQUEST, FORWARD_HOLD, EXCEPTIONAL_ACCOUNT_LEVEL_HOLD (officer request + supervisor approval only) |
| `holds.hold_layer` | `holds.layer` | layer 1 = first-layer account (SOP definition) |
| `holds.holder_role` | `holds.role` | MULE, INNOCENT_HOLDER (bona fide recipient, SOP 5(xii)), UNWITTING_CONDUIT, MERCHANT, INTERMEDIARY, LEGIT, UNKNOWN |
| `holds.holding_bank` | `holds.bank_id` | bank |
| `holds.needs_human` | `holds.requires_human` | s.106 escalation candidate, unwitting conduit, account-level exception |
| `account_branch.branch` | `account_branch.branch_city` | branch, city (proxy) |
| `edges.channel_type` | `edges.channel` | UPI, IMPS, NEFT, RTGS, ATM, CRYPTO, POS |
| `edges.hop_depth` | `edges.depth` | layer of the receiving side |
| `edges.transfer_date` | `DATE(CONVERT_TIMEZONE('UTC','Asia/Kolkata', edges.ts))` | IST |
| `clocks.clock_kind` | `clocks.kind` | STR_DUE, HOLD_LAPSE, HOLD_LAPSE_WARNING, FORWARD_HOLD_REVIEW, GRIEVANCE_FILING, IO_DECISION, … |
| `clocks.clock_notes` | `clocks.notes` | how the clock was counted (provisional start, lapse start point, forward-hold review) |
| `clocks.clock_status` | `clocks.status` | OPEN, WARNING, OVERDUE, DONE |
| `clocks.due_date` | `clocks.due_date_ist` | IST |
| `clocks.days_left` | `clocks.days_to_due` | label `FILTER` (`LABELS = ( FILTER )`) |

## Facts (row-level, used by metrics)
`holds.hold_amt` = `holds.hold_amount`; `holds.tainted_amt` = `holds.tainted_amount`;
`holds.forward_amt` = `holds.forward_hold_amount`; `edges.tainted_amt` = `edges.tainted_total`;
`cases.baseline_amt` = `cases.baseline_freeze_amount`.

## Metrics (plan §7.1 plus supporting ones)
| Metric | Expression | Meaning |
|---|---|---|
| `holds.held_amount` | `SUM(holds.hold_amount)` | ₹ on hold now (amount-specific) |
| `holds.recoverable_amount` | `SUM(IFF(holds.disposition = 'OUTBOUND_CFCFRMS_REQUEST', holds.tainted_amount, holds.hold_amount))` | holds + outbound requests (AGENTS.md definition; forward holds excluded) |
| `holds.over_hold_amount` | `SUM(GREATEST(holds.hold_amount - holds.tainted_amount, 0))` | must be ₹0 |
| `holds.forward_hold_total` | `SUM(holds.forward_hold_amount)` | first-layer only; not money in hand |
| `holds.accounts_held` | `COUNT_IF(holds.hold_amount > 0)` | |
| `holds.review_flags` | `COUNT_IF(holds.requires_human)` | s.106 candidates and other reviews |
| `holds.account_level_exceptions` | `COUNT_IF(holds.disposition = 'EXCEPTIONAL_ACCOUNT_LEVEL_HOLD')` | supervisor-approved exceptions only |
| `holds.str_recommended_conduits` | `COUNT_IF(ARRAY_CONTAINS('STR_RECOMMENDED_OFFICER_DECISION'::VARIANT, holds.reason_codes))` | unwitting conduits: STR recommended, the officer decides |
| `cases.cases_open` | `COUNT_IF(cases.status <> 'CLOSED')` | |
| `cases.stolen_amount` | `SUM(cases.stolen)` | reported victim transfers |
| `cases.freeze_baseline_amount` | `SUM(cases.baseline_freeze_amount)` | whole-account freeze baseline (comparison only) |
| `clocks.lapsing_holds_15d` | `COUNT_IF(clocks.kind = 'HOLD_LAPSE' AND clocks.status IN ('OPEN','WARNING') AND clocks.days_to_due BETWEEN 0 AND 15)` | 90-day lapse approaching [SOP-10-1] |
| `clocks.str_due_3d` | `COUNT_IF(clocks.kind = 'STR_DUE' AND clocks.status IN ('OPEN','WARNING') AND clocks.days_to_due BETWEEN 0 AND 3)` | STR within 7 working days [PMLR-8] |
| `clocks.forward_reviews_15d` | `COUNT_IF(clocks.kind = 'FORWARD_HOLD_REVIEW' AND clocks.status IN ('OPEN','WARNING') AND clocks.days_to_due BETWEEN 0 AND 15)` | forward holds due for their 90-day review [SOP-9-1-I] |
| `edges.cashed_out_amount` | `SUM(IFF(edges.channel IN ('ATM','CRYPTO') AND edges.to_account IS NULL, edges.tainted_total, 0))` | ATM / crypto exits |
| `hold_vs_freeze_ratio` (derived, no alias) | `holds.held_amount / NULLIF(cases.freeze_baseline_amount, 0)` | held vs the whole-account freeze baseline (comparison only); lower is better |

Suggested `AI_SQL_GENERATION` text: "Amounts are Indian rupees (NUMBER(18,2)). Timestamps are stored in UTC; show
dates in IST. 'Open' means case_status <> 'CLOSED'. Recoverable = holds plus outbound CFCFRMS requests; forward
holds are not recoverable money. Holds are amount-specific; an account-level hold exists only as a
supervisor-approved exception; the whole-account freeze baseline is a comparison figure only. Ring means case."

## Verified queries (6) — expected SQL shape
Write each in `AI_VERIFIED_QUERIES ( name AS ( QUESTION '…' ONBOARDING_QUESTION TRUE SQL '…' ) )` against the
logical tables (the docs' example uses logical table names). Validate each by asking Cortex Analyst the question
and comparing results with the raw SQL on the physical tables.

1. `branches_lapsing_15d` — "Which branches have holds lapsing in the next 15 days?" (finale demo question)
   ```sql
   SELECT b.branch_city, COUNT(DISTINCT cl.clock_id) AS holds_lapsing, MIN(cl.due_date_ist) AS first_lapse_date
   FROM clocks AS cl JOIN account_branch AS b ON cl.subject = b.account_id
   WHERE cl.kind = 'HOLD_LAPSE' AND cl.status IN ('OPEN','WARNING') AND cl.days_to_due BETWEEN 0 AND 15
   GROUP BY b.branch_city ORDER BY holds_lapsing DESC, first_lapse_date
   ```
2. `recoverable_by_disposition` — "How much money is recoverable across open cases, by disposition?"
   ```sql
   SELECT h.disposition, SUM(IFF(h.disposition = 'OUTBOUND_CFCFRMS_REQUEST', h.tainted_amount, h.hold_amount)) AS recoverable_inr,
          COUNT(*) AS accounts
   FROM holds AS h JOIN cases AS c ON h.case_id = c.case_id
   WHERE c.status <> 'CLOSED' GROUP BY h.disposition ORDER BY recoverable_inr DESC
   ```
3. `hold_vs_freeze` — "How much are we holding compared with the whole-account freeze baseline?"
   ```sql
   SELECT SUM(c.total_hold) AS held_inr, SUM(c.baseline_freeze_amount) AS freeze_baseline_inr,
          SUM(c.baseline_freeze_accounts) AS baseline_accounts, SUM(c.over_hold_amount) AS over_hold_inr
   FROM cases AS c WHERE c.status <> 'CLOSED'
   ```
4. `str_due_3d` — "Which STRs are due in the next 3 days and who owns them?"
   ```sql
   SELECT cl.case_id, c.assigned_to AS officer, cl.due_date_ist, cl.days_to_due
   FROM clocks AS cl JOIN cases AS c ON cl.case_id = c.case_id
   WHERE cl.kind = 'STR_DUE' AND cl.status IN ('OPEN','WARNING') AND cl.days_to_due BETWEEN 0 AND 3
   ORDER BY cl.days_to_due, cl.case_id
   ```
5. `cashed_out_by_case` — "How much stolen money was cashed out at ATMs or crypto exchanges, by case?"
   ```sql
   SELECT e.case_id, e.channel, SUM(e.tainted_total) AS cashed_out_inr, COUNT(*) AS exits
   FROM edges AS e WHERE e.channel IN ('ATM','CRYPTO') AND e.to_account IS NULL
   GROUP BY e.case_id, e.channel ORDER BY cashed_out_inr DESC
   ```
6. `officer_workload` — "Which officers have the most open cases and how much is held for each?"
   ```sql
   SELECT c.assigned_to AS officer, COUNT(DISTINCT c.case_id) AS open_cases, SUM(c.total_hold) AS held_inr,
          SUM(c.total_recoverable) AS recoverable_inr
   FROM cases AS c WHERE c.status <> 'CLOSED'
   GROUP BY c.assigned_to ORDER BY open_cases DESC, held_inr DESC
   ```

## Acceptance
```sql
SHOW SEMANTIC VIEWS IN SCHEMA WAPSI.APP;
DESCRIBE SEMANTIC VIEW WAPSI.APP.WAPSI_FRAUD_SV;
SELECT * FROM SEMANTIC_VIEW(WAPSI.APP.WAPSI_FRAUD_SV
  METRICS holds.held_amount, holds.recoverable_amount, holds.over_hold_amount
  DIMENSIONS holds.disposition_type);
-- over_hold_amount must be 0 in every row; held_amount must equal SUM(hold_amount) from CASEMGMT.HOLD_PLAN
```
