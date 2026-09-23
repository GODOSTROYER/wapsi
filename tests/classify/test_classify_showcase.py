import math
import re
from datetime import timedelta

import pandas as pd
import pytest
from classify_testkit import GUILT_WORDS, INR_RE, T0, account, acct_id, txn, world

from wapsi.classify import (
    FEATURE_COLUMNS,
    HeuristicScorer,
    Scorer,
    classify_trace,
    hard_rules,
    role_from_score,
)
from wapsi.classify.features import account_features
from wapsi.classify.scoring import HEURISTIC_TERMS
from wapsi.config import ClassifyConfig
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.models import (
    AccountState,
    AccountType,
    ApportionmentRule,
    GstRecord,
    Invoice,
    OrderStatus,
    Role,
    TraceResult,
    TraceTotals,
    TrailEdge,
)
from wapsi.money import fmt_inr, parse_inr

CFG = ClassifyConfig()


@pytest.fixture(scope="module")
def results():
    ledger = sc.showcase_ledger()
    out = {}
    for which in (1, 2):
        out.update(classify_trace(ledger, showcase_bundle(which).trace, CFG))
    return out


def test_showcase_roles(results):
    for acct in (sc.A, sc.B, sc.D, sc.M):
        assert results[acct].role == Role.MULE, (acct, results[acct])
    assert results[sc.L].role == Role.INNOCENT_HOLDER
    assert results[sc.K].role == Role.MERCHANT
    assert results[sc.P].role == Role.INTERMEDIARY
    for acct in (sc.V1, sc.V2, sc.V3):
        assert results[acct].role == Role.VICTIM
    for acct in (sc.C, sc.E):
        assert results[acct].role in (Role.MULE, Role.UNKNOWN)
        assert results[acct].role != Role.INNOCENT_HOLDER


def test_every_trace_account_classified():
    ledger = sc.showcase_ledger()
    for which in (1, 2):
        trace = showcase_bundle(which).trace
        res = classify_trace(ledger, trace, CFG)
        assert set(res) == set(trace.accounts)
        for acct, r in res.items():
            assert r.account_id == acct


def test_reasons_are_clean(results):
    for acct, r in results.items():
        assert r.reasons, acct
        assert len(r.reasons) <= 4
        for text in r.reasons:
            assert text and text.strip() == text
            assert "nan" not in text.lower().split(), (acct, text)
            assert "nan" not in text.lower().replace(",", " ").replace("(", " ").replace(")", " ").split()
            assert not re.search(r"\bNone\b(?! of)", text) and "inf" not in text.split(), text
            assert not GUILT_WORDS.search(text), (acct, text)
            assert "Rs" not in text and "INR" not in text
            for amount in INR_RE.findall(text):  # every rupee amount is fmt_inr-formatted (Indian grouping)
                assert amount == fmt_inr(parse_inr(amount)), (acct, text)
            assert not any(tok.isdigit() and len(tok) > 3 for tok in text.replace(",", " ").split()), text


def test_reason_content(results):
    a = " | ".join(results[sc.A].reasons)
    assert "Account opened 11 days before the fraud" in a
    assert "₹4,80,000.00" in a and "7 minutes" in a
    assert "Device shared with 2 other accounts that received the same money" in a
    b = " | ".join(results[sc.B].reasons)
    assert "21 days" in b and "₹3,00,000.00" in b
    lr = results[sc.L].reasons
    assert "SOP Para 5(xii)" in lr[-1] and "rent" in lr[-1] and "Long-standing" in lr[-1]
    k = " | ".join(results[sc.K].reasons)
    assert "active GSTIN" in k and "INV-K-5521" in k and "SOP Para 9.2" in k
    assert "SOP Para 9.1(iii)" in " ".join(results[sc.P].reasons)
    assert "₹4,80,000.00" in " ".join(results[sc.V1].reasons)
    assert "₹1,00,000.00" in " ".join(results[sc.V2].reasons)
    for acct in (sc.A, sc.B, sc.C, sc.E, sc.M):
        assert any("before the fraud" in t for t in results[acct].reasons), acct
    assert any("ATM" in t for t in results[sc.E].reasons)
    assert any("25 minutes" in t for t in results[sc.M].reasons)
    assert any("Device shared with 2 other accounts" in t for t in results[sc.D].reasons)


