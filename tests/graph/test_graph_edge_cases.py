"""Edge cases of the money-trail engine: cycles, limits, consortium, overdrafts, pruning, merchants, flags."""

from __future__ import annotations

import pytest
from graph_worlds import World, parts_sum, roles

from wapsi.config import TraceConfig, WapsiConfig
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.expand import expand_candidates
from wapsi.models import (
    AccountType,
    ApportionmentRule,
    Channel,
    Disposition,
    RestorationProcess,
    Role,
    StrGround,
    TerminalKind,
)
from wapsi.money import eq

N1, N2 = "92000000000001", "92000000000002"
V1, V2 = "OB02-V1", "OB02-V2"
A, B, C, D, E = "HB01-A", "HB01-B", "HB01-C", "HB01-D", "HB01-E"
OC = "OB03-C"


def _plan(ledger, trace, role_map=None, cfg=None):
    return build_hold_plan(trace, roles(role_map or {}), ledger, cfg or WapsiConfig())


def _decisions(plan):
    return {d.account_id: d for d in plan.decisions}


# --------------------------------------------------------------------------- cycles and layers


def test_cycle_a_b_a():
    # A: B 20,000 T 10,000 -> A->B 4,000 carries 2,000 -> A 16,000/8,000, B 4,000/2,000
    # B->A 1,000 carries 500 -> B 3,000/1,500, A 17,000/8,500
    # A->ATM 2,000 carries 1,000 -> A 15,000/7,500
    w = World()
    w.acct(A, opening=10000.0)
    w.acct(B)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, B, 4000.0, txn_id="AB")
    w.txn(20, B, A, 1000.0, txn_id="BA")
    w.txn(30, A, None, 2000.0, Channel.ATM, txn_id="ATM")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    t = trace.totals
    assert (t.holdable, t.cashed_out, t.unlocated) == (9000.0, 1000.0, 0.0)
    assert trace.accounts[A].layer == 1 and trace.accounts[B].layer == 2
    edges = {e.txn_id: e for e in trace.edges}
    assert (edges["AB"].tainted_total, edges["BA"].tainted_total, edges["ATM"].tainted_total) == (
        2000.0,
        500.0,
        1000.0,
    )
    assert edges["BA"].depth == 1  # depth = layer of the receiving side
    assert edges["ATM"].depth == 2  # cash-out: sender layer + 1
    assert trace.accounts[A].tainted_received_total == 10500.0
    assert trace.accounts[A].tainted_sent_total == 3000.0
    assert trace.accounts[A].parents == [B, V1]
    plan = _plan(ledger, trace, {A: Role.MULE, B: Role.MULE})
    dec = _decisions(plan)
    assert (
        dec[A].hold_amount == 7500.0 and dec[A].forward_hold_amount == 0.0
    )  # balance 15,000 >= 10,500 disputed
    assert dec[B].hold_amount == 1500.0


def test_ping_pong_terminates():
    w = World()
    w.acct(A)
    w.acct(B)
    w.seed(N1, 0, V1, A, 5000.0)
    for i in range(30):
        frm, to = (A, B) if i % 2 == 0 else (B, A)
        w.txn(1 + i, frm, to, 5000.0)
    trace = trace_case(w.ledger(), [N1], TraceConfig())
    assert trace.accounts[A].tainted_now_total == 5000.0  # 30 hops: the money ends in A
    assert trace.totals.holdable == 5000.0 and trace.totals.unlocated == 0.0


def test_time_respecting_layers_and_reexpansion():
    # B first gets money via C at t10 (layer 3), sends to D at t20, then gets money directly from A at t30 (layer 2)
    w = World()
    for x in (A, B, C, D):
        w.acct(x)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(5, A, C, 4000.0, txn_id="AC")
    w.txn(10, C, B, 4000.0, txn_id="CB")
    w.txn(20, B, D, 1000.0, txn_id="BD")
    w.txn(30, A, B, 6000.0, txn_id="AB")
    trace = trace_case(w.ledger(), [N1], TraceConfig())
    acc = trace.accounts
    assert acc[D].tainted_now_total == 1000.0  # found only because B was re-expanded from t10
    assert acc[B].tainted_now_total == 9000.0
    assert (acc[A].layer, acc[C].layer, acc[B].layer, acc[D].layer) == (1, 2, 2, 4)
    assert {e.txn_id: e.depth for e in trace.edges}["BD"] == 4


