# ADR 0001: Hold the tainted amount in each account (amount-specific holds by default)

- **Status:** Accepted; amended by [0009](0009-account-level-hold-as-recorded-exception.md) (account-level action
  only as a recorded, supervisor-approved exception)
- **Date:** 2026-09-16
- **Related:** [0002](0002-pro-rata-apportionment.md) (how the tainted amount is computed),
  [0004](0004-intermediary-accounts-sop-9-1-iii.md) (nodal, pool and escrow accounts),
  [0008](0008-verifier-independent-of-planner.md) (how the rule is enforced)

## Context

When a bank receives a CFCFRMS notice, the stolen money has usually moved through several mule accounts in minutes,
mixing with the account holders' own money on the way. The simple response is to freeze every account the money
touched. That immobilises far more than the victim lost, and it hits people who did nothing wrong: a landlord who
was paid rent, a shop that sold goods, a payment aggregator whose pool account holds many merchants' money.

The legal texts point the other way:

- SOP Para 5(ix): "Unwarranted orders for freezing accounts shall be discouraged, and accountability measures shall
  be established." `[SOP-5-IX]`
- SOP Para 9.1 and the Annexure-I notice ask the bank nodal officer to put the amount on hold, not the account
  (`[SOP-9-1]`, `[SOP-ANNEX-I]`).
- Five High Court decisions are registered in `src/wapsi/citations.py`, each with a one-line summary to the same
  effect: hold or lien the disputed amount, do not freeze the account (`CASE-NEELKANTH`, `CASE-KARTIK-CHATUR`,
  `CASE-SAJEER`, `CASE-SAIFULLAH`, `CASE-SHREE-BALAJI`). Two carry a verbatim quote as summarised in SOP Annexure-III, for example:
  "freezes must be proportionate and confined only to the specific amounts indicated in police requisitions,
  rather than immobilizing entire accounts" `[CASE-SAJEER]`. The other three are cited by URL and summary only.

And the outcome the SOP itself reports is poor: from April 2021 to November 2025, CFCFRMS prevented ₹7,647 crore
of ₹52,969 crore reported from reaching criminals, but only ₹167 crore, about 2.18% of the money saved, had been
restored to victims (`data/docs/facts.json`, SOP Para 2, printed page 9).

## Decision

Wapsi recommends one amount-specific decision per account that received tainted money
(`wapsi.graph.holdplan.build_hold_plan`, `AGENTS.md` convention 6):

1. **Hold only the tainted money still in the account:** `hold_amount = min(tainted money now, balance now)`, from
   the exact trace ([ADR 0002](0002-pro-rata-apportionment.md)), not the whole balance. The planner never
   proposes account-level action; it exists only as a recorded, supervisor-approved exception on an officer's
   request ([ADR 0009](0009-account-level-hold-as-recorded-exception.md)).
2. **Home bank only.** Tainted money at another bank becomes an `OUTBOUND_CFCFRMS_REQUEST` with `hold_amount = 0`
   and the requested amount in `tainted_amount`.
3. **Forward hold on first-layer accounts only** (SOP Para 9.1(i)), when the balance is below the disputed amount.
   The forward-hold amount is the tainted money the account sent to ATM or crypto exits or to unlocated
   destinations. It is a hold on future credits, so it is not counted as recoverable.
4. **Special cases keep the same principle:** merchants with delivered goods are not held (`NO_HOLD_UPDATE_ONLY`,
   SOP Para 9.2); undelivered goods are held in the pool (`HOLD_IN_POOL`); innocent holders are held without
   escalation and with no automatic STR (`HOLD_AMOUNT_NO_ESCALATION`, SOP Para 5(xii); the officer decides, and
   an STR is recommended where the account was used as a conduit); nodal, pool and escrow accounts are held for the
   disputed amount only ([ADR 0004](0004-intermediary-accounts-sop-9-1-iii.md)).
5. **Show the alternative.** Every plan also records the whole-account freeze baseline: what freezing the same
   home-bank accounts would immobilise (`HoldPlan.baseline_freeze_amount`, `baseline_freeze_accounts`), a
   comparison figure, never a recommendation; and `over_hold_amount`, which must be 0.
6. **No draft may instruct a freeze** outside an approved exception. The verifier fails any draft that instructs a
   whole-account or debit freeze, or blocking the account itself (`wapsi.verify.policy.freeze_instructions`),
   unless it is the hold instruction of an account-level exception a supervisor approved
   ([ADR 0009](0009-account-level-hold-as-recorded-exception.md)). Negated or descriptive mentions, and a freeze
   whose object is an amount ("freeze the disputed amount"), are allowed.
