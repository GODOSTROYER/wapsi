# ADR 0008: The verifier re-implements the rules instead of trusting the planner

- **Status:** Accepted
- **Date:** 2026-09-16
- **Related:** [0001](0001-amount-specific-holds.md), [0003](0003-numbers-from-data-words-from-llm.md),
  [0004](0004-intermediary-accounts-sop-9-1-iii.md)

## Context

The hold planner (`src/wapsi/graph/holdplan.py`) and the drafter (`src/wapsi/draft/drafter.py`) are the most
intricate parts of Wapsi. A bug in either would produce numbers that are wrong but consistent: the plan, the drafts
and the UI would all agree with each other. A check that calls the same functions would agree too.

The rules being protected are legal constraints, not preferences: never hold more than the tainted money
(SOP Para 5(ix), 9.1), account-level action only as a recorded, supervisor-approved exception
([ADR 0009](0009-account-level-hold-as-recorded-exception.md)), forward holds only on first-layer accounts (Para 9.1(i)), intermediary accounts held for the
disputed amount only (Para 9.1(iii)), no hold on a merchant whose goods were delivered (Para 9.2), innocent holders
never named in the automatic STR (Para 5(xii)), s.106 flags only with an FIR and prior reports. And the drafts contain LLM
prose ([ADR 0003](0003-numbers-from-data-words-from-llm.md)), which must be checked by something the LLM cannot talk to.

## Decision

1. **Separate code.** `wapsi.verify` does not import `wapsi.graph`, `wapsi.classify` or `wapsi.draft`. It imports
   only the shared contracts and helpers: `wapsi.models`, `wapsi.ports`, `wapsi.config`, `wapsi.money`,
   `wapsi.citations`, `wapsi.timeutil`, and `wapsi.domain.clocks` (to know which dates belong to the case).
2. **Rules re-implemented from `AGENTS.md` convention 6** in `wapsi.verify.policy`, checked against the trace, not
   against the plan's own fields. For example, whether an account is a pool account is read from
   `TraceResult.accounts`, so a plan that mislabels the pool account's role still fails.
3. **Every value recomputed.** `wapsi.verify.numeric` recomputes every slot with its own resolvers
   ([draft-contract.md](../draft-contract.md)), re-fills the draft's skeleton and requires exact equality, and
   checks that every amount, account number, UTR and date in the body and the structured payload belongs to the
   case.
4. **Citations and injection.** `wapsi.verify.citations` fails unknown or undeclared citation ids (and warns when a
   cited id has no verbatim quote). `wapsi.verify.injection` checks drafts for untrusted text that leaked in.
5. **Checked at three points.** `process_case` runs `verify_plan` (report id `PLAN`) right after planning and
   `verify_draft` on every draft. `approve` re-verifies the draft when given a ledger, and binds the approval to the
   SHA-256 fingerprint of the verified content.
6. **Errors block, warnings do not.** A report passes only when no error-severity check fails. Every message starts
   with a stable code (`over_hold`, `intermediary_disposition`, `forward_hold_not_first_layer`,
   `body_skeleton_mismatch`, `innocent_in_str`, ...), so tests and the UI can match on it.
7. **A third implementation for the trace.** The verifier checks the plan against the trace but does not re-run the
   trace. The trace itself is checked offline against golden sets produced by `wapsi.synth.reference`, which does
   not import `wapsi.graph`, `wapsi.classify`, `wapsi.draft` or `wapsi.verify`.

## Consequences

**The verifier catches planner errors.** `tests/verify/test_verify_plan.py` takes the correct showcase plan, breaks
one thing, and expects a specific failure. Examples:

| Mutation | Expected failure code |
|---|---|
| Hold ₹45,000.00 (the balance) on mule A instead of ₹43,200.00 (the tainted money) | `over_hold` |
| Hold the landlord's whole ₹1,55,000.00 balance | `over_hold` |
| Forward hold on a layer-2 account | `forward_hold_not_first_layer` |
| `HOLD_AMOUNT` instead of `HOLD_AMOUNT_INTERMEDIARY` on the pool account (right amount) | `intermediary_disposition` |
| Pool account left unheld | `intermediary_disposition` |
| Forward hold on the pool account | `forward_hold_on_intermediary` (and `forward_hold_not_first_layer`) |
| s.106 flag on the pool account | `intermediary_escalated` |
| `HOLD_AMOUNT_INTERMEDIARY` on an ordinary account | `intermediary_disposition_misused` |
| Hold on the kirana shop whose goods were delivered | `delivered_merchant_held` |