# --------------------------------------------------------------------------- limits


def _chain():
    # V1 -> A 10,000 -> B 10,000 -> C 10,000 -> D 4,000
    w = World()
    for x in (A, B, C, D):
        w.acct(x)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, B, 10000.0, txn_id="AB")
    w.txn(20, B, C, 10000.0, txn_id="BC")
    w.txn(30, C, D, 4000.0, txn_id="CD")
    return w.ledger()


def test_full_chain_is_traced():
    trace = trace_case(_chain(), [N1], TraceConfig())
    assert trace.accounts[C].tainted_now_total == 6000.0
    assert trace.accounts[D].tainted_now_total == 4000.0
    assert trace.accounts[D].layer == 4
    assert trace.totals.unlocated == 0.0 and trace.warnings == []


def test_max_depth_truncation_is_unlocated():
    trace = trace_case(_chain(), [N1], TraceConfig(max_depth=2))
    assert set(trace.accounts) == {V1, A, B}
    assert trace.totals.unlocated == 10000.0 and trace.totals.holdable == 0.0
    assert any("Depth limit" in w for w in trace.warnings)
    bc = next(e for e in trace.edges if e.txn_id == "BC")
    assert bc.depth == 3 and bc.tainted_total == 10000.0  # off-trail edges stay visible


def test_first_layer_forward_hold_covers_unlocated_money():
    ledger = _chain()
    trace = trace_case(ledger, [N1], TraceConfig(max_depth=1))
    assert set(trace.accounts) == {V1, A}
    cfg = WapsiConfig(trace=TraceConfig(max_depth=1))
    d = _decisions(_plan(ledger, trace, {A: Role.MULE}, cfg))[A]
    assert d.disposition == Disposition.FORWARD_HOLD
    assert d.hold_amount == 0.0 and d.forward_hold_amount == 10000.0
    assert d.reason_codes == ["FORWARD_HOLD", "FORWARD_HOLD_FIRST_LAYER"]
    assert d.citation_ids == ["SOP-9-1-I", "SOP-DEF-FIRST-LAYER"]
    assert d.per_victim == {}


def test_max_accounts_limit():
    w = World()
    w.acct(A)
    bs = [w.acct(f"HB01-B{i}") for i in range(5)]
    w.seed(N1, 0, V1, A, 10000.0)
    for i, b in enumerate(bs):
        w.txn(10 + i, A, b, 2000.0)
    trace = trace_case(w.ledger(), [N1], TraceConfig(max_accounts=3))
    assert len(trace.accounts) == 1 + 3  # victim + A + two B accounts
    assert trace.totals.unlocated == 6000.0 and trace.totals.holdable == 4000.0
    assert any("Account limit" in w for w in trace.warnings)


# --------------------------------------------------------------------------- other banks


def _consortium_world():
    # V1 -> A 10,000; A -> C(OB03) 6,000; C -> ATM 5,000 (other-bank, consortium only); A -> ATM 1,000
    w = World()
    w.acct(A)
    w.acct(OC)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, OC, 6000.0, txn_id="AC")
    w.txn(20, OC, None, 5000.0, Channel.ATM, txn_id="C-ATM")
    w.txn(30, A, None, 1000.0, Channel.ATM, txn_id="A-ATM")
    return w.ledger()


def test_follow_consortium_on():
    trace = trace_case(_consortium_world(), [N1], TraceConfig())
    t = trace.totals
    assert (t.holdable, t.cashed_out, t.outbound, t.unlocated) == (3000.0, 6000.0, 1000.0, 0.0)
    assert trace.accounts[OC].terminal is None
    assert {e.txn_id: e.via_consortium for e in trace.edges}["C-ATM"] is True


