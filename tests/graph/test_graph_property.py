"""Randomized invariants on small seeded worlds (50 iterations each) plus a performance sanity check.

The second generator adds same-second bursts (transfer chains whose txn ids sort against the money's path) and
victim-to-victim chains (a victim paying into another victim's home-bank account, which then reports its own
transfer).
"""

from __future__ import annotations

import dataclasses
import random
import time

import pytest
from graph_worlds import HOME, World, parts_sum, roles

from wapsi import citations
from wapsi.config import TraceConfig, WapsiConfig
from wapsi.graph import build_hold_plan, trace_case
from wapsi.graph.common import PASS_THROUGH_TERMINALS, terminal_kind
from wapsi.graph.expand import expand_candidates
from wapsi.graph.taint import simulate
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    INTERMEDIARY_ACCOUNT_TYPES,
    AccountType,
    ApportionmentRule,
    Channel,
    Disposition,
    Role,
    TerminalKind,
)

_TYPES = [AccountType.INDIVIDUAL] * 7 + [AccountType.BUSINESS, AccountType.MERCHANT, AccountType.POOL]


def _random_world(rng: random.Random, bursts: bool = False):
    w = World()
    accts = []
    for i in range(rng.randint(3, 9)):
        accts.append(w.acct(f"{HOME}-H{i}", opening=_opening(rng), kind=rng.choice(_TYPES)))
    for i in range(rng.randint(1, 4)):
        bank = rng.choice(["OB02", "OB03"])
        accts.append(w.acct(f"{bank}-O{i}", opening=_opening(rng), kind=rng.choice(_TYPES)))
    serial = iter(range(10**6))

    def tid() -> str | None:
        # bursts: txn ids unrelated to the order of the money
        return f"R{rng.randrange(10**4):04d}-{next(serial)}" if bursts else None

    burst_minutes = [rng.choice([0.0, 30.0, 60.0, 90.0]) for _ in range(3)] if bursts else []

    def minute(lo: float, hi: float) -> float:
        if bursts and rng.random() < 0.5:
            return rng.choice(burst_minutes)
        return rng.uniform(lo, hi)

    ncrp_ids = []
    victims = []
    for v in range(rng.randint(1, 3)):
        n = f"9300000000{v:04d}"
        ncrp_ids.append(n)
        victim = f"OB02-V{v}"
        if bursts and v > 0 and rng.random() < 0.6:
            # victim-to-victim chain: the previous victim pays into this victim's home account
            victim = w.acct(f"{HOME}-V{v}", opening=_opening(rng))
            to = victim
            amount = round(rng.uniform(1000, 100000), 2)
            w.seed(ncrp_ids[v - 1], minute(0, 60), victims[-1], to, amount, txn_id=tid())
        victims.append(victim)
        for _ in range(rng.randint(1, 2)):
            to = rng.choice(accts[:3] + [accts[-1]] + victims[:-1] * bursts)
            amount = round(rng.uniform(1000, 100000), 2)
            t = w.seed(
                n, minute(0, 120), victim, to, amount, fir=rng.choice([None, "FIR 1/2026"]), txn_id=tid()
            )
            if w.accounts[to].account_type == AccountType.MERCHANT and rng.random() < 0.9:
                w.invoice(to, victim, t, amount, delivered=rng.random() < 0.85)
    endpoints = accts * 3 + victims + ["HB01-GHOST"]
    if bursts:
        # same-second chains along random paths
        for _ in range(rng.randint(1, 4)):
            path = rng.sample(accts + victims, k=min(len(accts + victims), rng.randint(2, 5)))
            at_minute = rng.choice(burst_minutes)
            amount = round(rng.uniform(500, 80000), 2)
            for frm, to in zip(path, path[1:], strict=False):
                w.txn(at_minute, frm, to, amount, Channel.IMPS, txn_id=tid())
            if rng.random() < 0.5:
                w.txn(at_minute, path[-1], None, round(amount / 2, 2), Channel.ATM, txn_id=tid())
    for _ in range(rng.randint(30, 90)):
        at_minute = minute(-60, 5000)
        roll = rng.random()
        amount = round(rng.uniform(50, 60000), 2)
        if roll < 0.1:
            w.txn(at_minute, None, rng.choice(accts), amount, Channel.CASH_DEPOSIT, txn_id=tid())
        elif roll < 0.25:
            w.txn(
                at_minute,
                rng.choice(accts),
                None,
                amount,
                rng.choice([Channel.ATM, Channel.CRYPTO]),
                txn_id=tid(),
            )
        else:
            frm, to = rng.choice(accts + victims), rng.choice(endpoints)
            t = w.txn(at_minute, frm, to, amount, rng.choice([Channel.UPI, Channel.IMPS]), txn_id=tid())
            if (
                to in w.accounts
                and w.accounts[to].account_type == AccountType.MERCHANT
                and rng.random() < 0.9
            ):
                w.invoice(to, frm, t, amount, delivered=rng.random() < 0.85)
    for a in accts:
        if rng.random() < 0.5:
            w.signal(a, rng.randint(0, 3))
    role_map = {a: rng.choice(list(Role)) for a in accts if rng.random() < 0.8}
    return w.ledger(), ncrp_ids, role_map