At draft level, `tests/verify/test_verify_drafts.py` and `tests/verify/test_verify_review_fixes.py` do the same for
wrong amounts, fabricated UTRs, unknown accounts and dates, swapped slot values, a whole-account freeze instruction,
an innocent holder named in the STR, and a restoration pack that uses Process 1 for two victims.

**On correct output it stays quiet.** Showcase case 1 produces 17 reports (16 drafts plus the plan), all passing.
On the demo world `draft_verification_pass_rate` is 1.0 (`evals/reports/latest.json`). With a hallucinating LLM,
all 301 demo drafts still pass, because the drafter's own prose checks caught every wrong amount first; the
verifier is the second line ([ADR 0003](0003-numbers-from-data-words-from-llm.md)).

Costs and trade-offs:

- **Every rule lives in several places**: the planner, the verifier, the reference implementation, and the
  `lien-calculator` skill script. A rule change must update all of them; the SOP Para 9.1(iii) correction
  ([ADR 0004](0004-intermediary-accounts-sop-9-1-iii.md)) now appears in each one.
- **Some shared code remains, on purpose.** The money, account, UTR and date finders in `wapsi.money` are shared
  by the drafter and the verifier, because both must parse text identically (`docs/draft-contract.md`). A bug
  there is common to both; `tests/verify/test_verify_money_mentions.py` covers them separately.
- **Roles are taken as given.** The verifier re-checks the rules for each role, but it does not re-classify
  accounts: `innocent_in_str` and the innocent-holder checks read roles from the classifier and the plan. An innocent
  holder that the classifier labels `MULE` is therefore treated as a mule, and an STR that names it passes. This
  happened in two held-out synthetic cases
  ([ADR 0006](0006-transparent-heuristic-classifier-default.md#what-the-evaluation-showed)); role errors are left
  to the officer and the supervisor.
- **Some checks need the ledger.** Merchant delivery and prior NCRP reports behind an s.106 flag can only be
  checked with a ledger. Without one, `verify_plan` reports the warning `escalation_unverified` and cannot fire
  `delivered_merchant_held`.
- **Runtime.** Verification runs inside `process_case`; on the demo world the whole case including verification
  has a p95 of 0.7245 s with the offline LLM (`evals/reports/demo.json`, 2026-09-29).

## Alternatives considered

| Option | Why not |
|---|---|
| Assertions inside the planner | Same code, same blind spots: a wrong rule is asserted as right |
| A verifier that calls the planner's helpers | Cheaper to write, but a shared bug passes both |
| An LLM as the judge | Not deterministic, and exposed to the same injected text. The AMD edition adds an LLM judge as an extra guard on top of the rules, not in place of this verifier |
| Re-run the full trace inside the verifier | Doubles the work per case; the independent reference implementation checks the trace offline against golden sets instead |

## Evidence

- Code: `src/wapsi/verify/` (`runner.py`, `policy.py`, `numeric.py`, `citations.py`, `injection.py`,
  `report.py`), `src/wapsi/pipeline/run.py` (`process_case`, `approve`, `draft_fingerprint`),
  `src/wapsi/synth/reference.py`.
- Import boundary: `grep -rnE --include=*.py "^\s*(from|import) wapsi\.(graph|classify|draft)" src/wapsi/verify`
  finds nothing (one comment in `numeric.py` mentions `wapsi.draft.render` to say its placeholder pattern follows
  the same grammar; there is no import).
- Tests: `tests/verify/test_verify_plan.py`, `tests/verify/test_verify_drafts.py`,
  `tests/verify/test_verify_review_fixes.py`, `tests/verify/test_verify_policy_extra.py`,
  `tests/pipeline/test_pipeline_approvals.py`.
- Reports: `evals/reports/latest.json` (`draft_verification_pass_rate`, `citation_validity`, `injection_leaks`).
