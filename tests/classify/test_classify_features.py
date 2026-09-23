from datetime import timedelta

import numpy as np
import pytest
from classify_testkit import HOME, OTHER, T0, account, acct_id, link, txn, world

from wapsi.classify.features import FEATURE_COLUMNS, NO_FORWARD_MINUTES, account_features
from wapsi.config import ClassifyConfig
from wapsi.models import AccountType, Channel, Customer, ExternalSignal, RiskLevel
from wapsi.timeutil import IST

CFG = ClassifyConfig()
H = timedelta(hours=1)
M = timedelta(minutes=1)
D = timedelta(days=1)


def _feats(ledger, ids, **kw):
    kw.setdefault("burst_start", T0)
    return account_features(ledger, ids, T0, CFG, **kw)


def test_frame_shape_and_no_nan():
    x, src = acct_id(1), acct_id(2, OTHER)
    ledger = world([account(1), account(2, bank=OTHER)], [txn(T0 + H, src, x, 5000.0)])
    df = _feats(ledger, [x, src, x])  # duplicates collapse
    assert list(df.columns) == list(FEATURE_COLUMNS)
    assert list(df.index) == [x, src]
    assert df.index.name == "account_id"
    assert not df.isna().any().any()
    assert all(dt == np.float64 for dt in df.dtypes)


def test_empty_ids():
    ledger = world([account(1)], [])
    df = account_features(ledger, [], T0, CFG)
    assert df.empty and list(df.columns) == list(FEATURE_COLUMNS)


def test_pass_through_ratio_and_counts():
    x, v, y, z = acct_id(1), acct_id(9, OTHER), acct_id(2), acct_id(3)
    ledger = world(
        [account(1), account(9, bank=OTHER), account(2), account(3)],
        [
            txn(T0 - 2 * D, x, z, 7000.0),  # history: outside the burst window
            txn(T0 + 5 * M, v, x, 100_000.0),
            txn(T0 + 20 * M, x, y, 60_000.0),
            txn(T0 + 2 * H, x, None, 20_000.0, Channel.ATM),
            txn(T0 + 30 * H, x, z, 15_000.0),  # after the 24 h window
        ],
    )
    f = _feats(ledger, [x]).loc[x]
    assert f.in_count_24h == 1 and f.out_count_24h == 2
    assert f.in_amount_24h == 100_000.0 and f.out_amount_24h == 80_000.0
    assert f.pass_through_ratio_24h == pytest.approx(0.8)
    assert f.atm_share_24h == pytest.approx(0.25)
    assert f.crypto_share_24h == 0.0
    assert f.n_distinct_payers_7d == 1 and f.n_distinct_payees_7d == 2  # ATM has no counterparty


def test_pass_through_capped_and_zero_inflow():
    x, y = acct_id(1), acct_id(2)
    ledger = world([account(1), account(2)], [txn(T0 + H, y, x, 1000.0), txn(T0 + 2 * H, x, y, 5000.0)])
    f = _feats(ledger, [x, y])
    assert f.loc[x, "pass_through_ratio_24h"] == 1.0
    # y received 5000 and sent 1000 before it: forwarded share 0.2
    assert f.loc[y, "pass_through_ratio_24h"] == pytest.approx(0.2)
    empty = world([account(1)], [])
    assert _feats(empty, [x]).loc[x, "pass_through_ratio_24h"] == 0.0


def test_median_forward_minutes():
    x, v, y = acct_id(1), acct_id(9, OTHER), acct_id(2)
    ledger = world(
        [account(1), account(9, bank=OTHER), account(2)],
        [
            txn(T0, v, x, 10_000.0),
            txn(T0 + 10 * M, x, y, 9_000.0),  # 10 min after credit 1
            txn(T0 + 2 * H, v, x, 10_000.0),
            txn(T0 + 2 * H + 30 * M, x, y, 9_000.0),  # 30 min after credit 2
            txn(T0 + 5 * H, v, x, 1_000.0),  # never forwarded -> ignored in the median
        ],
    )
    f = _feats(ledger, [x, y])
    assert f.loc[x, "median_forward_minutes"] == pytest.approx(20.0)
    assert f.loc[y, "median_forward_minutes"] == NO_FORWARD_MINUTES  # y never sends anything on


def test_median_forward_ignores_debits_before_credit():
    x, v, y = acct_id(1), acct_id(9, OTHER), acct_id(2)
    ledger = world(
        [account(1), account(9, bank=OTHER), account(2)],
        [txn(T0 + 10 * M, x, y, 500.0), txn(T0 + 20 * M, v, x, 10_000.0), txn(T0 + 65 * M, x, y, 9_000.0)],
    )
    assert _feats(ledger, [x]).loc[x, "median_forward_minutes"] == pytest.approx(45.0)