def test_scores_features_and_model(results):
    for r in results.values():
        assert 0.0 <= r.score <= 1.0
        assert set(r.features) == set(FEATURE_COLUMNS)
        assert all(isinstance(v, float) and math.isfinite(v) for v in r.features.values())
        if r.role in (Role.VICTIM, Role.MERCHANT, Role.INTERMEDIARY):
            assert r.model == f"rules+{HeuristicScorer.name}"
        else:
            assert r.model == HeuristicScorer.name
    assert results[sc.A].score >= CFG.mule_threshold
    assert results[sc.L].score <= CFG.innocent_threshold
    assert results[sc.A].features["age_days"] == pytest.approx(11.04, abs=0.01)
    assert results[sc.M].features["n_distinct_payers_7d"] == 2.0
    assert results[sc.L].features["recurring_rent_like_90d"] == 2.0


def test_deterministic():
    ledger = sc.showcase_ledger()
    trace = showcase_bundle(1).trace
    first = classify_trace(ledger, trace, CFG)
    second = classify_trace(sc.showcase_ledger(), trace, CFG)
    assert {k: v.model_dump() for k, v in first.items()} == {k: v.model_dump() for k, v in second.items()}


def test_thresholds_come_from_config():
    ledger = sc.showcase_ledger()
    trace = showcase_bundle(1).trace
    strict = classify_trace(ledger, trace, ClassifyConfig(mule_threshold=0.995, innocent_threshold=0.001))
    assert strict[sc.A].role == Role.UNKNOWN
    assert strict[sc.L].role == Role.UNKNOWN
    assert any("officer review" in t for t in strict[sc.L].reasons)
    assert strict[sc.K].role == Role.MERCHANT  # hard rules ignore thresholds
    assert role_from_score(0.6, CFG) == Role.MULE and role_from_score(0.35, CFG) == Role.INNOCENT_HOLDER
    assert role_from_score(0.5, CFG) == Role.UNKNOWN


class ConstScorer:
    def __init__(self, value: float, name: str = "const"):
        self.value, self.name = value, name

    def score(self, features: pd.DataFrame) -> pd.Series:
        return pd.Series(self.value, index=features.index)

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(0.0, index=features.index, columns=list(features.columns) + ["bias"])
        out["mulehunter_score"] = 1.0 if self.value > 0.5 else -1.0
        return out


def test_custom_scorer_protocol():
    assert isinstance(HeuristicScorer(), Scorer)
    assert isinstance(ConstScorer(0.5), Scorer)
    ledger = sc.showcase_ledger()
    trace = showcase_bundle(1).trace
    mid = classify_trace(ledger, trace, CFG, model=ConstScorer(0.5, "stub"))
    assert mid[sc.A].role == Role.UNKNOWN and mid[sc.A].model == "stub"
    assert mid[sc.K].role == Role.MERCHANT and mid[sc.K].model == "rules+stub"
    low = classify_trace(ledger, trace, CFG, model=ConstScorer(0.1))
    assert low[sc.L].role == Role.INNOCENT_HOLDER
    assert "SOP Para 5(xii)" in low[sc.L].reasons[-1]
    # safety guard: a low score cannot label an account that layered the money as innocent, whatever the scorer
    assert low[sc.A].role == Role.UNKNOWN and low[sc.A].model == "const"
    assert "sent on quickly to accounts it had not paid before" in low[sc.A].reasons[0]
    assert "officer review" in low[sc.A].reasons[-1] and "SOP Para 5(xii)" in low[sc.A].reasons[-1]
    high = classify_trace(ledger, trace, CFG, model=ConstScorer(0.9))
    assert high[sc.L].role == Role.MULE
    assert high[sc.L].reasons == ["MuleHunter-style risk score of 0.03"]
    # a merchant with strong mule indicators keeps the rule role but gets a review note
    assert any("Mule indicators are also present" in t for t in high[sc.K].reasons)


