# ADR 0009: Account-level holds only as a recorded, supervisor-approved exception

- **Status:** Accepted; amends [0001](0001-amount-specific-holds.md)
- **Date:** 2026-09-28
- **Related:** [0001](0001-amount-specific-holds.md) (amount-specific holds by default),
  [0004](0004-intermediary-accounts-sop-9-1-iii.md) (nodal, pool and escrow accounts),
  [0008](0008-verifier-independent-of-planner.md) (the verifier re-checks the rule)

## Context

[ADR 0001](0001-amount-specific-holds.md) made the tainted amount the unit of every hold. Its first wording ("never
the whole account") was stronger than the law it rests on:

- SOP Para 5(ix) discourages *unwarranted* freezing and asks for accountability: "Unwarranted orders for freezing
  accounts shall be discouraged, and accountability measures shall be established." `[SOP-5-IX]` It does not
  forbid account-level action where it is warranted.
- The RBI draft SOP on suspected money-mule accounts (draft KYC Amendment Directions, 2026, Annex III, issued
  11 Sep 2026) is a **DRAFT**: comments close on 2 Oct 2026 and it is proposed to take effect on 1 Apr 2027. It
  keeps a hold on a whole suspected-mule account, "as a last resort and only in exceptional circumstances"
  (radar row `RR-06`, `data/docs/reg_radar.json`). Wapsi never treats it as in force, but a nodal officer needs a
  lawful way to act when an account is wholly controlled by the fraudster.
- A police order can itself specify account-level action, and a bank must be able to record that it acted on it.

Two failure modes had to be avoided: an absolute rule that leaves the officer no recorded route for a warranted
exception, and a silent route that brings the whole-account freeze back.

## Decision

Holds stay **amount-specific by default**. Account-level action exists only as a **recorded, supervisor-approved
exception** (`AGENTS.md`, convention 6):

1. **Never automatic.** `build_hold_plan` never emits `Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`. The
   whole-account freeze baseline (`HoldPlan.baseline_freeze_amount`) stays a comparison figure, never a
   recommendation.
2. **The officer asks, with reasons from a closed list.** `wapsi.pipeline.run.request_account_level_hold` needs at
   least one `AccountLevelReason`: `FRAUDSTER_CONTROLLED_ACCOUNT`, `CONTINUING_FRAUD_EVIDENCE`,
   `LEA_ORDER_ACCOUNT_LEVEL` or `IDENTITY_THEFT_ACCOUNT`. Anything else raises `ValueError`; an auditor cannot
   ask. The request is an `ACCOUNT_LEVEL_HOLD_REQUEST` draft, verified like any other.
3. **Eligible accounts only.** A home-bank account on the trail whose decision is `HOLD_AMOUNT` or
   `HOLD_AMOUNT_NO_ESCALATION`. Never nodal, pool or escrow accounts (SOP 9.1(iii)), merchants (SOP 9.2),
   other banks' accounts or victim accounts (`wapsi.graph.holdplan.account_level_block`).
4. **A different supervisor approves.** `approve` binds the approval to the request's SHA-256 fingerprint; the
   requester cannot approve their own request (`PermissionError`).
5. **The money figures do not change.** `apply_account_level_hold` sets the disposition to
   `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`, adds the recorded reasons plus `OFFICER_REQUESTED_EXCEPTION` and
   `SUPERVISOR_APPROVED_EXCEPTION` to the reason codes, sets `requires_human`, and cites `SOP-5-IX` and `SOP-9-1`.
   `hold_amount`, per-victim shares and plan totals stay the amount-specific figures: the recoverable money.
6. **The paperwork follows.** With a ledger, the account's hold instruction and the case's CFCFRMS update are
   redrafted as new versions that need a fresh approval, and the account's unapproved customer notice and EDD memo
   are withdrawn. Every step is an audit event (`account_level_hold_requested`, `account_level_hold_applied`,
   `draft_withdrawn`) in the case's hash-chained log.
7. **The verifier checks it independently.** `wapsi.verify.policy` fails a plan or draft with an account-level
   hold that lacks recorded reasons (`account_level_without_reasons`), a valid approval
   (`account_level_unapproved`, `account_level_no_maker_checker`), matching reasons
   (`account_level_reason_mismatch`) or `requires_human` (`account_level_without_human`), or that sits on an
   other-bank or merchant account. It still fails any draft that instructs a whole-account or debit freeze,
   unless it is the hold instruction of such an approved exception.

## Consequences

- The absolute wording is gone from the product and the docs: "amount-specific by default; account-level only as
  a recorded, supervisor-approved exception".
- **Nothing changes for a plan without a request.** The showcase, demo and hard golden sets have no exception, and
  their numbers are unchanged. In showcase case 1, an approved exception on mule A leaves A's hold at ₹43,200.00
  and the plan total at ₹1,42,848.00 (`tests/pipeline/test_pipeline_account_level.py`).
- **Accountability is built in.** Who asked, on which recorded reason, who approved, and which drafts were
  redrafted or withdrawn are all in the audit chain, which is what SOP Para 5(ix) asks for.
- **A police request for whole-account action is not an exception by itself.** `wapsi.verify.lea_request`
  answers it with the identifiable amount and a clarification letter; the officer can then raise an exception
  with reason `LEA_ORDER_ACCOUNT_LEVEL` if the order specifies account-level action.
- **Cost:** one more disposition, one more draft kind and one more verifier path to maintain, and every UI view
  that shows dispositions must show the exception and its reasons (the Hold Plan screen does).

## Alternatives considered

| Option | Why not |
|---|---|
| Keep "never the whole account" | Stricter than SOP Para 5(ix) and the RBI DRAFT; leaves the officer no recorded route when an account is wholly fraudster-controlled |
| Let the classifier or planner propose account-level holds for high-scoring mules | Brings back automatic freezing on a model score; classifier errors ([ADR 0006](0006-transparent-heuristic-classifier-default.md)) would then lock innocent holders' whole balances |
| Free-text justification instead of a closed reason list | Cannot be checked by the verifier or counted by an auditor; a closed list can |
| Replace the amount hold with the account-level restriction | Loses the per-victim figures that restoration (Process 1 / 2) and the recoverable total depend on |

## Evidence

- Code: `src/wapsi/graph/holdplan.py` (`account_level_reasons`, `account_level_block`, `apply_account_level_hold`),
  `src/wapsi/pipeline/run.py` (`request_account_level_hold`, `approve`), `src/wapsi/draft/drafter.py`
  (`draft_account_level_request`), `src/wapsi/draft/templates/account_level_request.j2`,
  `src/wapsi/verify/policy.py` (`_check_account_level`, `_check_account_level_request`).
- Tests: `tests/pipeline/test_pipeline_account_level.py`.
- UI: Hold Plan, "Exceptional account-level hold" panel; the request is approved in Approvals.
- Radar: `RR-06` in `data/docs/reg_radar.json` (the RBI DRAFT's last-resort wording against SOP 5(ix)).
