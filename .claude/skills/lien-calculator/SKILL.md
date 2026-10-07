---
name: lien-calculator
description: Build and explain the amount-specific hold (lien) plan for an NCRP cyber-fraud case — one disposition per account (hold tainted amount, innocent-holder hold without escalation, merchant no-hold, amount hold in nodal/pool/escrow accounts, outbound CFCFRMS request, first-layer forward hold), each with SOP/BNSS citations, totals, per-victim recoverable, restoration process and the whole-account-freeze baseline comparison. Runs the portable Python core (wapsi.graph.holdplan.build_hold_plan) with an independent re-check of the hard rules and wapsi.verify.verify_plan. Use for "hold plan", "lien", "how much to hold", "dispositions", "recoverable".
---

# lien-calculator

Turns a trace into amount-specific holds. The plan is a **recommendation pending supervisor approval**; nothing is
held until a supervisor approves it (`wapsi.pipeline.run.approve`, role `supervisor`, maker-checker).

## Inputs
- NCRP id(s) (use `--group` to take the whole case).
- World as in `$money-trail-tracer` (`--world showcase-memory | demo | <dir>`). Apportionment rule defaults to
  pro-rata.

## Outputs
- Dispositions table: account, layer, role, disposition, tainted, balance, hold, forward hold, citation ids, flags.
- Hold lines (amount + UTRs + credit dates), outbound CFCFRMS requests.
- Totals: held now, forward holds, recoverable (= holds + outbound request amounts), over-hold (must be ₹0.00),
  per-victim recoverable, restoration process (P1/P2), STR ground.
- Baseline strip: "Whole-account freeze baseline (comparison only): ₹X across N accounts; this plan holds ₹Y
  across M."
- Policy re-check result (`N/N checks passed`) and `wapsi.verify.verify_plan` result.

## Dispositions (AGENTS.md convention 6 — enforced in code and re-checked by the verifier)
| Condition | Disposition | Hold amount | Cite |
|---|---|---|---|
| Mule / unknown individual or business account holding tainted money | `HOLD_AMOUNT` | `min(tainted, balance)` — amount-specific by default | [SOP-9-1] [SOP-ANNEX-I] [SOP-5-IX] |
| First-layer account whose balance < disputed amount | stays `HOLD_AMOUNT` (or `FORWARD_HOLD` if nothing holdable now) + reason `FORWARD_HOLD_FIRST_LAYER` | `forward_hold_amount` = tainted money it sent to ATM/crypto exits or unlocated destinations; never on nodal/pool/escrow accounts | [SOP-9-1-I] [SOP-DEF-FIRST-LAYER] |
| Innocent holder, bona fide recipient (kept the money; role value `INNOCENT_HOLDER`) | `HOLD_AMOUNT_NO_ESCALATION` + EDD memo; no STR by default | `min(tainted, balance)` | [SOP-5-XII] [SOP-10-1] |
| Innocent holder, unwitting conduit (used as a pass-through by someone else) | `HOLD_AMOUNT_NO_ESCALATION` + EDD memo + reasons `UNWITTING_CONDUIT`, `STR_RECOMMENDED_OFFICER_DECISION`, `requires_human`: STR recommended, the officer decides (never automatic) | `min(tainted, balance)` | [SOP-5-XII] [SOP-10-1] [PMLR-2-1-G] |
| Merchant, goods delivered (invoice evidence) | `NO_HOLD_UPDATE_ONLY` | 0 | [SOP-9-2] |
| Merchant, goods undelivered | `HOLD_IN_POOL` | tainted amount, in the PA/PG pool | [SOP-9-2] |
| Nodal / pool / escrow account | `HOLD_AMOUNT_INTERMEDIARY` (a holding disposition) | `min(tainted, balance)` — the disputed amount only; **no forward hold**; never an s.106 flag; no suspension of digital banking, no seizure of the account | [SOP-9-1-III] [SOP-9-1] [SOP-ANNEX-I] |
| Other-bank account holding tainted money | `OUTBOUND_CFCFRMS_REQUEST` | 0; request amount in `tainted_amount` | [SOP-9-1] [SOP-6] |
| FIR present **and** ≥ 2 prior NCRP reports | flag only: reason `ESCALATE_S106_CANDIDATE`, `requires_human = true`; disposition stays `HOLD_AMOUNT` | unchanged | [BNSS-106] [CASE-KARTIK-CHATUR] |
| Officer request with ≥ 1 recorded `AccountLevelReason`, approved by a different supervisor (never automatic) | `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD` | unchanged amount-specific hold (the recoverable money); the account-level restriction is on top | [SOP-5-IX] [SOP-9-1] |

