"""Per-victim rounding: the money a plan says is recoverable for a victim never exceeds that victim's loss.

Each account's tainted money is rounded to paise on its own, so a victim whose money sits in several accounts could
be shown a paisa or two more recoverable than the victim lost (the "r9" finding: ₹20,000.02 recoverable for a
₹20,000.00 loss). ``wapsi.graph.holdplan.cap_recoverable_at_loss`` takes the excess back from the victim's largest
share; plans already within every victim's loss (showcase, golden sets) are unchanged.
"""

from __future__ import annotations

import random

import pytest
from graph_worlds import World, roles

from wapsi.config import TraceConfig, WapsiConfig
from wapsi.fixtures import showcase as sc
from wapsi.graph.holdplan import NOTE_ROUNDING_CAP, build_hold_plan, cap_recoverable_at_loss
from wapsi.graph.trace import trace_case
from wapsi.models import (
    AccountType,
    ApportionmentRule,
    Case,
    CaseBundle,
    Channel,
    Disposition,
    HoldDecision,
    Role,
)
from wapsi.timeutil import utcnow
from wapsi.verify import summarize, verify_plan


def _thirds_world():
    """V -> A 20,000; A (own 10,000) sends 10,000 each to B and C: A, B and C each hold 6,666.666... of it."""
    w = World()
    a, b, c = w.acct("HB01-1000000001", 10_000.0), w.acct("HB01-1000000002"), w.acct("HB01-1000000003")
    w.seed("N1", 0, "OB02-5000000001", a, 20_000.0)
    w.txn(5, a, b, 10_000.0)
    w.txn(6, a, c, 10_000.0)
    return w.ledger(), [a, b, c]


def test_recoverable_per_victim_never_exceeds_the_loss():
    ledger, (a, b, c) = _thirds_world()
    cfg = WapsiConfig()
    trace = trace_case(ledger, ["N1"], cfg.trace)
    assert trace.victim_seed == {"N1": 20_000.0}
    # each account's figure stays the correctly rounded amount it holds
    assert [trace.accounts[x].tainted_now["N1"] for x in (a, b, c)] == [6666.67, 6666.67, 6666.67]
    plan = build_hold_plan(trace, roles(dict.fromkeys((a, b, c), Role.MULE)), ledger, cfg)
    assert plan.per_victim_recoverable == {"N1": 20_000.0}  # was 20,000.01
    assert plan.total_hold == plan.total_recoverable == trace.totals.stolen == 20_000.0
    holds = {d.account_id: d for d in plan.decisions}
    # the paisa comes back from the largest share; ties go to the first decision in plan order (layer 1: A)
    assert {x: holds[x].hold_amount for x in (a, b, c)} == {a: 6666.66, b: 6666.67, c: 6666.67}
    assert holds[a].per_victim == {"N1": 6666.66} and NOTE_ROUNDING_CAP in holds[a].notes
    assert NOTE_ROUNDING_CAP not in holds[b].notes
    bundle = CaseBundle(case=Case(case_id="CASE-N1", ncrp_ids=["N1"], opened_at=utcnow()),
                        complaints=[ledger.complaint("N1")], trace=trace, plan=plan)
    report = verify_plan(bundle, ledger, cfg=cfg)
    assert report.passed, summarize(report)


def _dec(acct: str, disp: Disposition, amount: float, per: dict[str, float]) -> HoldDecision:
    outbound = disp == Disposition.OUTBOUND_CFCFRMS_REQUEST
    return HoldDecision(account_id=acct, bank_id=acct[:4], disposition=disp, hold_amount=0.0 if outbound else amount,
                        tainted_amount=amount, balance_now=amount, layer=1, role=Role.MULE, per_victim=per)


def test_cap_trims_outbound_requests_and_leaves_victims_within_their_loss():
    decisions = [
        _dec("HB01-1", Disposition.HOLD_AMOUNT, 100.01, {"N1": 50.0, "N2": 50.01}),
        _dec("OB03-1", Disposition.OUTBOUND_CFCFRMS_REQUEST, 50.01, {"N1": 50.01}),
        _dec("HB01-2", Disposition.NO_HOLD_UPDATE_ONLY, 0.0, {"N1": 9.0}),  # not recoverable: never counted
    ]
    out = cap_recoverable_at_loss(decisions, {"N1": 100.0, "N2": 50.01}, ["N1", "N2"])
    assert out[0] == decisions[0]  # N2 within its loss; N1's largest share is the outbound request
    assert out[1].tainted_amount == 50.0 and out[1].per_victim == {"N1": 50.0}
    assert out[2] == decisions[2]
    assert cap_recoverable_at_loss(decisions, {"N1": 200.0, "N2": 60.0}) is decisions  # nothing to trim


