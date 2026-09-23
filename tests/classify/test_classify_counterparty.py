"""Counterparty-history features, their heuristic weights and the INNOCENT_HOLDER safety guard.

Scenario behind them (T6 triangulation): a fraudster pays a genuine seller with victim money. The seller is an
innocent holder (SOP Para 5(xii)): years old, an established base of past customers, turnover well above the
payment, and money going out only to its usual landlord / supplier. A mule is fresh or dormant, has no such
history and forwards to accounts it never dealt with.
"""

from datetime import timedelta

import pandas as pd
import pytest
from classify_testkit import OTHER, T0, account, acct_id, link, txn, world

from wapsi.classify import classify_trace, innocent_blockers, role_from_score
from wapsi.classify.explain import describe, explain, explain_blocked
from wapsi.classify.features import FEATURE_COLUMNS, NO_FORWARD_MINUTES, TURNOVER_RATIO_CAP, account_features
from wapsi.classify.scoring import HEURISTIC_TERMS, HeuristicScorer
from wapsi.config import ClassifyConfig
from wapsi.models import (
    AccountState,
    AccountType,
    ApportionmentRule,
    Channel,
    ExternalSignal,
    RiskLevel,
    Role,
    TraceResult,
    TraceTotals,
    TrailEdge,
)

CFG = ClassifyConfig()
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)
NEW_FEATURES = ("hist_distinct_payers_90d", "hist_credit_turnover_ratio", "known_payee_out_ratio_24h")


def _feats(ledger, ids):
    return account_features(ledger, ids, T0, CFG, burst_start=T0)


# --------------------------------------------------------------------------- features


def test_new_columns_registered():
    assert set(NEW_FEATURES) <= set(FEATURE_COLUMNS)


def test_hist_distinct_payers_90d():
    x = acct_id(1)
    payers = [acct_id(n, OTHER) for n in (11, 12, 13, 14)]
    ledger = world(
        [account(1), *(account(n, bank=OTHER) for n in (11, 12, 13, 14, 15))],
        [
            txn(T0 - 5 * D, payers[0], x, 1_000.0),
            txn(T0 - 20 * D, payers[0], x, 1_000.0),  # repeat payer counts once
            txn(T0 - 40 * D, payers[1], x, 2_000.0),
            txn(T0 - 89 * D, payers[2], x, 3_000.0),
            txn(T0 - 100 * D, payers[3], x, 4_000.0),  # older than 90 days
            txn(T0 - 3 * D, None, x, 5_000.0, Channel.CASH_DEPOSIT),  # no counterparty
            txn(T0 - 2 * D, x, payers[3], 500.0),  # a debit is not a payer
            txn(T0 + 5 * M, acct_id(15, OTHER), x, 50_000.0),  # the fraud payment itself is not history
        ],
    )
    f = _feats(ledger, [x]).loc[x]
    assert f.hist_distinct_payers_90d == 3.0
    assert f.n_distinct_payers_7d == 2.0  # unchanged meaning: window includes the burst


def test_hist_credit_turnover_ratio():
    x, y, z, v = acct_id(1), acct_id(2), acct_id(3), acct_id(9, OTHER)
    ledger = world(
        [account(1), account(2), account(3), account(9, bank=OTHER)],
        [
            txn(T0 - 10 * D, v, x, 20_000.0),
            txn(T0 - 30 * D, None, x, 10_000.0, Channel.CASH_DEPOSIT),  # cash is turnover too
            txn(T0 - 95 * D, v, x, 99_000.0),  # outside 90 days
            txn(T0 + H, v, x, 10_000.0),
            txn(T0 - 10 * D, v, y, 900_000.0),
            txn(T0 + H, v, y, 1_000.0),
            txn(T0 - 10 * D, v, z, 5_000.0),  # history but no burst inflow
        ],
    )
    f = _feats(ledger, [x, y, z])
    assert f.loc[x, "hist_credit_turnover_ratio"] == pytest.approx(3.0)
    assert f.loc[y, "hist_credit_turnover_ratio"] == TURNOVER_RATIO_CAP
    assert f.loc[z, "hist_credit_turnover_ratio"] == 0.0


