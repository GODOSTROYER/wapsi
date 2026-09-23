"""Regression tests for the money-trail review findings (F2, F3, F4, F9, F10, F11 and consortium detection).

Each test pins the corrected behaviour on a small hand-computed world.
"""

from __future__ import annotations

import random

import pytest
from graph_worlds import World, parts_sum, roles

from wapsi.config import TraceConfig, WapsiConfig
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.holdplan import _scale, forward_hold_vectors
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    AccountType,
    Channel,
    Disposition,
    OrderStatus,
    RestorationProcess,
    Role,
)

N1, N2, N3, N4 = "95000000000001", "95000000000002", "95000000000003", "95000000000004"
V1 = "OB02-V1"
A, B, C = "HB01-A", "HB01-B", "HB01-C"
K = "HB01-K"
OC = "OB03-C"


def _decisions(plan):
    return {d.account_id: d for d in plan.decisions}


# --------------------------------------------------------------------------- F2: same-timestamp ordering


@pytest.mark.parametrize(("credit_id", "debit_id"), [("TX0001", "TX0002"), ("TX0002", "TX0001")])
def test_same_second_credit_is_applied_before_the_debit_it_funds(credit_id, debit_id):
    # victim -> A and A -> B in the same second; the debit must not run before the credit that funds it
    w = World()
    w.acct(A)
    w.acct(B)
    w.seed(N1, 0, V1, A, 100000.0, txn_id=credit_id)
    w.txn(0, A, B, 90000.0, txn_id=debit_id)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.accounts[A].tainted_now_total == 10000.0
    assert trace.accounts[B].tainted_now_total == 90000.0
    assert trace.accounts[B].layer == 2
    t = trace.totals
    assert (t.stolen, t.holdable, t.unlocated) == (100000.0, 100000.0, 0.0)
    assert trace.warnings == []
    dec = _decisions(build_hold_plan(trace, roles({A: Role.MULE, B: Role.MULE}), ledger, WapsiConfig()))
    assert (dec[A].hold_amount, dec[B].hold_amount) == (10000.0, 90000.0)


def test_same_second_chain_burst_with_adversarial_txn_ids():
    # V1 -> A -> B -> C -> ATM, all in one second; txn ids sort in the reverse of the money's path
    w = World()
    for x in (A, B, C):
        w.acct(x)
    w.seed(N1, 0, V1, A, 10000.0, txn_id="Z9")
    w.txn(0, A, B, 10000.0, txn_id="Z5")
    w.txn(0, B, C, 10000.0, txn_id="Z1")
    w.txn(0, C, None, 4000.0, Channel.ATM, txn_id="A0")
    trace = trace_case(w.ledger(), [N1], TraceConfig())
    acc = trace.accounts
    assert (acc[A].layer, acc[B].layer, acc[C].layer) == (1, 2, 3)
    assert acc[C].tainted_now_total == 6000.0
    t = trace.totals
    assert (t.holdable, t.cashed_out, t.unlocated) == (6000.0, 4000.0, 0.0)
    assert {e.txn_id: e.tainted_total for e in trace.edges} == {
        "Z9": 10000.0,
        "Z5": 10000.0,
        "Z1": 10000.0,
        "A0": 4000.0,
    }


def test_same_second_cycle_is_deterministic_and_conserves():
    w = World()
    w.acct(A, opening=1000.0)
    w.acct(B, opening=1000.0)
    w.seed(N1, 0, V1, A, 5000.0, txn_id="S")
    w.txn(0, A, B, 3000.0, txn_id="AB")
    w.txn(0, B, A, 2000.0, txn_id="BA")
    ledger = w.ledger()
    first = trace_case(ledger, [N1], TraceConfig())
    again = trace_case(ledger, [N1], TraceConfig())
    assert first.model_dump(exclude={"computed_at"}) == again.model_dump(exclude={"computed_at"})
    assert abs(parts_sum(first.totals) - 5000.0) < 1e-6
    assert first.totals.unlocated == 0.0
    # S funds A first; A -> B and B -> A form a cycle, broken by txn id (AB before BA)
    assert first.accounts[B].tainted_received_total == 2500.0