def test_heuristic_contributions_are_additive():
    ledger = sc.showcase_ledger()
    trace = showcase_bundle(1).trace
    feats = account_features(ledger, sorted(trace.accounts), trace.window_start, CFG)
    hs = HeuristicScorer()
    contrib = hs.contributions(feats)
    assert set(FEATURE_COLUMNS) <= set(contrib.columns) and "bias" in contrib.columns
    z = contrib.sum(axis=1)
    expected = (1 / (1 + (-z).map(math.exp))).clip(0.01, 0.99)
    pd.testing.assert_series_equal(hs.score(feats), expected, check_names=False)
    weighted = {t.feature for t in HEURISTIC_TERMS}
    assert weighted <= set(FEATURE_COLUMNS)
    assert (
        (contrib.drop(columns=[c for c in contrib.columns if c in weighted or c == "bias"]) == 0).all().all()
    )
    assert len(hs.describe()) == len(HEURISTIC_TERMS) + 1


def test_empty_trace():
    trace = showcase_bundle(1).trace.model_copy(update={"accounts": {}, "edges": []})
    assert classify_trace(sc.showcase_ledger(), trace, CFG) == {}


# --------------------------------------------------------------------------- hard rules on a hand-built trace


def _state(acct, kind, layer=1, bank="HB01"):
    return AccountState(
        account_id=acct, bank_id=bank, account_type=kind, is_home_bank=bank == "HB01", layer=layer,
        first_tainted_at=T0 + timedelta(minutes=5) if layer else None, balance_now=0.0,
    )  # fmt: skip


def _edge(t, depth):
    return TrailEdge(
        txn_id=t.txn_id, ts=t.ts, from_account=t.from_account, to_account=t.to_account,
        from_bank_id=t.from_bank_id, to_bank_id=t.to_bank_id, channel=t.channel, utr=t.utr, amount=t.amount,
        depth=depth, tainted={"N": t.amount}, tainted_total=t.amount,
    )  # fmt: skip


