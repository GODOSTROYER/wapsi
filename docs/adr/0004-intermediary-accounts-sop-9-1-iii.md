# ADR 0004: Nodal, pool and escrow accounts: hold the disputed amount only (SOP Para 9.1(iii))

- **Status:** Accepted. Supersedes the earlier rule "never hold nodal, pool or escrow accounts"
  (`BLOCK_NODAL_POOL_ESCROW`), which is withdrawn.
- **Date:** 2026-09-16 (the correction date recorded in a build playbook that is now only in the git history; see
  Context and [originality](../originality.md))
- **Related:** [0001](0001-amount-specific-holds.md), [0008](0008-verifier-independent-of-planner.md)

## Context

Stolen money often passes through intermediary accounts: a payment aggregator's pool account, a nodal account or an
escrow account. These accounts hold many other people's money. In showcase case 1, mule B pays ₹20,000.00 as a
"wallet topup" into a payment-aggregator pool account (`HB01-9000000001`) that holds ₹5,00,20,000.00 in total.

**The first rule was wrong.** The pre-build plan (`WAPSI_BUILD_PLAN.md`, kept outside this repository) had a rule
"Never hold nodal, pool or escrow accounts", attributed to SOP Para 5(x), implemented as an account-type hard block
`BLOCK_NODAL_POOL_ESCROW` with a hold of 0 and a referral to the payment system operator / aggregator / gateway
process (the plan cites SOP Para 9.3 and 9.5 for that). Reading the SOP text in full on 2026-09-16 showed three
things:

- Para 9.1(i) excludes nodal, pool and escrow accounts from **forward-looking** holds on first-layer accounts
  `[SOP-9-1-I]`. It does not forbid holding the disputed amount.
- Para 9.1(iii) addresses these accounts directly: "where CFCFRMS notifies for action against a nodal, escrow, or
  pool account, the bank shall hold the disputed amount" `[SOP-9-1-III]`. The same paragraph says law enforcement
  agencies should normally refrain from suspending such accounts or seizing them.
- Para 5(x), the paragraph the old rule cited, says money held with banks at any layer can be released to the victim
  `[SOP-5-X]`. It supports holding, not blocking.

The correction overrides `WAPSI_BUILD_PLAN.md` §2 and §3.3; the corrected rule is in `AGENTS.md` convention 6. The
dated record of the correction is the build playbook's "Legal corrections applied on 2026-09-16" block, which was
removed from the tree on 2026-10-07 with the edition it belonged to and is still readable in the git history (commits
`b27a8de` to `13ee458`).

## Decision

1. **New disposition `HOLD_AMOUNT_INTERMEDIARY`** for home-bank accounts of type `NODAL`, `POOL` or `ESCROW`
   (`INTERMEDIARY_ACCOUNT_TYPES` in `src/wapsi/models.py`). It is a holding disposition: hold
   `min(tainted money now, balance)`, the disputed amount only.
2. **What never happens to these accounts:** no forward hold (SOP Para 9.1(i)), no suspension of digital banking or
   seizure (reason code `NO_SUSPENSION_OR_SEIZURE`), never an s.106 escalation flag.
3. **Citations on the decision:** `SOP-9-1-III`, `SOP-9-1`, `SOP-ANNEX-I`.
4. **Other-bank intermediaries** get an `OUTBOUND_CFCFRMS_REQUEST`: the other bank holds the amount. Tainted money
   in an other-bank pool counts as `outbound`; in a home-bank pool it counts as `intermediary`.
5. **Classification and tracing agree.** The classifier labels these accounts `INTERMEDIARY` by account type,
   before any score. The trace treats them as terminals that keep the victims' money while their balance covers
   it; any shortfall leaves pro-rata.
6. **The drafts say it.** The hold instruction and customer notice are addressed to the intermediary, and the hold
   instruction must state that the account is not suspended or seized.
7. **The legacy disposition is gone.** `Disposition` has no `BLOCK_NODAL_POOL_ESCROW` value, so a plan that
   carries it fails validation; the `lien-calculator` skill script also rejects it. `HOLD_AMOUNT_INTERMEDIARY` is reserved for intermediary accounts, and the verifier reads the account
   type from the trace, not from the plan.

## Consequences