`total_recoverable` = home-bank holds + outbound request amounts (forward holds are not money in hand).
Restoration process = P2 when more than one victim has recoverable money [SOP-11-P2], else P1 [SOP-11-P1].
Layer-0 (victim) accounts never get a decision.

## Steps
```
uv run python .claude/skills/lien-calculator/scripts/hold_plan.py 31009260000101 --world showcase-memory
uv run python .claude/skills/lien-calculator/scripts/hold_plan.py <ncrp_id> --world demo --group
uv run python .claude/skills/lien-calculator/scripts/hold_plan.py <ncrp_id> --world demo --group --all-checks
```
Exit 1 = a policy check failed. Report the FAIL lines verbatim and stop; do not present the plan as ready.

## How to explain the plan
1. Lead with the baseline strip (the court-mandated difference: lien on the disputed amount, not a freeze —
   [CASE-NEELKANTH] [CASE-SHREE-BALAJI]).
2. Go row by row in layer order: disposition, amount, one-line reason, citation ids exactly as in the output.
3. Call out every `requires_human` row and every innocent holder (EDD memo; no automatic STR, the officer
   decides; STR recommended where the account was used as a conduit).
4. State the apportionment rule, the restoration process and the next step (`$str-drafter`, then approval).
Use `$sop-citer` if the officer wants the text behind a citation id.

## Guardrails
- Allowed verbs: "hold ₹X in account Y", "request the other bank", "no hold". Do not recommend "freeze",
  "block the account" or "debit freeze" (a debit freeze needs a s.107 BNSS magistrate order — [CASE-KARTIK-CHATUR]):
  account-level action exists only as a recorded, supervisor-approved `EXCEPTIONAL_ACCOUNT_LEVEL_HOLD`.
- Never raise a hold above the tainted amount. Nodal / pool / escrow accounts: hold the disputed amount only
  (SOP 9.1(iii)); never a forward hold on them (SOP 9.1(i)), never suspension of digital banking or seizure, never
  an s.106 flag. The legacy disposition `BLOCK_NODAL_POOL_ESCROW` ("never hold") is wrong and must not appear.
- Never turn the s.106 flag into an action; it needs a human, an FIR and prior reports.
- Only cite ids that appear in the output (all come from `wapsi.citations`). Never invent a clause.
- Amounts are copied from the script output, never recomputed in prose.

## How to verify
- Numbers must equal `SHOWCASE_1` / `SHOWCASE_2` in `src/wapsi/fixtures/showcase.py` (the fixture is the source of
  truth). Showcase 1 today: holds ₹43,200 + ₹27,648 + ₹48,000 + ₹4,800 (landlord, no escalation) + ₹19,200 in the
  PA pool account `HB01-9000000001` (`HOLD_AMOUNT_INTERMEDIARY`, no forward hold) = ₹1,42,848.00 across 5
  accounts; forward hold ₹96,000.00 on `HB01-1000000001` only; outbound requests ₹48,000.00 + ₹1,44,000.00;
  recoverable ₹3,34,848.00; baseline ₹5,03,50,000.00 across 6 accounts; P1.
- Showcase 2 (`31109260000102 --group`): hold ₹1,20,000.00 on `HB01-1000000004` (₹75,000 / ₹45,000 per victim),
  forward hold ₹40,000.00, `ESCALATE_S106_CANDIDATE` flag with HUMAN REVIEW, P2.
- Script ends with `N/N checks passed` and `verify_plan: PASS`.