def test_cap_never_takes_an_amount_to_zero_and_skips_unknown_victims():
    decisions = [_dec("HB01-1", Disposition.HOLD_AMOUNT, 0.01, {"N1": 0.01}),
                 _dec("HB01-2", Disposition.HOLD_AMOUNT, 0.01, {"N1": 0.01})]
    assert cap_recoverable_at_loss(decisions, {"N1": 0.0}) is decisions  # a holding decision always holds money
    assert cap_recoverable_at_loss(decisions, {}) is decisions


def test_showcase_numbers_are_unchanged():
    ledger = sc.showcase_ledger()
    for world in (sc.SHOWCASE_1, sc.SHOWCASE_2):
        trace = trace_case(ledger, world["ncrp_ids"], TraceConfig())
        plan = build_hold_plan(trace, roles(world["roles"]), ledger, WapsiConfig())
        assert cap_recoverable_at_loss(plan.decisions, trace.victim_seed, trace.ncrp_ids) is plan.decisions
        assert not any(NOTE_ROUNDING_CAP in d.notes for d in plan.decisions)
    trace = trace_case(ledger, sc.SHOWCASE_1["ncrp_ids"], TraceConfig())
    plan = build_hold_plan(trace, roles(sc.SHOWCASE_1["roles"]), ledger, WapsiConfig())
    holds = {d.account_id: d.hold_amount for d in plan.decisions if d.hold_amount > 0}
    assert holds == {sc.A: 43_200.0, sc.B: 27_648.0, sc.D: 48_000.0, sc.L: 4_800.0, sc.P: 19_200.0}
    assert (plan.total_hold, plan.total_forward_hold, plan.total_recoverable) == (142_848.0, 96_000.0, 334_848.0)


def _random_world(rng: random.Random):
    w = World()
    home = [w.acct(f"HB01-10000000{i:02d}", rng.choice([0, 0, 500.0, 20_000.0])) for i in range(7)]
    other = [w.acct(f"OB03-40000000{i:02d}", rng.choice([0, 999.99])) for i in range(3)]
    pool = w.acct("HB01-9000000001", 1e6, AccountType.POOL)
    merchant = w.acct("HB01-3000000001", 100.0, AccountType.MERCHANT)
    for v in range(rng.randint(1, 3)):
        for _ in range(rng.randint(1, 2)):
            amount = rng.choice([10_000.0, 33_333.33, 7_777.77])
            w.seed(f"N{v}", rng.choice([0, 2, 4]), f"OB02-500000000{v}", rng.choice(home[:3]), amount)
    for _ in range(rng.randint(5, 30)):
        frm = rng.choice(home + other + [pool])
        to = rng.choice(home + other + [pool, merchant, None, "OB02-5000000000"])
        w.txn(rng.choice([1, 3, 5, 7, 9, 20, 30]), frm, to, round(rng.uniform(1, 15_000), 2),
              Channel.ATM if to is None else Channel.IMPS)
    return w, w.ledger()


@pytest.mark.parametrize("rule", list(ApportionmentRule))
def test_random_worlds_never_recover_more_than_a_victim_lost(rule):
    rng = random.Random(11)
    for _ in range(40):
        w, ledger = _random_world(rng)
        ids = sorted({c.ncrp_id for c in w.complaints})
        for follow in (True, False):
            cfg = WapsiConfig(trace=TraceConfig(rule=rule, follow_consortium=follow))
            trace = trace_case(ledger, ids, cfg.trace)
            plan = build_hold_plan(trace, roles(dict.fromkeys(trace.accounts, Role.MULE)), ledger, cfg)
            for n, x in plan.per_victim_recoverable.items():
                assert x <= trace.victim_seed.get(n, 0.0), (n, x, trace.victim_seed.get(n))
            assert round(plan.total_recoverable - trace.totals.stolen, 2) <= 0
            for d in plan.decisions:
                assert round(sum(d.per_victim.values()), 2) <= max(d.hold_amount, d.tainted_amount)
                if d.disposition in (Disposition.HOLD_AMOUNT, Disposition.HOLD_AMOUNT_NO_ESCALATION):
                    assert d.hold_amount > 0