def _opening(rng):
    roll = rng.random()
    if roll < 0.4:
        return 0.0
    if roll < 0.9:
        return round(rng.uniform(0, 80000), 2)
    return -round(rng.uniform(0, 3000), 2)


def _check_trace(trace, cfg):
    t = trace.totals
    assert abs(parts_sum(t) - t.stolen) < 1e-6
    for part in (t.holdable, t.cashed_out, t.outbound, t.intermediary, t.merchant_settled):
        assert part >= 0
    tol = 0.01 * (sum(len(s.tainted_now) + 1 for s in trace.accounts.values()) + len(trace.edges) + 1)
    assert t.unlocated >= -tol
    assert abs(sum(trace.victim_seed.values()) - t.stolen) < 0.01 * (len(trace.victim_seed) + 1)
    # no victim's money is ever counted twice
    held: dict[str, float] = {}
    for st in trace.accounts.values():
        for n, x in st.tainted_now.items():
            held[n] = held.get(n, 0.0) + x
    for n, x in held.items():
        assert x <= trace.victim_seed[n] + tol, n
    for acct, st in trace.accounts.items():
        assert set(st.tainted_now) <= set(trace.ncrp_ids)
        assert all(v > 0 for v in st.tainted_now.values())
        if st.layer == 0:
            assert st.tainted_received_total == 0 and st.tainted_now == {}
            continue
        k = len(st.tainted_now) + 1
        assert st.tainted_received_total > 0
        assert st.tainted_now_total <= st.tainted_received_total + 0.01 * k
        assert st.tainted_now_total + st.tainted_sent_total <= st.tainted_received_total + 0.01 * k
        absorbing = st.terminal == TerminalKind.OTHER_BANK or (
            st.terminal in PASS_THROUGH_TERMINALS and not st.is_home_bank and not cfg.follow_consortium
        )
        if absorbing:
            assert st.tainted_sent_total == 0
        else:  # rule-based accounts and pass-through terminals never hold more taint than money
            assert st.tainted_now_total <= max(st.balance_now, 0.0) + 0.01 * k, acct
        assert st.parents and st.first_tainted_at is not None
    for e in trace.edges:
        assert 0 < e.tainted_total <= e.amount + 0.01 * len(e.tainted)
        assert e.depth >= 1
        assert trace.window_start <= e.ts <= trace.window_end