def test_follow_consortium_off_is_outbound():
    ledger = _consortium_world()
    cfg = TraceConfig(follow_consortium=False)
    trace = trace_case(ledger, [N1], cfg)
    t = trace.totals
    assert (t.holdable, t.cashed_out, t.outbound, t.unlocated) == (3000.0, 1000.0, 6000.0, 0.0)
    assert trace.accounts[OC].terminal == TerminalKind.OTHER_BANK
    assert "C-ATM" not in {e.txn_id for e in trace.edges}
    plan = _plan(ledger, trace, {A: Role.MULE, OC: Role.MULE}, WapsiConfig(trace=cfg))
    dec = _decisions(plan)
    assert dec[OC].disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST
    assert (dec[OC].hold_amount, dec[OC].tainted_amount) == (0.0, 6000.0)
    assert dec[OC].per_victim == {N1: 6000.0}
    assert dec[A].forward_hold_amount == 1000.0  # only the ATM money; the outbound money is requested instead
    assert plan.total_recoverable == 3000.0 + 6000.0
    assert plan.baseline_freeze_accounts == 1  # other-bank accounts are not part of the baseline


def test_unfollowed_other_bank_beyond_depth_is_outbound():
    ledger = _consortium_world()
    cfg = TraceConfig(follow_consortium=False, max_depth=1)
    trace = trace_case(ledger, [N1], cfg)
    assert OC not in trace.accounts
    assert trace.totals.outbound == 6000.0 and trace.totals.unlocated == 0.0
    d = _decisions(_plan(ledger, trace, {A: Role.MULE}, WapsiConfig(trace=cfg)))[A]
    assert d.forward_hold_amount == 1000.0


def test_other_bank_pass_through_accounts():
    # followed other-bank account that cashed everything out -> CASHED_OUT; one that sent it all home -> NO_ACTION
    w = World()
    w.acct(A)
    w.acct("OB03-P")
    w.acct("OB03-Q")
    w.acct(B)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(5, A, "OB03-P", 3000.0)
    w.txn(6, "OB03-P", None, 3000.0, Channel.CRYPTO, txn_id="CRY")
    w.txn(7, A, "OB03-Q", 7000.0)
    w.txn(8, "OB03-Q", B, 7000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.totals.cashed_out == 3000.0 and trace.totals.holdable == 7000.0
    dec = _decisions(
        _plan(ledger, trace, {A: Role.MULE, "OB03-P": Role.MULE, "OB03-Q": Role.MULE, B: Role.MULE})
    )
    assert dec["OB03-P"].disposition == Disposition.CASHED_OUT
    assert dec["OB03-Q"].disposition == Disposition.NO_ACTION
    # A passed everything on to traced accounts: nothing to hold, nothing to forward-hold
    assert dec[A].disposition == Disposition.NO_ACTION and dec[A].forward_hold_amount == 0.0
    assert plan_str_ground(ledger, trace) == StrGround.PROCEEDS_OF_CRIME
    assert dec[B].hold_amount == 7000.0


def plan_str_ground(ledger, trace):
    return _plan(ledger, trace, {a: Role.MULE for a, st in trace.accounts.items() if st.layer}).str_ground


# --------------------------------------------------------------------------- overdrafts


@pytest.mark.parametrize("rule", list(ApportionmentRule))
def test_overdraft_clamp(rule):
    # A: +10,000 (N1); A -> B 12,000 (overdraft, all taint leaves; A = -2,000)
    # +5,000 (N2): 2,000 repays the overdraft (unlocated), 3,000 stays tainted
    w = World()
    w.acct(A)
    w.acct(B)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, B, 12000.0, txn_id="AB")
    w.seed(N2, 20, V2, A, 5000.0)
    trace = trace_case(w.ledger(), [N1, N2], TraceConfig(rule=rule))
    t = trace.totals
    assert (t.stolen, t.holdable, t.unlocated) == (15000.0, 13000.0, 2000.0)
    assert trace.accounts[A].tainted_now == {N2: 3000.0}
    assert trace.accounts[A].balance_now == 3000.0
    assert trace.accounts[B].tainted_now == {N1: 10000.0}
    assert any("overdraft" in w for w in trace.warnings)
    assert any("repaid overdrawn" in w for w in trace.warnings)


def test_negative_opening_balance_absorbs_taint():
    w = World()
    w.acct(A, opening=-1000.0)
    w.seed(N1, 0, V1, A, 5000.0)
    trace = trace_case(w.ledger(), [N1], TraceConfig())
    assert trace.accounts[A].tainted_now_total == 4000.0
    assert trace.accounts[A].balance_now == 4000.0
    assert trace.totals.unlocated == 1000.0
    assert eq(parts_sum(trace.totals), 5000.0)


# --------------------------------------------------------------------------- pruning


