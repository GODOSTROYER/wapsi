# ADR 0002: Apportion mixed money to victims pro-rata, by exact replay

- **Status:** Accepted
- **Date:** 2026-09-16
- **Related:** [0001](0001-amount-specific-holds.md) (holds need the tainted amount per account),
  [0008](0008-verifier-independent-of-planner.md)

## Context

A mule account rarely holds one victim's money alone. It receives money from several victims, has some money of its
own, and sends money out between receipts. To hold only the victim's money ([ADR 0001](0001-amount-specific-holds.md))
and to return it to the right people, Wapsi has to answer: of this balance, how much belongs to each victim?

The SOP answers the question directly. Para 5(xiii) asks for attribution to the victim where possible and, where
money is commingled, says "the principle of equitable or pro-rata distribution will be adopted" `[SOP-5-XIII]`.
Process 2 for multiple victims ascribes amounts under Para 5(xiii) `[SOP-11-P2]`. Annexure-V, Illustration 2,
works an example: five complainants' credits into one account, one debit, and the interim custody "released on a
pro rata basis" `[SOP-ANNEX-V]`.

Common alternatives in practice are first-in-first-out (FIFO), last-in-first-out (LIFO), or treating every rupee in
a touched account as tainted. They give different answers, and the differences decide who gets money back.

## Decision

1. **Pro-rata is the default rule.** `TraceConfig.rule` defaults to `ApportionmentRule.PRO_RATA`. A debit of `a`
   from a balance `B` that carries taint vector `τ` takes `a × τ[v] / B` of each victim `v`'s money
   (`wapsi.graph.taint`). Legitimate credits dilute the ratio.
2. **Exact replay, not estimation.** Every transaction touching a candidate account inside the trace window is
   replayed in time order, starting from `Ledger.balances_at(candidates, window_start)`. Same-second transactions
   are ordered so that a credit comes before the debits it funds (`wapsi.graph.common.event_order`).
3. **One taint vector per victim complaint.** Vectors are keyed by `ncrp_id` and carried on every edge
   (`TrailEdge.tainted`) and account (`AccountState.tainted_now`), so each hold has a per-victim split
   (`HoldDecision.per_victim`).
4. **Terminals are not expanded.** Merchant, nodal, pool and escrow accounts get their own SOP treatment
   (Para 9.1(iii), 9.2), so the trace stops there. They keep the victims' money while their balance covers it
   (lowest intermediate balance, an `AGENTS.md` convention rather than an SOP text); a debit that takes the
   balance below the tainted amount carries the shortfall off the trail, split pro-rata across victims.
5. **FIFO and LIFO stay available** as explicit overrides for comparison (`TraceConfig(rule=ApportionmentRule.FIFO)`),
   with per-account lots. The hold plan and drafts state which rule was used.
6. **Paise-exact splits.** When a hold is smaller than the tainted amount, per-victim shares use largest-remainder
   apportionment in paise (`wapsi.graph.holdplan`).
7. **Every rupee is accounted for:** `stolen = holdable + cashed_out + outbound + intermediary + merchant_settled +
   unlocated`, with `unlocated` expected to be about ₹0.00 (`TraceTotals`).

## Consequences

**Wapsi reproduces the SOP's own worked example to the paisa.** Annexure-V Illustration 2: credits of ₹85,326
(V), ₹38,443 (W), ₹50,000 (X), ₹50,000 (Y) and ₹30,000 (Z) into one account on 15.12.2024, then a debit of
₹47,226, leaving ₹2,06,543. `tests/test_sop_annexure_v.py` asserts that Wapsi's per-victim shares equal the SOP's.
The same account under the three rules (computed with `trace_case` on the ledger built in
`tests/test_sop_annexure_v.py` and each `TraceConfig(rule=...)`; re-run on 2026-09-28):