def _check_plan(plan, trace, ledger, cfg):
    home = ledger.home_bank_id()
    assert plan.over_hold_amount == 0.0
    assert plan.total_recoverable <= trace.totals.stolen + 0.01
    keys = [(d.layer, d.account_id) for d in plan.decisions]
    assert keys == sorted(keys)
    expected = {a for a, s in trace.accounts.items() if s.layer > 0 and s.tainted_received_total > 0}
    assert {d.account_id for d in plan.decisions} == expected
    for n, x in plan.per_victim_recoverable.items():
        assert 0 <= x <= trace.victim_seed.get(n, 0.0) + 0.01 * (len(plan.decisions) + 1), n
    for d in plan.decisions:
        st = trace.accounts[d.account_id]
        assert d.reason_codes[0] == d.disposition.value
        assert all(v > 0 for v in d.per_victim.values())
        if d.disposition in HOLDING_DISPOSITIONS:  # a holding disposition always holds money
            assert d.hold_amount > 0
        if d.disposition == Disposition.HOLD_IN_POOL:
            assert st.account_type == AccountType.MERCHANT or d.role == Role.MERCHANT
        assert d.disposition != Disposition.ESCALATE_S106_CANDIDATE
        assert all(citations.is_known(c) for c in d.citation_ids)
        assert d.hold_amount <= d.tainted_amount + 0.005
        assert d.hold_amount <= max(d.balance_now, 0.0) + 0.005
        if d.hold_amount > 0:
            assert d.disposition in HOLDING_DISPOSITIONS and st.bank_id == home
            assert abs(sum(d.per_victim.values()) - d.hold_amount) < 0.005
        if d.forward_hold_amount > 0:
            assert d.layer == 1 and st.bank_id == home
            assert "FORWARD_HOLD_FIRST_LAYER" in d.reason_codes
        if st.account_type in INTERMEDIARY_ACCOUNT_TYPES or d.role == Role.INTERMEDIARY:
            # SOP 9.1(iii) / 9.1(i): amount-specific hold only; no forward hold, never an s.106 flag
            assert d.role == Role.INTERMEDIARY
            assert d.forward_hold_amount == 0 and "FORWARD_HOLD_FIRST_LAYER" not in d.reason_codes
            assert "ESCALATE_S106_CANDIDATE" not in d.reason_codes and not d.requires_human
            if st.bank_id == home:
                assert d.disposition in (Disposition.HOLD_AMOUNT_INTERMEDIARY, Disposition.NO_ACTION)
                assert (d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY) == (d.hold_amount > 0)
            else:
                assert d.disposition in (
                    Disposition.OUTBOUND_CFCFRMS_REQUEST,
                    Disposition.CASHED_OUT,
                    Disposition.NO_ACTION,
                )
        if d.disposition == Disposition.HOLD_AMOUNT_INTERMEDIARY:
            assert d.role == Role.INTERMEDIARY and "NO_SUSPENSION_OR_SEIZURE" in d.reason_codes
            assert d.citation_ids[0] == "SOP-9-1-III"
        if d.disposition == Disposition.OUTBOUND_CFCFRMS_REQUEST:
            assert d.hold_amount == 0 and st.bank_id != home and d.tainted_amount > 0
        if "ESCALATE_S106_CANDIDATE" in d.reason_codes:
            assert d.requires_human and d.disposition in (Disposition.HOLD_AMOUNT, Disposition.FORWARD_HOLD)
            assert d.role in (Role.MULE, Role.UNKNOWN)
    assert abs(sum(plan.per_victim_recoverable.values()) - plan.total_recoverable) < 0.01 * (
        len(plan.decisions) + 1
    )


@pytest.mark.parametrize(("seed", "bursts"), [(s, False) for s in range(50)] + [(s, True) for s in range(50)])
def test_random_worlds_conserve_money(seed, bursts):
    rng = random.Random(seed + 5000 * bursts)
    ledger, ncrp_ids, role_map = _random_world(rng, bursts=bursts)
    cfg = TraceConfig(
        rule=rng.choice(list(ApportionmentRule)),
        follow_consortium=rng.random() < 0.6,
        max_depth=rng.randint(1, 6),
        max_accounts=rng.choice([2, 4, 500]),
        min_edge_amount=rng.choice([0.0, 100.0, 5000.0]),
    )
    trace = trace_case(ledger, ncrp_ids, cfg)
    _check_trace(trace, cfg)

    # the published unlocated amount is the explicitly untraced money plus paise-level rounding residue
    exp = expand_candidates(ledger, ncrp_ids, cfg)
    sim = simulate(ledger, exp, cfg)
    tol = 0.01 * (sum(len(s.tainted_now) + 1 for s in trace.accounts.values()) + len(trace.edges) + 1)
    assert abs(sim.unlocated_flow - trace.totals.unlocated) <= tol

    wcfg = WapsiConfig(trace=cfg)
    plan = build_hold_plan(trace, roles(role_map), ledger, wcfg)
    _check_plan(plan, trace, ledger, wcfg)

    # determinism
    again = trace_case(ledger, ncrp_ids, cfg)
    assert again.model_dump(exclude={"computed_at"}) == trace.model_dump(exclude={"computed_at"})
    assert build_hold_plan(again, roles(role_map), ledger, wcfg) == plan


