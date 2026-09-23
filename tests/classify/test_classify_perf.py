import time
from datetime import timedelta

import numpy as np
from classify_testkit import HOME, OTHER, T0

from wapsi.adapters.memory import InMemoryLedger
from wapsi.classify.features import FEATURE_COLUMNS, account_features
from wapsi.config import ClassifyConfig
from wapsi.models import (
    Account,
    AccountType,
    Bank,
    Channel,
    Customer,
    DeviceLink,
    ExternalSignal,
    RiskLevel,
    Transaction,
)


def big_world(
    n_accounts: int = 2000, n_txns: int = 20_000, seed: int = 11
) -> tuple[InMemoryLedger, list[str]]:
    rng = np.random.default_rng(seed)
    ids = [f"{HOME if i % 4 else OTHER}-{i:010d}" for i in range(n_accounts)]
    accounts = [
        Account(
            account_id=a, bank_id=a[:4], customer_id=f"C{i}", account_type=AccountType.INDIVIDUAL,
            opened_at=T0 - timedelta(days=float(rng.uniform(1, 2000))), opening_balance=50_000.0,
        )
        for i, a in enumerate(ids)
    ]  # fmt: skip
    customers = [
        Customer(
            customer_id=f"C{i}", name="x", mobile="9", pan_masked="X", kyc_tier=("MIN", "FULL", "VKYC")[i % 3]
        )
        for i in range(n_accounts)
    ]
    src = rng.integers(0, n_accounts, n_txns)
    dst = rng.integers(0, n_accounts, n_txns)
    offs = rng.uniform(-120 * 86400, 2 * 86400, n_txns)
    amounts = np.round(rng.lognormal(8, 1.5, n_txns), 2)
    kinds = rng.random(n_txns)
    txns = []
    for k in range(n_txns):
        frm, to, ch = ids[src[k]], ids[dst[k]], Channel.UPI
        if kinds[k] < 0.05:
            to, ch = None, Channel.ATM
        elif kinds[k] < 0.07:
            to, ch = None, Channel.CRYPTO
        elif frm == to:
            continue
        txns.append(
            Transaction.model_construct(
                txn_id=f"TX-{k:07d}", ts=T0 + timedelta(seconds=float(offs[k])), from_account=frm, to_account=to,
                from_bank_id=frm[:4], to_bank_id=to[:4] if to else None, amount=float(amounts[k]), channel=ch,
                utr=f"{k:012d}", remark="salary" if k % 97 == 0 else "", counterparty_label=None,
            )
        )  # fmt: skip
    links = [DeviceLink(device_id=f"DEV-{i // 3}", account_id=a, first_seen=T0 - timedelta(days=60))
             for i, a in enumerate(ids)]  # fmt: skip
    signals = [ExternalSignal(account_id=a, fri_risk=RiskLevel.HIGH, mulehunter_score=0.5) for a in ids[::5]]
    ledger = InMemoryLedger(
        banks=[Bank(bank_id=HOME, name="h", is_home=True), Bank(bank_id=OTHER, name="o")],
        accounts=accounts, transactions=txns, customers=customers, device_links=links, signals=signals,
    )  # fmt: skip
    return ledger, ids


def test_features_500_accounts_20k_txns_under_3s():
    ledger, ids = big_world()
    assert len(ledger.all_transactions()) > 19_000
    query = ids[:500]
    seeds = set(ids[:50])
    burst = {a: T0 + timedelta(minutes=i) for i, a in enumerate(query)}
    t = time.perf_counter()
    df = account_features(ledger, query, T0, ClassifyConfig(), seed_accounts=seeds, burst_start=burst)
    elapsed = time.perf_counter() - t
    assert elapsed < 3.0, f"account_features took {elapsed:.2f}s"
    assert df.shape == (500, len(FEATURE_COLUMNS))
    assert not df.isna().any().any()
    assert df["in_count_24h"].sum() > 0 and df["history_days"].max() > 60
    assert df["device_shared_count"].max() == 2 and df["shared_device_with_seed_accounts"].sum() > 0
    assert df["is_other_bank"].sum() == 125