**Showcase case 1:** the pool account `HB01-9000000001` has ₹19,200.00 of the victim's money (96% of the ₹20,000.00
top-up). Wapsi holds exactly ₹19,200.00 and leaves the rest of the ₹5,00,20,000.00 balance in use. The plan's
`total_hold` is ₹1,42,848.00 and `total_recoverable` ₹3,34,848.00, both including the ₹19,200.00. Under the
withdrawn rule, the plan would have held nothing at the pool account: `total_hold` ₹1,23,648.00 and
`total_recoverable` ₹3,15,648.00, with the ₹19,200.00 left to a separate process.

**Demo world:** the `T10_NODAL_POOL` typology has 3 cases. Its golden totals put ₹1,71,334 (whole rupees, as
printed in `data/golden/demo/SUMMARY.md`) in intermediary accounts, and 3 accounts get
`HOLD_AMOUNT_INTERMEDIARY`. In `evals/reports/latest.json`, role precision and recall for `INTERMEDIARY` are both
1.0 and every T10 hold matches the golden set.

Costs and trade-offs:

- **The operator carries a hold inside a shared account.** Wapsi does not model a payment aggregator's settlement
  cycle or check whether a hold of this size disturbs other merchants. That stays with the bank and the operator.
- **Several copies to keep in step.** The rule lives in the planner (`wapsi.graph.holdplan`), the independent verifier
  (`wapsi.verify.policy`), the reference implementation for the golden set (`wapsi.synth.reference`) and the
  `lien-calculator` skill script. The skill script still carries a comment about the migration from the legacy
  disposition.
- **One stale comment remains.** The docstring of `src/wapsi/classify/rules.py` still describes intermediary
  accounts with "(SOP Para 5(x): never held)". The code in that module only assigns the role; the hold decision is
  made in `holdplan.py`. The comment should be corrected in a code change.

**Lesson recorded.** The error came from a plan written before the legal quotes were checked. Every rule in code
now cites a registered id whose quote is checked word for word against the downloaded SOP
(`uv run python -m wapsi.search.check_citations`: 121/121 checks passed on 2026-09-29), and the verifier test
`test_hold_amount_disposition_on_pool_account` asserts that the error message cites `[SOP-9-1-III]` and not
`[SOP-5-X]`.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep "never hold" (`BLOCK_NODAL_POOL_ESCROW`) | Contradicts SOP Para 9.1(iii); leaves the victim's money in the pool unprotected. `tests/verify/test_verify_plan.py::test_pool_account_left_unheld` now fails such a plan |
| Treat the pool account like any other account (`HOLD_AMOUNT`) | Right amount, wrong treatment: it would allow a forward hold and an s.106 flag and would not carry the no-suspension rule. `test_hold_amount_disposition_on_pool_account` fails it |
| Freeze the pool account | Immobilises ₹5,00,20,000.00 of other people's money to protect ₹19,200.00, and stops every merchant settled through it |
| Add a forward hold on the pool account | Excluded by SOP Para 9.1(i); `test_forward_hold_on_pool_account` fails it |

## Evidence

- Code: `src/wapsi/graph/holdplan.py` (`REASON_NO_SUSPENSION`, `DISPOSITION_CITATIONS`), `src/wapsi/verify/policy.py`
  (`_check_intermediary`, `suspension_instructions`), `src/wapsi/models.py` (`Disposition.HOLD_AMOUNT_INTERMEDIARY`).
- Tests: `tests/verify/test_verify_plan.py` (`test_showcase_pool_account_holds_the_disputed_amount`,
  `test_hold_amount_disposition_on_pool_account`, `test_pool_type_is_taken_from_the_trace_not_the_plan`,
  `test_pool_account_over_hold`, `test_pool_account_left_unheld`, `test_forward_hold_on_pool_account`,
  `test_pool_account_never_flagged_for_escalation`, `test_other_bank_pool_account_is_an_outbound_request`);
  `tests/verify/test_verify_drafts.py` (`test_pool_hold_instruction_must_not_suspend_or_seize`,
  `test_pool_hold_instruction_must_state_the_intermediary_rule`).
- History: `AGENTS.md` convention 6 (the corrected rule), `.claude/skills/lien-calculator/scripts/hold_plan.py`
  (legacy check), and the build playbook's legal corrections block in the git history (commits `b27a8de` to
  `13ee458`).
- Citations: `SOP-9-1-III`, `SOP-9-1-I`, `SOP-5-X`, `SOP-9-1`, `SOP-ANNEX-I`.