def test_known_payee_out_ratio():
    x, landlord, fresh, v = acct_id(1), acct_id(2), acct_id(3), acct_id(9, OTHER)
    ledger = world(
        [account(1), account(2), account(3, opened_days_before=2), account(9, bank=OTHER)],
        [
            txn(T0 - 30 * D, x, landlord, 15_000.0, remark="RENT AUG"),
            txn(T0 + M, v, x, 100_000.0),
            txn(T0 + 2 * H, x, landlord, 15_000.0, remark="RENT SEP"),  # known payee
            txn(T0 + 3 * H, x, fresh, 45_000.0),  # never paid before
            txn(T0 + 4 * H, x, fresh, 5_000.0),  # paid in the burst, still not "known"
            txn(T0 + 5 * H, x, None, 10_000.0, Channel.ATM),  # no counterparty
        ],
    )
    f = _feats(ledger, [x]).loc[x]
    assert f.pass_through_ratio_24h == pytest.approx(0.75)
    assert f.known_payee_out_ratio_24h == pytest.approx(0.15)


def test_known_payee_ratio_never_exceeds_pass_through_and_zero_without_history():
    x, y, v = acct_id(1), acct_id(2), acct_id(9, OTHER)
    ledger = world(
        [account(1), account(2), account(9, bank=OTHER)],
        [txn(T0 - 3 * D, x, y, 100.0), txn(T0 + M, v, x, 1_000.0), txn(T0 + 2 * M, x, y, 5_000.0)],
    )
    f = _feats(ledger, [x, y])
    assert f.loc[x, "known_payee_out_ratio_24h"] == 1.0 == f.loc[x, "pass_through_ratio_24h"]
    assert f.loc[y, "known_payee_out_ratio_24h"] == 0.0
    empty = world([account(1)], [])
    assert _feats(empty, [x]).loc[x, list(NEW_FEATURES)].eq(0.0).all()


# --------------------------------------------------------------------------- heuristic weights


def row(**kw) -> dict:
    r = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    r["median_forward_minutes"] = NO_FORWARD_MINUTES
    r.update(kw)
    return r


SELLER = dict(age_days=2500.0, history_days=30.0, n_distinct_payers_7d=6.0, in_amount_24h=60_000.0,
              pass_through_ratio_24h=0.35, known_payee_out_ratio_24h=0.3, median_forward_minutes=400.0,
              hist_distinct_payers_90d=12.0, hist_credit_turnover_ratio=2.5)  # fmt: skip
FAST = dict(pass_through_ratio_24h=0.95, median_forward_minutes=10.0, in_amount_24h=200_000.0)
ESTABLISHED = dict(age_days=1800.0, history_days=180.0, salary_credits_90d=3.0, hist_distinct_payers_90d=12.0,
                   hist_credit_turnover_ratio=3.0)  # fmt: skip
CASES = {
    # name: (features, role from score, blocked by the guard)
    "genuine_seller": (row(**SELLER), Role.INNOCENT_HOLDER, False),
    "genuine_seller_middling_external": (row(**SELLER, mulehunter_score=0.45, fri_risk=1.0), Role.INNOCENT_HOLDER,
                                         False),
    # a legitimate-looking profile outweighs fast layering in the score; the guard must catch it
    "established_fast_layering": (row(**ESTABLISHED, **FAST), Role.INNOCENT_HOLDER, True),
    "established_no_salary_fast_layering": (row(**{**ESTABLISHED, "salary_credits_90d": 0.0}, **FAST),
                                            Role.UNKNOWN, True),
    "old_no_history_fast_layering": (row(age_days=1800, history_days=180, **FAST), Role.MULE, True),
    # ring warm-up: paying the next mule before the fraud makes it a "known" payee; still a mule
    "fresh_mule_known_payee": (row(age_days=10, known_payee_out_ratio_24h=1.0, shared_device_with_seed_accounts=2,
                                   **FAST), Role.MULE, True),
    "fresh_account_couple_of_payers": (row(age_days=10, in_amount_24h=50_000, hist_distinct_payers_90d=2,
                                           hist_credit_turnover_ratio=1.0), Role.UNKNOWN, False),
}  # fmt: skip