def test_device_sharing_and_seed_accounts():
    x, y, z, w, q = (acct_id(i) for i in range(1, 6))
    later = T0 + 10 * D  # linked after the observation window: must not count
    ledger = world(
        [account(i) for i in range(1, 6)],
        [],
        links=[
            link("DEV-1", x), link("DEV-1", y), link("DEV-1", z),
            link("DEV-2", x), link("DEV-2", w), link("DEV-2", y),
            link("DEV-3", x), link("DEV-3", q, first_seen=later),
        ],
    )  # fmt: skip
    f = _feats(ledger, [x, y, q], seed_accounts={y, q})
    assert f.loc[x, "device_shared_count"] == 3  # y, z, w (distinct, q linked too late)
    assert f.loc[x, "shared_device_with_seed_accounts"] == 1  # y only
    assert f.loc[y, "device_shared_count"] == 3  # x, z, w
    assert f.loc[y, "shared_device_with_seed_accounts"] == 0  # itself does not count
    assert f.loc[q, "device_shared_count"] == 0  # its own link is after obs_end
    assert _feats(ledger, [x]).loc[x, "shared_device_with_seed_accounts"] == 0  # no seeds given


def test_dormant_then_burst():
    dormant, active, fresh, src = acct_id(1), acct_id(2), acct_id(3), acct_id(9, OTHER)
    sink = acct_id(8)
    burst = []
    for acct in (dormant, active, fresh):
        burst += [
            txn(T0 + 5 * M, src, acct, 30_000.0),
            txn(T0 + 15 * M, acct, sink, 10_000.0),
            txn(T0 + 25 * M, acct, sink, 10_000.0),
        ]
    history = [
        txn(T0 - 120 * D, src, dormant, 2_000.0),  # last activity 120 days ago
        txn(T0 - 3 * D, src, active, 2_000.0),
    ]
    ledger = world(
        [account(1, opened_days_before=900), account(2, opened_days_before=900),
         account(3, opened_days_before=20), account(8), account(9, bank=OTHER)],
        history + burst,
    )  # fmt: skip
    f = _feats(ledger, [dormant, active, fresh])
    assert f.loc[dormant, "dormant_then_burst"] == 1.0
    assert f.loc[active, "dormant_then_burst"] == 0.0
    assert f.loc[fresh, "dormant_then_burst"] == 0.0  # new, not dormant
    assert f.loc[dormant, "history_days"] == pytest.approx(120.0)
    # no history at all within the 180-day lookback also counts as dormant for an old account
    silent = world([account(1, opened_days_before=900), account(8), account(9, bank=OTHER)], burst[:3])
    assert _feats(silent, [dormant]).loc[dormant, "dormant_then_burst"] == 1.0


def test_night_share_uses_ist():
    x, y = acct_id(1), acct_id(2)
    day = T0.replace(hour=0, minute=0)
    ledger = world(
        [account(1), account(2)],
        [
            txn(day + timedelta(hours=23, minutes=30), x, y, 100.0),  # 23:30 IST
            txn(day + timedelta(days=1, hours=2), x, y, 100.0),  # 02:00 IST
            txn(day + timedelta(days=1, hours=14), x, y, 100.0),  # 14:00 IST
            txn(day + timedelta(days=1, hours=5), x, y, 100.0),  # 05:00 IST is not night
        ],
    )
    f = account_features(ledger, [x], day + timedelta(days=3), CFG)
    assert f.loc[x, "night_share"] == pytest.approx(0.5)
    # 18:00 UTC is 23:30 IST
    from datetime import UTC, datetime

    utc_night = world([account(1), account(2)], [txn(datetime(2026, 9, 1, 18, 0, tzinfo=UTC), x, y, 1.0)])
    assert account_features(utc_night, [x], T0, CFG).loc[x, "night_share"] == 1.0


def test_default_burst_window_is_before_as_of():
    x, y = acct_id(1), acct_id(2)
    ledger = world(
        [account(1), account(2)],
        [txn(T0 - 2 * H, y, x, 4_000.0), txn(T0 - 30 * H, y, x, 1_000.0), txn(T0 + H, y, x, 9_000.0)],
    )
    f = account_features(ledger, [x], T0, CFG).loc[x]
    assert f.in_count_24h == 1 and f.in_amount_24h == 4_000.0
    wide = account_features(ledger, [x], T0, ClassifyConfig(feature_window_hours=48)).loc[x]
    assert wide.in_amount_24h == 5_000.0