# --------------------------------------------------------------------------- F3: victim-to-victim chains


def _victim_chain(v2_sends_first: bool = False, fir: str | None = None):
    """V1 pays ₹50,000 into victim V2 (N1); V2 (₹30,000 of its own) reports a ₹60,000 transfer to M (N2)."""
    w = World()
    v2 = w.acct("HB01-VIC2", opening=60000.0 if v2_sends_first else 30000.0)
    m = w.acct("HB01-M")
    if v2_sends_first:
        w.seed(N2, 0, v2, m, 60000.0)
        w.seed(N1, 5, V1, v2, 50000.0, fir=fir)
    else:
        w.seed(N1, 0, V1, v2, 50000.0, fir=fir)
        w.seed(N2, 5, v2, m, 60000.0)
    return w, v2, m


@pytest.mark.parametrize("order", [[N1, N2], [N2, N1]])
def test_victim_account_forwarding_another_victims_money(order):
    w, v2, m = _victim_chain()
    ledger = w.ledger()
    trace = trace_case(ledger, order, TraceConfig())
    # V2 is on N1's trail: simulated like any other account (layer 1), its balance mixes N1's money and its own
    assert trace.accounts[V1].layer == 0
    st = trace.accounts[v2]
    assert st.layer == 1 and st.balance_now == 20000.0
    assert st.tainted_now == {N1: 12500.0}
    assert (st.tainted_received_total, st.tainted_sent_total) == (50000.0, 37500.0)
    # the reported ₹60,000 carries pro-rata N1 money 60,000 x 50/80 = 37,500; N2's own money is the rest
    assert trace.accounts[m].tainted_now == {N1: 37500.0, N2: 22500.0}
    assert trace.accounts[m].layer == 1
    seed = next(e for e in trace.edges if e.from_account == v2)
    assert seed.tainted == {N1: 37500.0, N2: 22500.0} and seed.depth == 1
    assert trace.victim_seed == {N1: 50000.0, N2: 22500.0}
    t = trace.totals
    assert (t.stolen, t.holdable, t.unlocated) == (72500.0, 72500.0, 0.0)
    assert any(v2 in x and N1 in x and N2 in x for x in trace.warnings)

    plan = build_hold_plan(trace, roles({m: Role.MULE, v2: Role.UNKNOWN}), ledger, WapsiConfig())
    dec = _decisions(plan)
    assert dec[m].hold_amount == 60000.0 and dec[m].per_victim == {N1: 37500.0, N2: 22500.0}
    assert sum(dec[m].per_victim.values()) <= trace.accounts[m].balance_now
    assert dec[v2].disposition == Disposition.HOLD_AMOUNT and dec[v2].hold_amount == 12500.0
    assert dec[v2].per_victim == {N1: 12500.0}
    assert dec[v2].requires_human and "VICTIM_ACCOUNT_ON_TRAIL" in dec[v2].reason_codes
    assert plan.per_victim_recoverable == {N1: 50000.0, N2: 22500.0}
    assert plan.total_recoverable == 72500.0
    assert plan.restoration_process == RestorationProcess.P2


def test_victim_account_on_trail_is_never_an_escalation_candidate():
    w, v2, m = _victim_chain(fir="FIR 5/2026")
    w.signal(v2, 5)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1, N2], TraceConfig())
    d = _decisions(build_hold_plan(trace, roles({v2: Role.MULE}), ledger, WapsiConfig()))[v2]
    assert d.disposition == Disposition.HOLD_AMOUNT
    assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes and d.requires_human