@pytest.mark.parametrize(("seed", "bursts"), [(s, False) for s in range(50)] + [(s, True) for s in range(50)])
def test_search_finds_every_reachable_account(seed, bursts):
    """With generous limits the BFS candidate set must give the same answer as simulating every account."""
    rng = random.Random(1000 + seed + 5000 * bursts)
    ledger, ncrp_ids, _ = _random_world(rng, bursts=bursts)
    cfg = TraceConfig(
        rule=rng.choice(list(ApportionmentRule)),
        follow_consortium=True,
        max_depth=100,
        max_accounts=10_000,
        min_edge_amount=0.0,
    )
    exp = expand_candidates(ledger, ncrp_ids, cfg)
    # every account, victims included (victim accounts are simulated and never terminals)
    everyone = ledger.all_accounts()
    victims = set(exp.victim_of)
    home = ledger.home_bank_id()
    brute = dataclasses.replace(
        exp,
        candidates=everyone,
        terminal={
            a: k
            for a, acct in everyone.items()
            if a not in victims
            and (k := terminal_kind(acct, home_bank_id=home, follow_consortium=True)) is not None
        },
        depth_limited=set(),
        dropped={},
        known_accounts=ledger.all_accounts(),
    )
    got = simulate(ledger, exp, cfg)
    ref = simulate(ledger, brute, cfg)
    assert got.totals == ref.totals
    assert got.accounts.keys() == ref.accounts.keys()
    for a, st in ref.accounts.items():
        assert got.accounts[a] == st, a
    assert got.edges == ref.edges


def _big_world(n_accounts=300, n_txns=20_000, seed=7):
    rng = random.Random(seed)
    w = World()
    # balances large enough that the random traffic never overdraws an account
    accts = [w.acct(f"{HOME}-P{i:04d}", opening=round(rng.uniform(2e6, 5e6), 2)) for i in range(n_accounts)]
    others = [w.acct(f"OB03-Q{i:03d}", opening=round(rng.uniform(2e6, 5e6), 2)) for i in range(30)]
    for v in range(3):
        w.seed(f"9400000000{v:04d}", v, f"OB02-V{v}", accts[v], 250000.0)
    pool = accts + others
    for _ in range(n_txns):
        minute = rng.uniform(0, 72 * 60)
        roll = rng.random()
        amount = round(rng.uniform(100, 20000), 2)
        if roll < 0.05:
            w.txn(minute, rng.choice(accts), None, amount, Channel.ATM)
        elif roll < 0.1:
            w.txn(minute, None, rng.choice(accts), amount, Channel.CASH_DEPOSIT)
        else:
            w.txn(minute, rng.choice(pool), rng.choice(pool), amount, Channel.UPI)
    return w.ledger(), [f"9400000000{v:04d}" for v in range(3)]


@pytest.mark.parametrize("rule", list(ApportionmentRule))
def test_performance_20k_transactions(rule):
    ledger, ncrp_ids = _big_world()
    cfg = TraceConfig(rule=rule)
    start = time.perf_counter()
    trace = trace_case(ledger, ncrp_ids, cfg)
    elapsed = time.perf_counter() - start
    assert trace.warnings == []
    # FIFO keeps the victims' money behind the large opening lots of the first-layer accounts
    assert len(trace.accounts) > (3 if rule == ApportionmentRule.FIFO else 300)
    assert abs(parts_sum(trace.totals) - trace.totals.stolen) < 1e-6
    assert abs(trace.totals.unlocated) < 1.0  # rounding residue only
    assert elapsed < 2.0, f"trace took {elapsed:.2f}s"
    start = time.perf_counter()
    build_hold_plan(trace, {}, ledger, WapsiConfig(trace=cfg))
    assert time.perf_counter() - start < 2.0