@pytest.fixture(scope="module")
def scored():
    frame = pd.DataFrame({k: v[0] for k, v in CASES.items()}).T[list(FEATURE_COLUMNS)].astype(float)
    hs = HeuristicScorer()
    return frame, hs.score(frame), hs.contributions(frame)


@pytest.mark.parametrize("name", list(CASES))
def test_counterparty_archetypes(scored, name):
    frame, scores, _ = scored
    _, role, blocked = CASES[name]
    assert role_from_score(scores[name], CFG) == role, (name, round(scores[name], 3))
    assert bool(innocent_blockers(frame.loc[name])) == blocked


def test_seller_would_not_have_been_innocent_without_the_new_terms(scored):
    frame, _, _ = scored
    old = HeuristicScorer([t for t in HEURISTIC_TERMS if t.feature not in NEW_FEATURES])
    assert (
        role_from_score(old.score(frame).loc["genuine_seller_middling_external"], CFG) != Role.INNOCENT_HOLDER
    )


def test_new_terms_only_pull_towards_legitimate_and_are_neutral_at_zero():
    values = [0.0, 0.5, 1.0, 2.0, 5.0, 12.0, 100.0]
    frame = pd.DataFrame([row(**dict.fromkeys(NEW_FEATURES, v)) for v in values]).astype(float)
    contrib = HeuristicScorer().contributions(frame)[list(NEW_FEATURES)]
    assert (contrib <= 0).all().all()
    assert (contrib.iloc[0] == 0).all()
    # no credit for a couple of payers or for turnover up to the payment itself
    low = pd.DataFrame([row(hist_distinct_payers_90d=2.0, hist_credit_turnover_ratio=1.0)]).astype(float)
    assert (HeuristicScorer().contributions(low)[list(NEW_FEATURES)] == 0).all().all()
    # bounded: the profile terms alone cannot bring an old account that layers fast into the innocent band
    profile = ("hist_distinct_payers_90d", "hist_credit_turnover_ratio")
    layering = pd.DataFrame([row(age_days=1800, history_days=180, **FAST,
                                 **dict.fromkeys(profile, 100.0))]).astype(float)  # fmt: skip
    assert role_from_score(HeuristicScorer().score(layering).iloc[0], CFG) != Role.INNOCENT_HOLDER


# --------------------------------------------------------------------------- safety guard


@pytest.mark.parametrize(
    ("features", "fragment"),
    [
        (row(pass_through_ratio_24h=0.9, median_forward_minutes=30), "sent on quickly"),
        (row(in_amount_24h=10_000, out_amount_24h=8_000, atm_share_24h=0.7), "cash or sent to crypto"),
        (row(in_amount_24h=10_000, out_amount_24h=6_000, crypto_share_24h=1.0), "cash or sent to crypto"),
        (row(shared_device_with_seed_accounts=1), "Device shared"),
        (row(dormant_then_burst=1), "Dormant account"),
        (row(ncrp_prior_reports=2), "Reported on NCRP more than once"),
    ],
)
def test_innocent_blockers_fire(features, fragment):
    reasons = innocent_blockers(features)
    assert len(reasons) == 1 and fragment in reasons[0]


def test_innocent_blockers_quiet_cases():
    assert innocent_blockers(row()) == []
    assert innocent_blockers(row(**SELLER)) == []
    # slow forwarding, spending at known payees, small cash withdrawal, a single prior report
    assert innocent_blockers(row(pass_through_ratio_24h=0.9, median_forward_minutes=300)) == []
    assert innocent_blockers(row(pass_through_ratio_24h=0.9, known_payee_out_ratio_24h=0.5,
                                 median_forward_minutes=5)) == []  # fmt: skip
    assert innocent_blockers(row(in_amount_24h=10_000, out_amount_24h=2_000, atm_share_24h=1.0)) == []
    assert innocent_blockers(row(ncrp_prior_reports=1)) == []
    assert innocent_blockers({"pass_through_ratio_24h": float("nan")}) == []