def _dusty_world():
    # A has 10,00,000 of its own; the victim's 10,000 is ~0.99% of it, so onward edges carry < ₹100 of taint
    w = World()
    w.acct(A, opening=1_000_000.0)
    w.acct(B)
    w.acct(C)
    k = w.acct("HB01-K", opening=100.0, kind=AccountType.MERCHANT)
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, B, 5000.0, txn_id="AB")
    w.txn(20, A, None, 5000.0, Channel.ATM, txn_id="ATM")
    w.txn(30, B, C, 5000.0, txn_id="BC")
    w.txn(40, A, k, 5000.0, txn_id="AK")
    w.invoice(k, A, "AK", 5000.0, delivered=True)
    return w.ledger()


def test_min_edge_amount_pruning_keeps_totals_exact():
    ledger = _dusty_world()
    full = trace_case(ledger, [N1], TraceConfig(min_edge_amount=0))
    pruned = trace_case(ledger, [N1], TraceConfig(min_edge_amount=100))
    assert {e.txn_id for e in full.edges} == {"T00001", "AB", "ATM", "BC", "AK"}
    # A->B and B->C carry ~49.50 and are dropped; seed, cash-out and merchant edges are always kept
    assert {e.txn_id for e in pruned.edges} == {"T00001", "ATM", "AK"}
    assert pruned.totals == full.totals
    assert pruned.accounts == full.accounts
    assert full.accounts[B].tainted_received_total == 49.5
    # 9,851.49 (A) + 49.50 (C) holdable, 49.50 cashed out, 49.50 settled: the paisa lost to rounding is the
    # only unlocated money
    assert (full.totals.holdable, full.totals.cashed_out, full.totals.merchant_settled) == (
        9900.99,
        49.5,
        49.5,
    )
    assert full.totals.unlocated == 0.01
    assert parts_sum(full.totals) == 10000.0
    assert not any("unlocated" in x for x in full.warnings)
    p_full = _plan(ledger, full, {A: Role.MULE, B: Role.MULE, C: Role.MULE})
    p_pruned = _plan(ledger, pruned, {A: Role.MULE, B: Role.MULE, C: Role.MULE})
    assert p_full.model_dump() == p_pruned.model_dump()


# --------------------------------------------------------------------------- merchants and intermediaries


def test_merchant_undelivered_and_partial_delivery():
    w = World()
    w.acct(A)
    k = w.acct("HB01-K", opening=500.0, kind=AccountType.MERCHANT)
    s = w.acct("HB01-S")
    w.seed(N1, 0, V1, A, 10000.0)
    w.txn(10, A, k, 3000.0, txn_id="K1")
    w.txn(11, A, k, 1000.0, txn_id="K2")
    w.invoice(k, A, "K1", 3000.0, delivered=False)
    w.invoice(k, A, "K2", 1000.0, delivered=True)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    # not every tainted inflow was delivered: only the undelivered 3,000 is held in the pool (SOP 9.2)
    assert trace.totals.merchant_settled == 1000.0
    assert trace.totals.holdable == 9000.0  # A 6,000 + undelivered 3,000 at K
    d = _decisions(_plan(ledger, trace, {A: Role.MULE}))[k]
    assert d.role == Role.MERCHANT and d.disposition == Disposition.HOLD_IN_POOL
    assert d.hold_amount == 3000.0 and d.per_victim == {N1: 3000.0}
    assert d.tainted_amount == 4000.0
    assert d.citation_ids == ["SOP-9-2"]

    # the merchant pays its supplier 4,000 out of 4,500: a terminal (supplier never traced) keeps the disputed
    # money only up to its balance; the 3,500 shortfall leaves the trail, delivered and undelivered alike
    w.txn(20, k, s, 4000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert s not in trace.accounts
    assert trace.accounts[k].tainted_now_total == 500.0 and trace.accounts[k].balance_now == 500.0
    assert (trace.totals.merchant_settled, trace.totals.holdable, trace.totals.unlocated) == (
        125.0,
        6375.0,
        3500.0,
    )
    d = _decisions(_plan(ledger, trace, {A: Role.MULE}))[k]
    assert d.disposition == Disposition.HOLD_IN_POOL
    assert d.hold_amount == 375.0 and d.per_victim == {N1: 375.0}
    assert "HOLD_CAPPED_AT_BALANCE" not in d.reason_codes

    # a hand-built trace whose merchant holds more undelivered money than its balance: the hold is capped
    st = trace.accounts[k].model_copy(update={"balance_now": 100.0})
    capped = trace.model_copy(update={"accounts": {**trace.accounts, k: st}})
    d = _decisions(_plan(ledger, capped, {A: Role.MULE}))[k]
    assert d.hold_amount == 100.0 and d.per_victim == {N1: 100.0}
    assert "HOLD_CAPPED_AT_BALANCE" in d.reason_codes


def test_other_bank_merchant():
    w = World()
    w.acct(A)
    m1 = w.acct("OB03-M1", kind=AccountType.MERCHANT)
    m2 = w.acct("OB03-M2", kind=AccountType.MERCHANT)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, m1, 2000.0, txn_id="M1")
    w.txn(11, A, m2, 3000.0, txn_id="M2")
    w.invoice(m1, A, "M1", 2000.0, delivered=True)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert (trace.totals.merchant_settled, trace.totals.outbound) == (2000.0, 3000.0)
    dec = _decisions(_plan(ledger, trace, {A: Role.MULE}))
    assert dec[m1].disposition == Disposition.NO_HOLD_UPDATE_ONLY
    assert dec[m2].disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST and dec[m2].tainted_amount == 3000.0