def test_victim_that_sends_before_receiving_keeps_its_full_claim():
    w, v2, m = _victim_chain(v2_sends_first=True)
    trace = trace_case(w.ledger(), [N1, N2], TraceConfig())
    assert trace.victim_seed == {N1: 50000.0, N2: 60000.0}
    assert trace.accounts[m].tainted_now == {N2: 60000.0}
    assert trace.accounts[v2].tainted_now == {N1: 50000.0} and trace.accounts[v2].layer == 1
    assert trace.totals.stolen == 110000.0 and trace.totals.unlocated == 0.0


def test_sending_only_victims_stay_at_layer_zero():
    w, v2, m = _victim_chain()
    trace = trace_case(w.ledger(), [N2], TraceConfig())  # N1 not in this case: V2 is a plain victim
    assert trace.accounts[v2].layer == 0 and trace.accounts[v2].tainted_now == {}
    assert trace.victim_seed == {N2: 60000.0}
    assert trace.accounts[m].tainted_now == {N2: 60000.0}


def test_refund_of_own_money_to_a_victim_in_a_multi_victim_case():
    # A refunds ₹2,000 of commingled money to V1: N1's half is returned money (unlocated), N2's half now sits in V1
    w = World()
    w.acct(A)
    v2 = "OB02-V2"
    w.seed(N1, 0, V1, A, 5000.0)
    w.seed(N2, 1, v2, A, 5000.0)
    w.txn(10, A, V1, 2000.0, txn_id="REFUND")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1, N2], TraceConfig())
    assert trace.totals.unlocated == 1000.0
    assert any("victim account" in x for x in trace.warnings)
    assert trace.accounts[V1].layer == 2 and trace.accounts[V1].tainted_now == {N2: 1000.0}
    assert trace.accounts[v2].layer == 0
    assert trace.accounts[A].tainted_now == {N1: 4000.0, N2: 4000.0}
    dec = _decisions(build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig()))
    assert dec[V1].disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST and dec[V1].requires_human
    assert dec[A].forward_hold_amount == 0.0


# --------------------------------------------------------------------------- F4: merchant delivery


def _merchant_world(opening: float = 1000.0):
    w = World()
    w.acct(A)
    w.acct(K, opening=opening, kind=AccountType.MERCHANT)
    w.seed(N1, 0, V1, A, 100000.0)
    t = w.txn(5, A, K, 90000.0, txn_id="AK")
    return w, t


def test_mixed_invoices_on_one_payment_hold_the_undelivered_part():
    w, t = _merchant_world()
    w.invoice(K, A, t, 10000.0, delivered=True)
    w.invoice(K, A, t, 80000.0, delivered=False)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.totals.merchant_settled == 10000.0
    assert trace.totals.holdable == 90000.0  # A 10,000 + undelivered 80,000 at K
    d = _decisions(build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig()))[K]
    assert d.disposition == Disposition.HOLD_IN_POOL
    assert d.hold_amount == 80000.0 and d.per_victim == {N1: 80000.0}
    assert d.tainted_amount == 90000.0
    assert "HOLD_CAPPED_AT_BALANCE" not in d.reason_codes


@pytest.mark.parametrize(
    ("status", "delivered"),
    [
        (OrderStatus.DELIVERED, True),
        (OrderStatus.UNDELIVERED, False),
        (OrderStatus.CANCELLED, False),
        (None, False),  # no invoice at all
    ],
)
def test_merchant_invoice_status(status, delivered):
    w, t = _merchant_world()
    if status is not None:
        w.invoice(K, A, t, 90000.0, delivered=False, status=status)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    d = _decisions(build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig()))[K]
    if delivered:
        assert d.disposition == Disposition.NO_HOLD_UPDATE_ONLY and d.hold_amount == 0.0
        assert trace.totals.merchant_settled == 90000.0
    else:
        assert d.disposition == Disposition.HOLD_IN_POOL and d.hold_amount == 90000.0
        assert trace.totals.merchant_settled == 0.0 and trace.totals.holdable == 100000.0