| Complainant | SOP Annexure-V | Pro-rata (default) | FIFO | LIFO |
|---|---:|---:|---:|---:|
| V | ₹69,446.97 | ₹69,446.97 | ₹38,100.00 | ₹85,326.00 |
| W | ₹31,288.82 | ₹31,288.82 | ₹38,443.00 | ₹38,443.00 |
| X | ₹40,695.08 | ₹40,695.08 | ₹50,000.00 | ₹50,000.00 |
| Y | ₹40,695.08 | ₹40,695.08 | ₹50,000.00 | ₹32,774.00 |
| Z | ₹24,417.05 | ₹24,417.05 | ₹30,000.00 | ₹0.00 |

Only pro-rata matches the SOP. Under LIFO the last complainant, Z, would get nothing.

**The rule changes who gets what in multi-victim cases.** Showcase case 2 (`SHOWCASE_2`): two victims send
₹1,00,000 and ₹60,000 to the same mule, which then withdraws ₹40,000 at an ATM. The ₹1,20,000 left is split:

| Rule | Victim of complaint `31109260000102` | Victim of complaint `31109260000103` |
|---|---:|---:|
| Pro-rata (default) | ₹75,000.00 | ₹45,000.00 |
| FIFO | ₹60,000.00 | ₹60,000.00 |
| LIFO | ₹1,00,000.00 | ₹20,000.00 |

Other effects:

- **Independent ground truth agrees.** `wapsi.synth.reference` implements exact pro-rata separately and produces
  the golden sets. On the demo world (39 cases) `traced_amount_recall` is 1.0, and the summed absolute error over
  all trace-total fields of all cases is ₹0.34, which is rounding (`evals/reports/latest.json`).
- **It costs a full replay.** Every transaction of every candidate account in the window is simulated. The
  20,000-transaction performance test asserts under 2 seconds for each rule
  (`tests/graph/test_graph_property.py::test_performance_20k_transactions`); on a busy development machine some
  runs took longer ([architecture.md](../architecture.md#performance)).
- **It depends on complete visibility.** Money that leaves for an account outside the candidate set is reported as
  `unlocated` with a warning, unless it goes to another bank that is not followed (`outbound`).
- **Attribution first is still a human call.** Para 5(xiii) prefers direct attribution where it is possible.
  Wapsi does not guess it. FIFO or LIFO can be chosen in configuration (`TraceConfig.rule`) to compare results,
  and the plan records the rule used. The officer app shows the rule but has no switch to change it.

## Alternatives considered

| Option | Why not the default |
|---|---|
| FIFO | Does not reproduce Annexure-V; favours whichever victim paid last into an account that later drained (in showcase case 2 it moves ₹15,000 from one victim to the other) |
| LIFO | Does not reproduce Annexure-V; in Illustration 2 it leaves complainant Z with ₹0.00 |
| Treat every rupee in a touched account as tainted | This is the logic of the whole-account freeze that [ADR 0001](0001-amount-specific-holds.md) rejects; it also cannot split money between victims |
| Trace only the first layer | Loses the money that moved on (₹99,648.00 of holds and ₹1,92,000.00 of requests at later layers in showcase case 1) |
| A statistical or ML estimate of attribution | Not reproducible to the paisa, and hard to defend in a restoration order |

## Evidence

- Code: `src/wapsi/graph/taint.py` (rules in the module docstring), `src/wapsi/graph/expand.py`,
  `src/wapsi/graph/common.py`, `src/wapsi/config.py` (`TraceConfig`).
- Tests: `tests/test_sop_annexure_v.py`; `tests/graph/test_graph_rules.py` (`test_pro_rata`, `test_fifo`,
  `test_lifo`, `test_multi_victim_vectors`); `tests/graph/test_graph_property.py`.
- Ground truth: `src/wapsi/synth/reference.py`, `data/golden/README.md`.
- Facts: `ANNEX_V_ILL2_*` entries in `data/docs/facts.json`.
- Citations: `SOP-5-XIII`, `SOP-11-P2`, `SOP-ANNEX-V`.