def test_intermediary_holds_disputed_amount_only_even_if_classified_mule():
    w = World()
    w.acct(A)
    pool = w.acct("HB01-POOL", opening=1_000_000.0, kind=AccountType.NODAL)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, pool, 5000.0)
    w.txn(20, pool, "HB01-OUT", 900_000.0)
    w.acct("HB01-OUT")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert "HB01-OUT" not in trace.accounts  # terminals are not expanded and absorb the taint
    assert trace.totals.intermediary == 5000.0 and trace.totals.unlocated == 0.0
    plan = _plan(ledger, trace, {A: Role.MULE, pool: Role.MULE})
    d = _decisions(plan)[pool]
    # SOP 9.1(iii): hold the disputed amount only; no suspension or seizure of the pool account
    assert d.role == Role.INTERMEDIARY and d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY
    assert d.hold_amount == 5000.0 and d.tainted_amount == 5000.0 and d.per_victim == {N1: 5000.0}
    assert d.forward_hold_amount == 0.0 and not d.requires_human
    assert d.reason_codes == ["HOLD_AMOUNT_INTERMEDIARY", "NO_SUSPENSION_OR_SEIZURE"]
    assert d.citation_ids == ["SOP-9-1-III", "SOP-9-1", "SOP-ANNEX-I"]
    assert plan.baseline_freeze_amount == 105_000.0  # the pool's whole balance is what a freeze would hit
    assert plan.total_hold == 5000.0 and plan.total_recoverable == 5000.0
    assert plan.per_victim_recoverable == {N1: 5000.0}
    assert plan.over_hold_amount == 0.0


def _first_layer_pool(balance_after: float):
    """Victim pays straight into a home pool account (layer 1) whose complaint carries an FIR."""
    w = World()
    pool = w.acct("HB01-POOL", opening=0.0, kind=AccountType.POOL)
    w.seed(N1, 0, V1, pool, 10000.0, fir="FIR 7/2026")
    w.signal(pool, 5)
    if balance_after < 10000.0:
        w.acct("HB01-SETTLE")
        w.txn(10, pool, "HB01-SETTLE", 10000.0 - balance_after)
    ledger = w.ledger()
    return ledger, trace_case(ledger, [N1], TraceConfig()), pool