# --------------------------------------------------------------------------- explanations


def test_new_feature_wording():
    r = pd.Series(
        row(hist_distinct_payers_90d=12, hist_credit_turnover_ratio=2.54, known_payee_out_ratio_24h=0.3)
    )
    assert describe("hist_distinct_payers_90d", r, -1.0) == (
        "Received payments from 12 different senders in the 90 days before the fraud"
    )
    assert describe("hist_credit_turnover_ratio", r, -0.5) == (
        "Credits in the 90 days before the fraud were about 2.5 times the amount received"
    )
    capped = pd.Series(row(hist_credit_turnover_ratio=TURNOVER_RATIO_CAP))
    assert "more than 10 times" in describe("hist_credit_turnover_ratio", capped, -0.5)
    assert describe("known_payee_out_ratio_24h", r, -0.4) == (
        "30% of the money received was paid within 24 hours to accounts it had already paid before the fraud"
    )
    for feat in NEW_FEATURES:  # legitimate-profile facts are never stated as mule indicators
        assert describe(feat, r, +1.0) is None


def test_established_base_needs_at_least_three_payers():
    few = pd.Series(row(age_days=2000, hist_distinct_payers_90d=1))
    contrib = pd.Series(
        {**dict.fromkeys(FEATURE_COLUMNS, 0.0), "hist_distinct_payers_90d": -2.0, "age_days": -1.0}
    )
    reasons = explain(Role.INNOCENT_HOLDER, few, contrib, score=0.05)
    assert not any("sender" in t for t in reasons)
    assert "established base" not in reasons[-1] and "SOP Para 5(xii)" in reasons[-1]
    many = few.copy()
    many["hist_distinct_payers_90d"] = 9
    reasons = explain(Role.INNOCENT_HOLDER, many, contrib, score=0.05)
    assert "with an established base of payers" in reasons[-1]
    assert "Received payments from 9 different senders in the 90 days before the fraud" in reasons


def test_explain_blocked_format():
    r = pd.Series(row(**ESTABLISHED, **FAST))
    contrib = HeuristicScorer().contributions(pd.DataFrame([r]).astype(float)).iloc[0]
    reasons = explain_blocked(r, contrib, innocent_blockers(r), score=0.2)
    assert len(reasons) == 3
    assert reasons[0].startswith("Most of the money received was sent on quickly")
    assert "officer review" in reasons[-1] and "mule score 0.20" in reasons[-1]
    no_legit = explain_blocked(r, contrib * 0, ["a", "b", "c"], score=0.1)
    assert no_legit[:2] == ["a", "b"] and len(no_legit) == 3


# --------------------------------------------------------------------------- end to end on hand-built traces


def _state(acct, kind, layer, first=None):
    return AccountState(
        account_id=acct, bank_id=acct[:4], account_type=kind, is_home_bank=acct.startswith("HB01"), layer=layer,
        first_tainted_at=first, balance_now=0.0,
    )  # fmt: skip


def _edge(t, depth, tainted):
    return TrailEdge(
        txn_id=t.txn_id, ts=t.ts, from_account=t.from_account, to_account=t.to_account,
        from_bank_id=t.from_bank_id, to_bank_id=t.to_bank_id, channel=t.channel, utr=t.utr, amount=t.amount,
        depth=depth, tainted={"N1": tainted}, tainted_total=tainted,
    )  # fmt: skip


def _trace(edges, accounts):
    return TraceResult(
        case_id="CASE-CP", ncrp_ids=[], rule=ApportionmentRule.PRO_RATA, window_start=T0,
        window_end=T0 + 72 * H, victim_seed={}, edges=edges, accounts=accounts, totals=TraceTotals(),
        computed_at=T0,
    )  # fmt: skip