def test_other_bank_account_uses_consortium_view():
    home, c, d = acct_id(1), acct_id(2, OTHER), acct_id(3, OTHER)
    ledger = world(
        [account(1), account(2, bank=OTHER), account(3, bank=OTHER)],
        [txn(T0 + M, home, c, 50_000.0), txn(T0 + 20 * M, c, d, 45_000.0)],  # c -> d is invisible to home
    )
    f = _feats(ledger, [c]).loc[c]
    assert f.is_other_bank == 1.0
    assert f.out_amount_24h == 45_000.0 and f.pass_through_ratio_24h == pytest.approx(0.9)
    assert f.median_forward_minutes == pytest.approx(19.0)


def test_static_attributes():
    x, m, p, b = acct_id(1), acct_id(2), acct_id(3), acct_id(4)
    gstin = "27ABCDE1234F1Z5"
    from wapsi.models import GstRecord

    ledger = world(
        [
            account(1, opened_days_before=11.5),
            account(2, kind=AccountType.MERCHANT, gstin=gstin),
            account(3, kind=AccountType.POOL, customer=None),
            account(4, kind=AccountType.BUSINESS, gstin="29ZZZZZ9999Z1Z9"),
        ],
        [],
        customers=[
            Customer(customer_id=f"CUST-{HOME}-1", name="X", mobile="9", pan_masked="X", kyc_tier="MIN"),
            Customer(customer_id=f"CUST-{HOME}-2", name="Y", mobile="9", pan_masked="X", kyc_tier="vkyc"),
        ],
        signals=[
            ExternalSignal(
                account_id=x, fri_risk=RiskLevel.VERY_HIGH, mulehunter_score=0.9, ncrp_prior_reports=3
            )
        ],
        gst=[
            GstRecord(gstin=gstin, legal_name="Shop", status="ACTIVE", registered_at=T0.date()),
            GstRecord(gstin="29ZZZZZ9999Z1Z9", legal_name="Biz", status="CANCELLED", registered_at=T0.date()),
        ],
    )
    f = _feats(ledger, [x, m, p, b])
    assert f.loc[x, "age_days"] == pytest.approx(11.5)
    assert (f.loc[x, "kyc_min"], f.loc[x, "kyc_vkyc"]) == (1.0, 0.0)
    assert (f.loc[m, "kyc_min"], f.loc[m, "kyc_vkyc"]) == (0.0, 1.0)
    assert (f.loc[x, "fri_risk"], f.loc[x, "mulehunter_score"], f.loc[x, "ncrp_prior_reports"]) == (
        3.0,
        0.9,
        3.0,
    )
    assert f.loc[m, "fri_risk"] == 0.0
    assert (f.loc[m, "is_merchant"], f.loc[m, "gst_active"]) == (1.0, 1.0)
    assert (f.loc[b, "is_business"], f.loc[b, "gst_active"]) == (1.0, 0.0)
    assert f.loc[p, "is_intermediary"] == 1.0
    assert f.loc[x, ["is_merchant", "is_intermediary", "is_other_bank"]].sum() == 0.0


def test_recurring_rent_and_salary_credits():
    x, tenant, other, employer = acct_id(1), acct_id(5, OTHER), acct_id(6, OTHER), acct_id(7, OTHER)
    txns = [
        # same tenant, similar amount, monthly -> recurring
        txn(T0 - 61 * D, tenant, x, 15_000.0),
        txn(T0 - 31 * D, tenant, x, 15_500.0),
        txn(T0 - 1 * D, tenant, x, 15_000.0),
        # unrelated one-off credits: not recurring
        txn(T0 - 40 * D, other, x, 15_000.0),
        txn(T0 - 20 * D, other, x, 3_000.0),
        # salary
        txn(T0 - 45 * D, employer, x, 50_000.0, Channel.NEFT, remark="SALARY JUL"),
        txn(T0 - 15 * D, employer, x, 52_000.0, Channel.NEFT, remark="Salary for Aug"),
        txn(T0 - 100 * D, employer, x, 50_000.0, Channel.NEFT, remark="salary"),  # older than 90 days
    ]
    ledger = world([account(1), account(5, bank=OTHER), account(6, bank=OTHER), account(7, bank=OTHER)], txns)
    f = _feats(ledger, [x]).loc[x]
    assert f.recurring_rent_like_90d == 5.0  # 3 rent credits + 2 in-window salary credits (same employer)
    assert f.salary_credits_90d == 2.0


