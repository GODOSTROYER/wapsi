"""Apportionment rules on small hand-computed worlds (PRO_RATA, FIFO, LIFO)."""

from __future__ import annotations

import pytest
from graph_worlds import World, parts_sum

from wapsi.config import TraceConfig
from wapsi.graph import trace_case
from wapsi.models import ApportionmentRule, Channel
from wapsi.money import eq

N = "91000000000001"
X, Y, Z, V = "HB01-X", "HB01-Y", "HB01-Z", "OB02-V"


def _world():
    """
    X opens with 5,000 of its own money.
      t0   V -> X   10,000  (victim transfer, complaint N)
      t10  X -> Y    6,000
      t20  cash deposit into X 2,000
      t30  X -> ATM  9,500
      t40  Y -> Z    3,000
    """
    w = World()
    w.acct(X, opening=5000.0)
    w.acct(Y)
    w.acct(Z)
    w.seed(N, 0, V, X, 10000.0)
    w.txn(10, X, Y, 6000.0, txn_id="XY")
    w.txn(20, None, X, 2000.0, Channel.CASH_DEPOSIT, txn_id="DEP")
    w.txn(30, X, None, 9500.0, Channel.ATM, txn_id="ATM")
    w.txn(40, Y, Z, 3000.0, txn_id="YZ")
    return w.ledger()


def _run(rule):
    trace = trace_case(_world(), [N], TraceConfig(rule=rule, min_edge_amount=0))
    edges = {e.txn_id: e.tainted_total for e in trace.edges}
    now = {a: s.tainted_now_total for a, s in trace.accounts.items() if s.layer > 0}
    return trace, edges, now


def test_fifo():
    # X lots [5,000 L][10,000 T]
    # XY 6,000: 5,000 L + 1,000 T                    -> edge 1,000; X [9,000 T]
    # DEP: X [9,000 T][2,000 L]
    # ATM 9,500: 9,000 T + 500 L                     -> cashed 9,000; X [1,500 L]
    # Y: one mixed lot 6,000 carrying 1,000 T; YZ 3,000 takes half -> 500; Y keeps 500, Z gets 500
    trace, edges, now = _run(ApportionmentRule.FIFO)
    assert trace.rule == ApportionmentRule.FIFO
    assert edges == {"T00001": 10000.0, "XY": 1000.0, "ATM": 9000.0, "YZ": 500.0}
    assert now == {X: 0.0, Y: 500.0, Z: 500.0}
    t = trace.totals
    assert (t.stolen, t.cashed_out, t.holdable, t.unlocated) == (10000.0, 9000.0, 1000.0, 0.0)
    assert trace.accounts[X].balance_now == 1500.0
    assert trace.accounts[Z].layer == 3


def test_lifo():
    # X lots [5,000 L][10,000 T]
    # XY 6,000 from the top: 6,000 T                 -> edge 6,000; X [5,000 L][4,000 T]
    # DEP: X [5,000 L][4,000 T][2,000 L]
    # ATM 9,500: 2,000 L + 4,000 T + 3,500 L         -> cashed 4,000; X [1,500 L]
    # Y [6,000 T]; YZ 3,000 T                        -> Y 3,000, Z 3,000
    trace, edges, now = _run(ApportionmentRule.LIFO)
    assert edges == {"T00001": 10000.0, "XY": 6000.0, "ATM": 4000.0, "YZ": 3000.0}
    assert now == {X: 0.0, Y: 3000.0, Z: 3000.0}
    t = trace.totals
    assert (t.stolen, t.cashed_out, t.holdable, t.unlocated) == (10000.0, 4000.0, 6000.0, 0.0)


def test_pro_rata():
    # X: B 15,000, T 10,000
    # XY 6,000 * 10,000/15,000 = 4,000               -> X B 9,000 T 6,000
    # DEP: X B 11,000 T 6,000
    # ATM 9,500 * 6,000/11,000 = 5,181.818...        -> X B 1,500 T 818.18
    # Y B 6,000 T 4,000; YZ 3,000 -> 2,000           -> Y 2,000, Z 2,000
    trace, edges, now = _run(ApportionmentRule.PRO_RATA)
    assert edges == {"T00001": 10000.0, "XY": 4000.0, "ATM": 5181.82, "YZ": 2000.0}
    assert now == {X: 818.18, Y: 2000.0, Z: 2000.0}
    t = trace.totals
    assert (t.cashed_out, t.holdable, t.unlocated) == (5181.82, 4818.18, 0.0)
    assert eq(parts_sum(t), t.stolen)


def _two_victims():
    """
    X opens empty.
      t0  Va -> X 1,000 (complaint A)
      t5  Vb -> X 3,000 (complaint B)
      t10 X  -> Y 2,000
    """
    w = World()
    w.acct(X)
    w.acct(Y)
    w.seed("A", 0, "OB02-VA", X, 1000.0)
    w.seed("B", 5, "OB02-VB", X, 3000.0)
    w.txn(10, X, Y, 2000.0, txn_id="XY")
    return w.ledger()


@pytest.mark.parametrize(
    ("rule", "edge", "left"),
    [
        # FIFO: the older lot (A 1,000) leaves first, then 1,000 of B
        (ApportionmentRule.FIFO, {"A": 1000.0, "B": 1000.0}, {"B": 2000.0}),
        # LIFO: the newest lot (B) leaves first
        (ApportionmentRule.LIFO, {"B": 2000.0}, {"A": 1000.0, "B": 1000.0}),
        # PRO_RATA: 2,000 * 1/4 and 2,000 * 3/4
        (ApportionmentRule.PRO_RATA, {"A": 500.0, "B": 1500.0}, {"A": 500.0, "B": 1500.0}),
    ],
)
def test_multi_victim_vectors(rule, edge, left):
    trace = trace_case(_two_victims(), ["A", "B"], TraceConfig(rule=rule))
    xy = next(e for e in trace.edges if e.txn_id == "XY")
    assert xy.tainted == edge
    assert trace.accounts[X].tainted_now == left
    assert trace.accounts[Y].tainted_now == edge
    assert trace.victim_seed == {"A": 1000.0, "B": 3000.0}
    assert eq(parts_sum(trace.totals), 4000.0)


def test_fifo_lots_merge_legit_credits():
    # many small legit credits are merged into one lot; the tainted lot still leaves first under FIFO
    w = World()
    w.acct(X)
    w.seed(N, 0, V, X, 1000.0)
    for i in range(200):
        w.txn(1 + i * 0.01, None, X, 10.0, Channel.CASH_DEPOSIT)
    w.txn(10, X, None, 1500.0, Channel.ATM, txn_id="ATM")
    trace = trace_case(w.ledger(), [N], TraceConfig(rule=ApportionmentRule.FIFO))
    assert trace.totals.cashed_out == 1000.0
    assert trace.accounts[X].tainted_now_total == 0.0
    assert trace.accounts[X].balance_now == 1500.0