def test_genuine_seller_and_landlord_are_innocent_holders():
    seller, landlord, victim = acct_id(1), acct_id(2), acct_id(9, OTHER)
    buyers = [acct_id(100 + i, OTHER) for i in range(12)]
    history = [
        txn(T0 - (5 + 4 * i) * D, b, seller, 5_000.0 + 600 * i, remark="order") for i, b in enumerate(buyers)
    ]
    history += [txn(T0 - 3 * D + i * H, b, seller, 2_500.0, remark="order") for i, b in enumerate(buyers[:3])]
    history += [
        txn(T0 - 40 * D, seller, landlord, 15_000.0, remark="RENT JUL"),
        txn(T0 - 10 * D, seller, landlord, 15_000.0, remark="RENT AUG"),
    ]
    pay = txn(T0, victim, seller, 50_000.0, remark="ORDER phone")
    rent = txn(T0 + 6 * H, seller, landlord, 15_000.0, remark="RENT SEP")
    ledger = world(
        [account(1, opened_days_before=3000), account(2, opened_days_before=4000), account(9, bank=OTHER),
         *(account(100 + i, bank=OTHER) for i in range(12))],
        [*history, pay, rent],
        links=[link("DEV-1", seller), link("DEV-2", landlord)],
        signals=[ExternalSignal(account_id=seller, fri_risk=RiskLevel.MEDIUM, mulehunter_score=0.4)],
    )  # fmt: skip
    trace = _trace(
        [_edge(pay, 1, 50_000.0), _edge(rent, 2, 9_000.0)],
        {
            victim: _state(victim, AccountType.INDIVIDUAL, 0),
            seller: _state(seller, AccountType.INDIVIDUAL, 1, T0),
            landlord: _state(landlord, AccountType.INDIVIDUAL, 2, T0 + 6 * H),
        },
    )
    res = classify_trace(ledger, trace, CFG)
    s = res[seller]
    assert s.role == Role.INNOCENT_HOLDER, (s.score, s.reasons)
    assert s.features["hist_distinct_payers_90d"] == 12.0
    assert s.features["known_payee_out_ratio_24h"] == pytest.approx(0.3)
    assert any("12 different senders in the 90 days before the fraud" in t for t in s.reasons)
    assert "SOP Para 5(xii)" in s.reasons[-1]
    assert res[landlord].role == Role.INNOCENT_HOLDER
    # the same account scored without the counterparty-history terms would have been escalated or reviewed
    old = HeuristicScorer([t for t in HEURISTIC_TERMS if t.feature not in NEW_FEATURES])
    assert classify_trace(ledger, trace, CFG, model=old)[seller].role != Role.INNOCENT_HOLDER


def test_established_account_layering_is_never_auto_innocent():
    x, fresh, victim, employer = acct_id(1), acct_id(2), acct_id(9, OTHER), acct_id(8, OTHER)
    friends = [acct_id(200 + i, OTHER) for i in range(10)]
    history = [txn(T0 - d * D, employer, x, 60_000.0, remark="SALARY") for d in (85, 55, 25)]
    history += [txn(T0 - (10 + 6 * i) * D, f, x, 3_000.0) for i, f in enumerate(friends)]
    pay = txn(T0, victim, x, 200_000.0)
    fwd = txn(T0 + 8 * M, x, fresh, 190_000.0)
    ledger = world(
        [account(1, opened_days_before=1800), account(2, opened_days_before=5), account(8, bank=OTHER),
         account(9, bank=OTHER), *(account(200 + i, bank=OTHER) for i in range(10))],
        [*history, pay, fwd],
    )  # fmt: skip
    trace = _trace(
        [_edge(pay, 1, 200_000.0), _edge(fwd, 2, 190_000.0)],
        {
            victim: _state(victim, AccountType.INDIVIDUAL, 0),
            x: _state(x, AccountType.INDIVIDUAL, 1, T0),
            fresh: _state(fresh, AccountType.INDIVIDUAL, 2, T0 + 8 * M),
        },
    )
    r = classify_trace(ledger, trace, CFG)[x]
    assert r.role in (Role.UNKNOWN, Role.MULE)
    if r.score <= CFG.innocent_threshold:  # profile alone would say innocent: the guard decided
        assert r.role == Role.UNKNOWN
        assert (
            r.reasons[0]
            == "Most of the money received was sent on quickly to accounts it had not paid before"
        )
        assert "officer review" in r.reasons[-1]