def test_business_rules():
    mule, biz_ok, biz_open, biz_nogst, esc, nodal = (acct_id(i) for i in range(1, 7))
    gst_ok, gst_bad = "27AAAAA1111A1Z1", "27BBBBB2222B1Z2"
    t1 = txn(T0 + timedelta(minutes=10), mule, biz_ok, 2_000.0)
    t2 = txn(T0 + timedelta(minutes=11), mule, biz_open, 3_000.0)
    t3 = txn(T0 + timedelta(minutes=12), mule, biz_nogst, 4_000.0)
    t4 = txn(T0 + timedelta(minutes=13), mule, esc, 5_000.0)
    t5 = txn(T0 + timedelta(minutes=14), mule, nodal, 6_000.0)

    def inv(i, merchant, t, status, delivered=True):
        return Invoice(
            invoice_id=f"INV-{i}", merchant_account=merchant, payer_account=mule, txn_id=t.txn_id, amount=t.amount,
            ordered_at=T0, delivered_at=T0 + timedelta(hours=1) if delivered else None, order_status=status,
        )  # fmt: skip

    ledger = world(
        [
            account(1, opened_days_before=5),
            account(2, kind=AccountType.BUSINESS, gstin=gst_ok),
            account(3, kind=AccountType.BUSINESS, gstin=gst_ok),
            account(4, kind=AccountType.BUSINESS, gstin=gst_bad),
            account(5, kind=AccountType.ESCROW, customer=None),
            account(6, kind=AccountType.NODAL, customer=None),
        ],
        [t1, t2, t3, t4, t5],
        invoices=[
            inv(1, biz_ok, t1, OrderStatus.DELIVERED),
            inv(2, biz_open, t2, OrderStatus.UNDELIVERED, delivered=False),
            inv(3, biz_nogst, t3, OrderStatus.DELIVERED),
        ],
        gst=[
            GstRecord(gstin=gst_ok, legal_name="OK", status="ACTIVE", registered_at=T0.date()),
            GstRecord(gstin=gst_bad, legal_name="Bad", status="CANCELLED", registered_at=T0.date()),
        ],
    )
    accounts = {
        mule: _state(mule, AccountType.INDIVIDUAL),
        biz_ok: _state(biz_ok, AccountType.BUSINESS, 2),
        biz_open: _state(biz_open, AccountType.BUSINESS, 2),
        biz_nogst: _state(biz_nogst, AccountType.BUSINESS, 2),
        esc: _state(esc, AccountType.ESCROW, 2),
        nodal: _state(nodal, AccountType.NODAL, 2),
    }
    trace = TraceResult(
        case_id="CASE-T", ncrp_ids=["N-UNKNOWN"], rule=ApportionmentRule.PRO_RATA, window_start=T0,
        window_end=T0 + timedelta(hours=72), victim_seed={}, edges=[_edge(t, 2) for t in (t1, t2, t3, t4, t5)],
        accounts=accounts, totals=TraceTotals(), computed_at=T0,
    )  # fmt: skip
    hits = hard_rules(ledger, trace)  # unknown complaint id is tolerated
    assert hits[biz_ok].role == Role.MERCHANT and hits[biz_ok].rule == "BUSINESS_DELIVERED"
    assert any("INV-1" in r for r in hits[biz_ok].reasons)
    assert biz_open not in hits  # undelivered -> scored, not a rule merchant
    assert biz_nogst not in hits  # GSTIN cancelled
    assert hits[esc].role == Role.INTERMEDIARY and hits[esc].reasons[0] == "Escrow account"
    assert hits[nodal].role == Role.INTERMEDIARY and hits[nodal].reasons[0] == "Nodal account"
    assert mule not in hits

    res = classify_trace(ledger, trace, CFG)
    assert res[biz_ok].role == Role.MERCHANT
    assert res[biz_open].role in (Role.MULE, Role.UNKNOWN, Role.INNOCENT_HOLDER)
    assert res[esc].role == Role.INTERMEDIARY and res[nodal].role == Role.INTERMEDIARY


def test_merchant_type_undelivered_and_missing_invoice():
    mule, shop, shop2 = acct_id(1), acct_id(2), acct_id(3)
    t1 = txn(T0 + timedelta(minutes=10), mule, shop, 2_000.0)
    t2 = txn(T0 + timedelta(minutes=11), mule, shop2, 900.0)
    ledger = world(
        [account(1), account(2, kind=AccountType.MERCHANT), account(3, kind=AccountType.MERCHANT, gstin="X1")],
        [t1, t2],
        invoices=[
            Invoice(invoice_id="INV-9", merchant_account=shop, payer_account=mule, txn_id=t1.txn_id, amount=2_000.0,
                    ordered_at=T0, order_status=OrderStatus.UNDELIVERED)
        ],
    )  # fmt: skip
    trace = TraceResult(
        case_id="CASE-T", ncrp_ids=[], rule=ApportionmentRule.PRO_RATA, window_start=T0,
        window_end=T0 + timedelta(hours=72), victim_seed={}, edges=[_edge(t1, 2), _edge(t2, 2)],
        accounts={mule: _state(mule, AccountType.INDIVIDUAL, 0), shop: _state(shop, AccountType.MERCHANT, 2),
                  shop2: _state(shop2, AccountType.MERCHANT, 2)},
        totals=TraceTotals(), computed_at=T0,
    )  # fmt: skip
    hits = hard_rules(ledger, trace)
    assert hits[mule].role == Role.VICTIM  # layer-0 account
    assert hits[shop].role == Role.MERCHANT
    assert "Merchant account without a GSTIN on record" in hits[shop].reasons
    assert any("not marked delivered" in r for r in hits[shop].reasons)
    assert any("pool / nodal" in r for r in hits[shop].reasons)
    assert any("GSTIN status is not on record" in r for r in hits[shop2].reasons)
    assert any("No invoice found" in r for r in hits[shop2].reasons)
