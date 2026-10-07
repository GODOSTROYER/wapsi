"""SnowflakeLedger: query building (binds only) and Ledger-protocol parity with InMemoryLedger on the showcase."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wapsi.adapters import snowflake_ledger as sfl
from wapsi.adapters.snowflake_ledger import SnowflakeLedger, render_binds, row_dict
from wapsi.fixtures.showcase import HOME, N1, N2, N3, SHOWCASE_1, A, B, D, K, P, showcase_ledger
from wapsi.graph.trace import trace_case


class RecordingSession:
    """Returns canned rows and records (sql, params)."""

    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows or []

    def sql(self, query, params=None):
        self.calls.append((query, params))
        rows = self._rows

        class R:
            def collect(self_inner):
                return rows

        return R()


# --------------------------------------------------------------------------- binds


def test_render_binds_expands_lists_datetimes_and_keeps_casts_and_paths():
    ts = datetime(2026, 9, 10, 11, 2, tzinfo=sfl.ensure_utc(datetime(2026, 1, 1)).tzinfo)
    sql, params = render_binds(
        "SELECT a::STRING, f.value:\"x\" FROM t WHERE id IN (:ids) AND ts < TO_TIMESTAMP_NTZ(:ts) AND b = :b",
        {"ids": ["x", "y", "z"], "ts": ts, "b": 3},
    )
    assert sql == ("SELECT a::STRING, f.value:\"x\" FROM t WHERE id IN (?, ?, ?) AND ts < TO_TIMESTAMP_NTZ(?) "
                   "AND b = ?")
    assert params == ["x", "y", "z", "2026-09-10 11:02:00.000000", 3]
    assert render_binds("x IN (:ids)", {"ids": []}) == ("x IN (NULL)", [])
    with pytest.raises(KeyError):
        render_binds("x = :missing", {})


def test_ist_timestamps_are_bound_as_utc_wall_clock():
    from wapsi.timeutil import IST

    assert sfl.ts_param(datetime(2026, 9, 10, 11, 2, tzinfo=IST)) == "2026-09-10 05:32:00.000000"


def test_values_never_enter_the_sql_text():
    s = RecordingSession()
    led = SnowflakeLedger(s, "RAW")
    led._home = HOME
    led.transactions_touching([A, "x'; DROP TABLE t; --"], datetime(2026, 9, 1, tzinfo=UTC),
                              datetime(2026, 9, 30, tzinfo=UTC))
    led.balances_at([A, B], datetime(2026, 9, 10, tzinfo=UTC))
    led.complaint_transactions(N1)
    for sql, _params in s.calls:
        assert "DROP TABLE" not in sql and A not in sql and N1 not in sql
        assert "WAPSI.RAW." in sql
    sql, params = s.calls[1]
    assert params.count(A) == 3 and params.count("2026-09-10 00:00:00.000000") == 2  # ids x3, ts x2 in order
    assert [q for q, _ in led.queries] == ["transactions_touching", "balances_at", "complaint_transactions"]


def test_identifiers_are_validated():
    with pytest.raises(ValueError):
        SnowflakeLedger(RecordingSession(), "RAW; DROP SCHEMA X")
    assert SnowflakeLedger(RecordingSession(), "raw_showcase").table("banks") == "WAPSI.RAW_SHOWCASE.BANKS"


def test_row_dict_converts_snowflake_types():
    class Row:
        def as_dict(self):
            return {"AMOUNT": Decimal("480000.00"), "TS": datetime(2026, 9, 10, 5, 32), "NAME": "x"}

    d = row_dict(Row())
    assert d == {"amount": 480000.0, "ts": datetime(2026, 9, 10, 5, 32, tzinfo=UTC), "name": "x"}
    assert isinstance(d["amount"], float)


def test_long_id_lists_are_chunked(monkeypatch):
    monkeypatch.setattr(sfl, "MAX_IN_LIST", 2)
    s = RecordingSession()
    SnowflakeLedger(s).accounts(["a", "b", "c", "d", "e"])
    assert [len(p) for _, p in s.calls] == [2, 2, 1]


# --------------------------------------------------------------------------- parity with InMemoryLedger


@pytest.fixture()
def ledgers(sf_session):
    return showcase_ledger(), SnowflakeLedger(sf_session, "RAW_SHOWCASE")


def _ids(ledger):
    return sorted(ledger.all_accounts())


def test_protocol_answers_match_in_memory(ledgers):
    mem, sf = ledgers
    ids = _ids(mem)
    assert sf.home_bank_id() == mem.home_bank_id() == HOME
    assert sf.banks() == mem.banks()
    assert sf.accounts(ids) == mem.accounts(ids)
    cust = {a.customer_id for a in mem.accounts(ids).values() if a.customer_id}
    assert sf.customers(cust) == mem.customers(cust)
    for n in (N1, N2, N3):
        assert sf.complaint(n) == mem.complaint(n)
        assert sf.complaint_transactions(n) == mem.complaint_transactions(n)
    assert sf.complaints() == mem.complaints()
    since = datetime(2026, 9, 11, tzinfo=UTC)
    assert sf.complaints(since) == mem.complaints(since)
    start, end = datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    assert sf.transactions_touching(ids, start, end) == mem.transactions_touching(ids, start, end)
    assert sf.consortium_transactions(ids, start, end) == mem.consortium_transactions(ids, start, end)
    txns = [t.txn_id for t in mem.all_transactions()]
    assert sf.invoices_for_transactions(txns) == mem.invoices_for_transactions(txns)
    assert sf.external_signals(ids) == mem.external_signals(ids)
    key = lambda x: (x.account_id, x.device_id, x.first_seen)  # noqa: E731
    assert sf.device_links(ids) == sorted(mem.device_links(ids), key=key)
    assert sorted(sf.device_links_for_devices(["DEV-7781"]), key=key) == sorted(
        mem.device_links_for_devices(["DEV-7781"]), key=key)
    assert sf.gst_records(["27ABCDE1234F1Z5"]) == mem.gst_records(["27ABCDE1234F1Z5"])
    assert sf.meta()["world"] == "showcase"


def test_balances_match_at_every_transaction_time(ledgers):
    mem, sf = ledgers
    ids = _ids(mem)
    for t in mem.all_transactions()[::3]:
        for ts in (t.ts, t.ts + timedelta(seconds=1)):
            assert sf.balances_at(ids, ts) == mem.balances_at(ids, ts), ts


def test_trace_over_snowflake_sql_reproduces_showcase(ledgers):
    _, sf = ledgers
    tr = trace_case(sf, [N1])
    assert tr.totals.model_dump() == SHOWCASE_1["totals"]
    for acct, amount in SHOWCASE_1["tainted_now"].items():
        assert tr.accounts[acct].tainted_now_total == pytest.approx(amount, abs=0.005)


def test_prefetch_serves_complaints_from_two_queries(ledgers):
    mem, sf = ledgers
    sf.prefetch_complaints()
    before = len(sf.queries)
    for n in (N1, N2, N3):
        assert sf.complaint(n) == mem.complaint(n)
        assert sf.complaint_transactions(n) == mem.complaint_transactions(n)
    assert len(sf.queries) == before


def test_reachable_accounts_prefilter(ledgers):
    _, sf = ledgers
    layers = sf.reachable_accounts([N1], datetime(2026, 9, 13, tzinfo=UTC), max_depth=6)
    assert layers[A] == 1 and layers[B] == 2 and layers[D] == 3
    assert K in layers and P in layers
    with pytest.raises(ValueError):
        sf.reachable_accounts([N1], datetime(2026, 9, 13, tzinfo=UTC), max_depth=50)


def test_snapshot_mode_uses_parquet_ledger(sf_session, tmp_path):
    led = sfl.snapshot_ledger(sf_session, tmp_path / "world", "RAW_SHOWCASE")
    assert type(led).__name__ == "ParquetLedger"
    assert trace_case(led, [N1]).totals.model_dump() == SHOWCASE_1["totals"]


def test_meta_matches_the_parquet_ledger_shape(ledgers, sf_session, tmp_path):
    """``meta`` is a property like ``ParquetLedger.meta``: indexable, callable, a fresh copy, one query."""
    from wapsi.adapters.parquet_ledger import LedgerMeta

    _, sf = ledgers
    before = len(sf.queries)
    assert sf.meta["world"] == "showcase" and sf.meta()["world"] == "showcase"
    assert isinstance(sf.meta, LedgerMeta) and type(sf.meta()) is dict
    sf.meta["world"] = "tampered"
    assert sf.meta["world"] == "showcase"  # a copy every time
    assert [q for q, _ in sf.queries[before:]] == ["meta"]
    parquet = sfl.snapshot_ledger(sf_session, tmp_path / "world", "RAW_SHOWCASE")
    assert parquet.meta() == sf.meta() and parquet.meta["world"] == sf.meta["world"]
