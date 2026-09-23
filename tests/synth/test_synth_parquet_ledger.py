"""ParquetLedger must behave exactly like InMemoryLedger (every Ledger protocol method)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from wapsi.adapters.memory import InMemoryLedger
from wapsi.adapters.parquet_ledger import ParquetLedger
from wapsi.fixtures import showcase as sc
from wapsi.ports import Ledger
from wapsi.synth.export import export_ledger, frames_to_memory_ledger, load_frames
from wapsi.timeutil import IST

START = datetime(2026, 8, 1, tzinfo=IST)
END = datetime(2026, 9, 30, tzinfo=IST)


def _links(xs):
    return sorted(xs, key=lambda x: (x.account_id, x.device_id, x.first_seen))


def assert_equivalent(
    pl: ParquetLedger, ml: InMemoryLedger, account_ids: list[str], windows, ncrp_ids, stamps
) -> None:
    assert pl.home_bank_id() == ml.home_bank_id()
    assert pl.banks() == ml.banks()
    assert pl.complaints() == ml.complaints()
    for since in stamps:
        assert pl.complaints(since) == ml.complaints(since)
    for n in ncrp_ids:
        assert pl.complaint(n) == ml.complaint(n)
        assert pl.complaint_transactions(n) == ml.complaint_transactions(n)
    with pytest.raises(KeyError):
        pl.complaint("00000000000000")
    ids = [*account_ids, "HB01-0000000000", account_ids[0]]
    assert pl.accounts(ids) == ml.accounts(ids)
    cust = [a.customer_id for a in ml.accounts(account_ids).values() if a.customer_id] + ["CUST-NOPE"]
    assert pl.customers(cust) == ml.customers(cust)
    for ts in stamps:
        assert pl.balances_at(ids, ts) == ml.balances_at(ids, ts), ts
    for start, end in windows:
        assert pl.transactions_touching(ids, start, end) == ml.transactions_touching(ids, start, end)
        assert pl.consortium_transactions(ids, start, end) == ml.consortium_transactions(ids, start, end)
        for a in account_ids[:6]:
            assert pl.transactions_touching([a], start, end) == ml.transactions_touching([a], start, end)
            assert pl.consortium_transactions([a], start, end) == ml.consortium_transactions([a], start, end)
    txn_ids = [t.txn_id for t in ml.transactions_touching(account_ids, START, END)] + ["TX-NOPE"]
    assert pl.invoices_for_transactions(txn_ids) == ml.invoices_for_transactions(txn_ids)
    assert pl.external_signals(ids) == ml.external_signals(ids)
    links = pl.device_links(ids)
    assert links == _links(links) and links == _links(ml.device_links(ids))
    devices = sorted({link.device_id for link in links}) + ["DEV-NOPE"]
    assert _links(pl.device_links_for_devices(devices)) == _links(ml.device_links_for_devices(devices))
    gstins = [a.gstin for a in ml.accounts(account_ids).values() if a.gstin] + ["27XXXXX0000X1Z0"]
    assert pl.gst_records(gstins) == ml.gst_records(gstins)


@pytest.fixture(scope="module")
def showcase_pair(tmp_path_factory):
    ml = sc.showcase_ledger()
    path = tmp_path_factory.mktemp("sc_export")
    export_ledger(ml, path, name="showcase")
    return ParquetLedger(path), ml


def test_protocol(showcase_pair):
    pl, _ = showcase_pair
    assert isinstance(pl, Ledger)


def test_showcase_equivalence(showcase_pair):
    pl, ml = showcase_pair
    ids = sorted(ml.all_accounts())
    t = [
        datetime(2026, 9, 10, 11, 2, tzinfo=IST),
        datetime(2026, 9, 10, 11, 9, tzinfo=IST),
        datetime(2026, 9, 10, 12, 0, tzinfo=IST),
        START,
        END,
        datetime(2026, 9, 11, 15, 10),
    ]  # naive = UTC
    windows = [
        (START, END),
        (t[0], t[0]),
        (t[0], t[2]),
        (datetime(2026, 9, 10, 11, 3, tzinfo=IST), t[2]),
        (END, END + timedelta(days=1)),
    ]
    assert_equivalent(pl, ml, ids, windows, [sc.N1, sc.N2, sc.N3], t)
    # exact spot values from the fixture
    assert pl.balances_at([sc.A], t[1]) == {sc.A: 500000.0}  # strictly before 11:09
    assert [x.txn_id for x in pl.transactions_touching([sc.D, sc.E], START, END)] == ["TX-009", "TX-010"]
    assert [x.txn_id for x in pl.consortium_transactions([sc.E, sc.D], START, END)] == ["TX-010", "TX-011"]
    assert pl.all_accounts() == ml.all_accounts()
    assert pl.all_transactions() == ml.all_transactions()


def test_showcase_world_dir(showcase_dir, showcase_memory):
    """The CLI's showcase world (with labels) loads and matches the fixture ledger."""
    pl = ParquetLedger(showcase_dir)
    assert pl.balances_at(list(sc.SHOWCASE_1["balance_now"]), END) == sc.SHOWCASE_1["balance_now"]
    labels = pl.labels()
    assert set(labels) == {"labels_accounts", "labels_flows", "labels_complaints"}
    assert pl.meta()["size"] == "showcase"
    assert pl.all_transactions() == showcase_memory.all_transactions()


def test_demo_equivalence(demo_dir):
    frames = load_frames(demo_dir)
    ml = frames_to_memory_ledger(frames)
    pl = ParquetLedger(demo_dir)
    rng = np.random.default_rng(0)
    la = frames["labels_accounts"]
    ring = la.loc[la["ring_id"].notna(), "account_id"].tolist()
    acc = frames["accounts"]
    pools = acc.loc[acc["account_type"].isin(["POOL", "NODAL"]), "account_id"].tolist()[:2]
    other = acc.loc[acc["bank_id"] != "HB01", "account_id"].tolist()
    ids = sorted(
        set(rng.choice(ring, 25, replace=False).tolist())
        | set(rng.choice(other, 10).tolist())
        | set(rng.choice(acc["account_id"], 15).tolist())
        | set(pools)
    )
    comp = ml.complaints()
    seed_ts = [ml.complaint_transactions(c.ncrp_id)[0].ts for c in comp[:5]]
    windows = [(START, END)] + [(s, s + timedelta(hours=72)) for s in seed_ts]
    stamps = [*seed_ts, *(s + timedelta(seconds=1) for s in seed_ts), END, comp[len(comp) // 2].reported_at]
    assert_equivalent(pl, ml, ids, windows, [c.ncrp_id for c in comp], stamps)
    assert pl.labels()["labels_flows"].equals(frames["labels_flows"])


def test_missing_world(tmp_path):
    with pytest.raises(FileNotFoundError):
        ParquetLedger(tmp_path)