def test_first_layer_intermediary_is_never_forward_held_or_flagged():
    ledger, trace, pool = _first_layer_pool(2500.0)
    st = trace.accounts[pool]
    # the pool settled 7,500 out of 10,000: it keeps the disputed money only up to its balance
    assert st.layer == 1 and st.balance_now == 2500.0 and st.tainted_now_total == 2500.0
    assert trace.totals.intermediary == 2500.0 and trace.totals.unlocated == 7500.0
    # hand-built state: more disputed money than balance (the hold must be capped)
    st = st.model_copy(update={"tainted_now": {N1: 10000.0}, "tainted_now_total": 10000.0})
    trace = trace.model_copy(update={"accounts": {**trace.accounts, pool: st}})
    # even with a cash-out edge out of the pool on the trail, no forward hold is ever marked on it
    exit_edge = trace.edges[0].model_copy(
        update={
            "txn_id": "POOL-ATM",
            "from_account": pool,
            "to_account": None,
            "channel": Channel.ATM,
            "utr": "ATM999999999",
            "amount": 3000.0,
            "depth": 2,
            "tainted": {N1: 3000.0},
            "tainted_total": 3000.0,
        }
    )
    trace = trace.model_copy(update={"edges": [*trace.edges, exit_edge]})
    plan = _plan(ledger, trace, {pool: Role.MULE})
    d = _decisions(plan)[pool]
    assert d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY
    assert d.hold_amount == 2500.0 and d.per_victim == {N1: 2500.0}
    assert d.forward_hold_amount == 0.0 and plan.total_forward_hold == 0.0
    assert "FORWARD_HOLD_FIRST_LAYER" not in d.reason_codes
    assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes and not d.requires_human
    assert d.reason_codes == [
        "HOLD_AMOUNT_INTERMEDIARY",
        "NO_SUSPENSION_OR_SEIZURE",
        "HOLD_CAPPED_AT_BALANCE",
    ]
    assert not {"SOP-9-1-I", "BNSS-106", "SOP-DEF-FIRST-LAYER"} & set(d.citation_ids)


def test_intermediary_with_nothing_holdable_gets_no_action():
    ledger, trace, pool = _first_layer_pool(0.0)
    d = _decisions(_plan(ledger, trace))[pool]
    assert d.disposition == Disposition.NO_ACTION
    assert d.hold_amount == 0.0 and d.forward_hold_amount == 0.0 and d.per_victim == {}


def test_other_bank_intermediary_is_an_outbound_request():
    w = World()
    w.acct(A)
    pool = w.acct("OB03-POOL", opening=500_000.0, kind=AccountType.ESCROW)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, pool, 4000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    d = _decisions(_plan(ledger, trace, {A: Role.MULE}))[pool]
    assert d.role == Role.INTERMEDIARY and d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST
    assert d.hold_amount == 0.0 and d.tainted_amount == 4000.0 and d.forward_hold_amount == 0.0


# --------------------------------------------------------------------------- roles, flags, process


def test_unknown_role_requires_human():
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    d = build_hold_plan(trace, None, ledger, WapsiConfig()).decisions[0]
    assert d.role == Role.UNKNOWN and d.disposition == Disposition.HOLD_AMOUNT
    assert d.requires_human and "ROLE_UNKNOWN" in d.reason_codes
    assert d.reason_codes[0] == "HOLD_AMOUNT"


def test_innocent_holder_gets_no_forward_hold_and_no_str():
    w = World()
    w.acct(A)
    w.acct(B)
    w.seed(N1, 0, V1, A, 5000.0, fir="FIR 1/2026")
    w.signal(A, 5)
    w.txn(10, A, None, 4000.0, Channel.ATM)
    w.seed(N1, 20, V1, B, 1000.0)
    w.txn(30, B, None, 1000.0, Channel.ATM)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    plan = _plan(ledger, trace, {A: Role.INNOCENT_HOLDER, B: Role.LEGIT})
    dec = _decisions(plan)
    assert dec[A].disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
    assert dec[A].hold_amount == 1000.0 and dec[A].forward_hold_amount == 0.0
    assert not dec[A].requires_human and "ESCALATE_S106_CANDIDATE" not in dec[A].reason_codes
    assert dec[B].disposition == Disposition.CASHED_OUT and dec[B].hold_amount == 0.0
    assert plan.str_ground is None


def test_mule_cash_out_only_sets_str_ground():
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, None, 5000.0, Channel.CRYPTO)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.totals.cashed_out == 5000.0
    assert trace.edges[-1].depth == 2
    plan = _plan(ledger, trace, {A: Role.MULE})
    assert plan.str_ground == StrGround.PROCEEDS_OF_CRIME
    assert plan.decisions[0].disposition == Disposition.FORWARD_HOLD