def test_delivered_invoice_of_another_merchant_does_not_count():
    w, t = _merchant_world()
    w.acct("HB01-K2", kind=AccountType.MERCHANT)
    w.invoice("HB01-K2", A, t, 90000.0, delivered=True)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    d = _decisions(build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig()))[K]
    assert d.disposition == Disposition.HOLD_IN_POOL and d.hold_amount == 90000.0


# --------------------------------------------------------------------------- F9: terminals release taint


def test_undelivered_merchant_that_cashed_out_is_not_held():
    w = World()
    w.acct(A)
    w.acct(K, kind=AccountType.MERCHANT)
    w.seed(N1, 0, V1, A, 50000.0)
    t = w.txn(5, A, K, 50000.0)
    w.invoice(K, A, t, 50000.0, delivered=False)
    w.txn(10, K, None, 50000.0, Channel.ATM, txn_id="K-ATM")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    t_ = trace.totals
    assert (t_.holdable, t_.cashed_out, t_.unlocated) == (0.0, 50000.0, 0.0)
    assert trace.accounts[K].tainted_now_total == 0.0
    assert trace.accounts[K].tainted_sent_total == 50000.0
    atm = next(e for e in trace.edges if e.txn_id == "K-ATM")
    assert atm.tainted_total == 50000.0 and atm.depth == 3
    plan = build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig())
    d = _decisions(plan)[K]
    assert d.disposition == Disposition.CASHED_OUT and d.hold_amount == 0.0
    assert all(x.disposition not in HOLDING_DISPOSITIONS for x in plan.decisions)
    assert plan.total_hold == 0.0


def test_terminal_keeps_the_disputed_amount_while_its_balance_covers_it():
    # lowest-intermediate-balance: the pool pays out 3,000 of legitimate money first, then drops below the taint
    w = World()
    w.acct(A)
    pool = w.acct("HB01-POOL", opening=5000.0, kind=AccountType.POOL)
    out = w.acct("HB01-OUT")
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(5, A, pool, 10000.0)
    w.txn(10, pool, out, 3000.0, txn_id="P1")  # balance 12,000 >= taint 10,000: nothing tainted leaves
    w.txn(20, pool, out, 8000.0, txn_id="P2")  # balance 4,000 < 10,000: 6,000 of taint leaves
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig(min_edge_amount=0))
    st = trace.accounts[pool]
    assert st.tainted_now_total == 4000.0 and st.balance_now == 4000.0
    edges = {e.txn_id: e for e in trace.edges}
    assert "P1" not in edges and edges["P2"].tainted_total == 6000.0
    assert out not in trace.accounts  # terminals are not expanded
    assert trace.totals.intermediary == 4000.0 and trace.totals.unlocated == 6000.0
    assert any("nodal / pool / escrow" in x for x in trace.warnings)
    d = _decisions(build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig()))[pool]
    assert d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY and d.hold_amount == 4000.0


def test_terminal_payouts_do_not_taint_traced_accounts():
    w = World()
    w.acct(A)
    pool = w.acct("HB01-POOL", kind=AccountType.NODAL)
    w.acct(B)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(5, A, pool, 6000.0)
    w.txn(6, A, B, 4000.0)
    w.txn(10, pool, B, 6000.0, txn_id="PB")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.accounts[B].tainted_now == {N1: 4000.0}
    assert trace.accounts[B].balance_now == 10000.0
    assert trace.accounts[B].parents == [A]
    pb = next(e for e in trace.edges if e.txn_id == "PB")
    assert pb.tainted_total == 6000.0 and pb.depth == 3
    assert trace.totals.unlocated == 6000.0 and trace.totals.intermediary == 0.0
    plan = build_hold_plan(trace, roles({A: Role.MULE, B: Role.MULE}), ledger, WapsiConfig())
    dec = _decisions(plan)
    assert dec[pool].disposition == Disposition.NO_ACTION
    assert dec[B].hold_amount == 4000.0


# --------------------------------------------------------------------------- F10: other-bank intermediaries