7. **A holding disposition always holds money.** If nothing is left to hold, the decision is `CASHED_OUT` or
   `NO_ACTION`, never a hold of ₹0.00.

## Consequences

**Showcase case 1** (`src/wapsi/fixtures/showcase.py`): a victim loses ₹4,80,000.00.

| | Whole-account freeze baseline | Wapsi |
|---|---|---|
| Money immobilised at the home bank | ₹5,03,50,000.00 across 6 accounts | ₹1,42,848.00 across 5 accounts |
| Payment-aggregator pool account | ₹5,00,20,000.00 frozen | ₹19,200.00 held; no suspension or seizure |
| Landlord paid ₹5,000.00 rent from the mule | whole account frozen | ₹4,800.00 held, no escalation, EDD memo |
| Kirana shop, goods delivered | whole account frozen | no hold, CFCFRMS update only |
| Recoverable for the victim | not computed | ₹3,34,848.00 (home holds + ₹1,92,000.00 requested from the other bank) |

**Demo world** (39 cases, `evals/reports/demo.json`): a whole-account freeze of every home-bank account that
received tainted money would immobilise ₹35,25,90,069.78 across 101 accounts. Wapsi holds ₹29,19,245.69 across
84 accounts. Against the independent golden set, `hold_exact_match_rate` is 1.0, `over_hold_inr` is 0.0 and
`false_freeze_count` is 0.

Costs and trade-offs:

- **Exact tracing is required.** "The tainted amount now" needs a full replay of the money through every layer
  ([ADR 0002](0002-pro-rata-apportionment.md)). That is most of the engine's complexity.
- **Wapsi can hold less than a literal reading of "the disputed amount received".** When a mule mixed in its own
  money, Wapsi holds only the victims' share of what is left. In showcase case 1, holding
  `min(victim money received, balance)` at the same five accounts would give ₹1,47,800.00; Wapsi holds
  ₹1,42,848.00. The difference, ₹4,952.00, is what remains of mule A's own ₹20,000.00 cash deposit (01-09-2026)
  in A, B and D (₹1,800.00 + ₹1,152.00 + ₹2,000.00). There is no override in this version: a hold above the
  tainted amount fails verification as `over_hold`.
- **Amounts are as of the plan.** Money can move between planning and execution. The hold instruction states the
  amounts; the HOLD_LAPSE clock (90 calendar days) and `pending_holds` track what has not been executed.
- **Forward holds are recommendations.** They depend on future credits and are kept out of `total_recoverable`.

## Alternatives considered

| Option | Why not |
|---|---|
| Freeze every account the money touched (the status quo) | Discouraged by SOP Para 5(ix) and by the courts; in the showcase it immobilises ₹5,03,50,000.00 to protect a ₹4,80,000.00 loss |
| Debit-freeze the account | Same effect on the holder as a freeze; the citation registry summarises `CASE-KARTIK-CHATUR` (Bombay HC, 20.11.2025) as "Banks may only place the disputed amount under lien; no debit freeze under s.106" |
| Hold the reported amount at the first-layer account only | Misses money that moved on. In the showcase, first-layer account A has ₹45,000.00 left; the holds at later layers (B, D, L and the pool) come to ₹99,648.00, and another ₹1,92,000.00 is at another bank |
| Hold `min(amount received from the trail, balance)` at every account, without taint tracing | Simpler, but holds the holder's own commingled money: at the same five showcase accounts it gives ₹1,48,800.00 on gross receipts (₹1,47,800.00 if only the victim share of each receipt is counted, which already needs tracing) against Wapsi's ₹1,42,848.00; and it cannot say how much belongs to which victim, which Process 2 needs |

## Evidence

- Code: `src/wapsi/graph/holdplan.py` (module docstring lists every rule), `src/wapsi/verify/policy.py`
  (`check_plan`, `freeze_instructions`).
- Tests: `tests/graph/test_graph_showcase.py`; `tests/verify/test_verify_plan.py`
  (`test_over_hold_in_plan`, `test_whole_balance_hold_on_innocent_holder`, `test_forward_hold_on_layer_2`);
  `tests/verify/test_verify_drafts.py` (`test_whole_account_freeze_instruction`,
  `test_negated_freeze_mentions_are_allowed`).
- Reports: `evals/reports/demo.json`, `evals/reports/hard.json` (`baseline`, `over_hold_inr`, `false_freeze_count`;
  ₹0.00 and 0 in every world, including the 64-case hard world).
- Citations: `SOP-5-IX`, `SOP-9-1`, `SOP-9-1-I`, `SOP-9-2`, `SOP-5-XII`, `SOP-ANNEX-I`, `SOP-6` in
  `data/docs/citations.json`.