@pytest.mark.parametrize(
    ("fir", "prior", "role", "flagged"),
    [
        ("FIR 9/2026", 2, Role.MULE, True),
        ("FIR 9/2026", 2, Role.UNKNOWN, True),
        ("FIR 9/2026", 1, Role.MULE, False),
        (None, 5, Role.MULE, False),
        ("FIR 9/2026", 5, Role.INNOCENT_HOLDER, False),
    ],
)
def test_s106_escalation_flag(fir, prior, role, flagged):
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0, fir=fir)
    w.signal(A, prior)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    d = _plan(ledger, trace, {A: role}).decisions[0]
    assert ("ESCALATE_S106_CANDIDATE" in d.reason_codes) is flagged
    assert d.disposition in (Disposition.HOLD_AMOUNT, Disposition.HOLD_AMOUNT_NO_ESCALATION)
    assert d.disposition != Disposition.ESCALATE_S106_CANDIDATE
    if flagged:
        assert d.requires_human and {"BNSS-106", "CASE-KARTIK-CHATUR"} <= set(d.citation_ids)


def test_process_p1_when_only_one_victim_is_recoverable():
    w = World()
    w.acct(A)
    w.acct(B)
    w.seed(N1, 0, V1, A, 1000.0)
    w.txn(5, A, None, 1000.0, Channel.ATM)
    w.seed(N2, 10, V2, B, 2000.0)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1, N2], TraceConfig())
    plan = _plan(ledger, trace, {A: Role.MULE, B: Role.MULE})
    assert plan.per_victim_recoverable == {N1: 0.0, N2: 2000.0}
    assert plan.restoration_process == RestorationProcess.P1
    assert all("SOP-11-P2" not in d.citation_ids for d in plan.decisions)


def test_money_returned_to_victim_is_unlocated_not_forward_held():
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, V1, 2000.0, txn_id="REFUND")
    ledger = w.ledger()
    trace = trace_case(ledger, [N1], TraceConfig())
    assert trace.totals.unlocated == 2000.0
    assert any("victim account" in x for x in trace.warnings)
    d = _plan(ledger, trace, {A: Role.MULE}).decisions[0]
    assert d.hold_amount == 3000.0 and d.forward_hold_amount == 0.0


def test_unknown_destination_account():
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(10, A, "HB01-GHOST", 2000.0)
    trace = trace_case(w.ledger(), [N1], TraceConfig())
    assert trace.totals.unlocated == 2000.0
    assert any("not in the ledger" in x for x in trace.warnings)
    assert any("not found in the ledger" in x for x in trace.warnings)


# --------------------------------------------------------------------------- inputs


def test_inputs_and_defaults():
    w = World()
    w.acct(A)
    w.seed(N1, 0, V1, A, 5000.0)
    w.txn(1, A, A, 100.0)  # self-transfer is ignored
    w.complaints.append(w.complaints[0].model_copy(update={"ncrp_id": N2}))  # complaint without transactions
    ledger = w.ledger()
    with pytest.raises(ValueError):
        trace_case(ledger, [], TraceConfig())
    trace = trace_case(ledger, [N1, N1, N2], case_id="CASE-X")
    assert trace.case_id == "CASE-X" and trace.ncrp_ids == [N1, N2]
    assert trace.victim_seed == {N1: 5000.0, N2: 0.0}
    assert any(N2 in x and "no linked victim transactions" in x for x in trace.warnings)
    assert len(trace.edges) == 1 and trace.accounts[A].tainted_now_total == 5000.0


def test_shared_seed_and_window_extension():
    w = World()
    w.acct(A)
    tid = w.seed(N1, 0, V1, A, 5000.0)
    w.complaint_txns.append((N2, tid))
    w.complaints.append(w.complaints[0].model_copy(update={"ncrp_id": N2}))
    w.seed(N2, 4 * 24 * 60, V1, A, 1000.0)  # four days later
    w.txn(4 * 24 * 60 + 10, A, None, 6000.0, Channel.ATM)
    ledger = w.ledger()
    trace = trace_case(ledger, [N1, N2], TraceConfig())
    assert trace.victim_seed == {N1: 5000.0, N2: 1000.0}
    assert any("attributed to" in x for x in trace.warnings)
    assert any("window was extended" in x for x in trace.warnings)
    assert trace.totals.cashed_out == 6000.0
    exp = expand_candidates(ledger, [N1, N2], TraceConfig())
    assert (exp.window_end - exp.seeds[-1].ts).total_seconds() == 72 * 3600