def test_rent_remark_pairs_across_payers_but_not_new_money_only():
    landlord, tenant, mule = acct_id(1), acct_id(5, OTHER), acct_id(2)
    ledger = world(
        [account(1), account(2), account(5, bank=OTHER)],
        [
            txn(T0 - 31 * D, tenant, landlord, 5_000.0, remark="rent Aug"),
            txn(T0 + 13 * M, mule, landlord, 5_000.0, remark="rent Sept"),  # different payer, rent remark
            txn(T0 + 20 * M, mule, landlord, 5_000.0, remark="gift"),  # no remark match, different payer
        ],
    )
    f = _feats(ledger, [landlord]).loc[landlord]
    assert f.recurring_rent_like_90d == 2.0
    # a pair made only of post-as_of credits is not history
    fresh = world(
        [account(1), account(2)],
        [
            txn(T0 + H, mule, landlord, 5_000.0, remark="rent"),
            txn(T0 + 31 * D, mule, landlord, 5_000.0, remark="rent"),
        ],
    )
    wide = ClassifyConfig(feature_window_hours=24 * 40)
    assert (
        account_features(fresh, [landlord], T0, wide, burst_start=T0).loc[landlord, "recurring_rent_like_90d"]
        == 0
    )


def test_round_amount_share():
    x, y = acct_id(1), acct_id(2)
    ledger = world(
        [account(1), account(2)],
        [txn(T0 + H, y, x, 50_000.0), txn(T0 + 2 * H, x, y, 1_200.0), txn(T0 + 3 * H, x, y, 3_000.0),
         txn(T0 + 4 * H, x, y, 999.99)],
    )  # fmt: skip
    assert _feats(ledger, [x]).loc[x, "round_amount_share"] == pytest.approx(0.5)


def test_per_account_anchors():
    x, y, src = acct_id(1), acct_id(2), acct_id(9, OTHER)
    t1 = T0 + 3 * D
    ledger = world(
        [account(1), account(2), account(9, bank=OTHER)],
        [txn(T0 + H, src, x, 1_000.0), txn(t1 + H, src, y, 2_000.0)],
    )
    f = account_features(ledger, [x, y], {x: T0, y: t1}, CFG, burst_start={x: T0, y: t1})
    assert f.loc[x, "in_amount_24h"] == 1_000.0 and f.loc[y, "in_amount_24h"] == 2_000.0
    assert f.loc[y, "age_days"] == pytest.approx(403.0)
    with pytest.raises(KeyError):
        account_features(ledger, [x, y], {x: T0}, CFG)


def test_merchant_delivered_share():
    from wapsi.models import Invoice, OrderStatus

    k, p1, p2 = acct_id(1), acct_id(5, OTHER), acct_id(6, OTHER)
    t_ok, t_open = txn(T0 + H, p1, k, 3_000.0), txn(T0 + 2 * H, p2, k, 1_000.0)
    ledger = world(
        [account(1, kind=AccountType.MERCHANT), account(5, bank=OTHER), account(6, bank=OTHER)],
        [t_ok, t_open],
        invoices=[
            Invoice(invoice_id="I1", merchant_account=k, payer_account=p1, txn_id=t_ok.txn_id, amount=3_000.0,
                    ordered_at=T0, delivered_at=T0 + 2 * H, order_status=OrderStatus.DELIVERED),
            Invoice(invoice_id="I2", merchant_account=k, payer_account=p2, txn_id=t_open.txn_id, amount=1_000.0,
                    ordered_at=T0, order_status=OrderStatus.UNDELIVERED),
        ],
    )  # fmt: skip
    assert _feats(ledger, [k]).loc[k, "merchant_delivered_share"] == pytest.approx(0.75)


def test_ist_and_utc_anchors_are_equivalent():
    from datetime import UTC

    x, y = acct_id(1), acct_id(2)
    ledger = world([account(1), account(2)], [txn(T0 + H, y, x, 1_000.0), txn(T0 - 3 * D, y, x, 500.0)])
    assert T0.tzinfo is IST
    a = account_features(ledger, [x], T0, CFG, burst_start=T0)
    b = account_features(ledger, [x], T0.astimezone(UTC), CFG, burst_start=T0.astimezone(UTC))
    assert a.equals(b)
    assert a.loc[x, "in_amount_24h"] == 1_000.0 and a.loc[x, "history_days"] == pytest.approx(3.0)