def test_other_bank_pool_counts_as_outbound():
    w = World()
    w.acct(A)
    op = w.acct("OB03-9000000001", opening=1e6, kind=AccountType.POOL)
    w.seed(N1, 0, V1, A, 100000.0)
    w.txn(5, A, op, 100000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert (trace.totals.outbound, trace.totals.intermediary) == (100000.0, 0.0)
    plan = build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig())
    d = _decisions(plan)[op]
    assert d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST and d.tainted_amount == 100000.0
    assert plan.total_recoverable == 100000.0


# --------------------------------------------------------------------------- F11: per-victim apportionment


def test_scale_never_goes_negative_and_sums_to_the_hold():
    vec = {N1: 0.01, N2: 0.01, N3: 0.01, N4: 0.01}
    out = _scale(vec, 0.02, [N1, N2, N3, N4])
    assert all(v > 0 for v in out.values())
    assert round(sum(out.values()), 2) == 0.02
    assert out == {N1: 0.01, N2: 0.01}


def test_scale_random_vectors():
    rng = random.Random(3)
    for _ in range(2000):
        keys = [f"N{i}" for i in range(rng.randint(1, 6))]
        vec = {k: round(rng.choice([0.01, 0.02, rng.uniform(0.01, 5), rng.uniform(1, 1e6)]), 2) for k in keys}
        total = round(sum(vec.values()), 2)
        target = round(rng.uniform(0, total), 2)
        out = _scale(vec, target, keys)
        assert all(v > 0 for v in out.values())
        assert all(out[k] <= vec[k] + 1e-9 for k in out)
        assert round(sum(out.values()), 2) == (target if target > 0 else 0.0)


def test_plan_per_victim_is_never_negative():
    w = World()
    w.acct(A)
    for n in (N1, N2, N3, N4):
        w.seed(n, 0, f"OB02-{n}", A, 0.01)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1, N2, N3, N4], TraceConfig())
    st = trace.accounts[A].model_copy(update={"balance_now": 0.02})
    trace = trace.model_copy(update={"accounts": {**trace.accounts, A: st}})
    plan = build_hold_plan(trace, roles({A: Role.MULE}), ledger, WapsiConfig())
    d = _decisions(plan)[A]
    assert d.hold_amount == 0.02
    assert all(v > 0 for v in d.per_victim.values())
    assert round(sum(d.per_victim.values()), 2) == 0.02
    assert all(v >= 0 for v in plan.per_victim_recoverable.values())


# --------------------------------------------------------------------------- consortium following from the trace


def _consortium_world(max_depth_world: bool = False):
    w = World()
    w.acct(A)
    w.acct(OC)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, OC, 6000.0, txn_id="AC")
    w.txn(20, OC, None, 5000.0, Channel.ATM, txn_id="C-ATM")
    w.txn(30, A, None, 1000.0, Channel.ATM, txn_id="A-ATM")
    return w.ledger()


@pytest.mark.parametrize("plan_follow", [True, False])
def test_plan_reads_consortium_following_from_the_trace(plan_follow):
    ledger = _consortium_world()
    plan_cfg = WapsiConfig(trace=TraceConfig(follow_consortium=plan_follow))

    off = trace_case(ledger, [N1], TraceConfig(follow_consortium=False))
    dec = _decisions(build_hold_plan(off, roles({A: Role.MULE}), ledger, plan_cfg))
    assert dec[A].forward_hold_amount == 1000.0  # the other-bank money is requested, not forward-held
    assert dec[OC].disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST

    # OC dropped by the depth limit: nothing about OC is in the trace's accounts
    for follow, forward in ((False, 1000.0), (True, 7000.0)):
        cut = trace_case(ledger, [N1], TraceConfig(follow_consortium=follow, max_depth=1))
        assert OC not in cut.accounts
        d = _decisions(build_hold_plan(cut, roles({A: Role.MULE}), ledger, plan_cfg))[A]
        assert d.forward_hold_amount == forward, follow
        assert forward_hold_vectors(cut, ledger.home_bank_id()) == {A: {N1: forward}}
