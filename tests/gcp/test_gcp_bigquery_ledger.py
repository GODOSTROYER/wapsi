from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from gcp_fakes import SqliteBigQuery, load_ledger_into, translate

from wapsi.adapters.bigquery_ledger import (
    CLUSTERING,
    SCHEMAS,
    SQL,
    BigQueryLedger,
    load_job_config,
    load_world,
    setup_sql,
)
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.fixtures import showcase as sc
from wapsi.money import eq
from wapsi.pipeline.run import process_case
from wapsi.ports import Ledger

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mem():
    return sc.showcase_ledger()


@pytest.fixture()
def bq(mem):
    fake = SqliteBigQuery()
    load_ledger_into(fake, mem)
    return BigQueryLedger("wapsi-test", "wapsi", client=fake), fake


def test_implements_protocol(bq):
    assert isinstance(bq[0], Ledger)


def test_sql_is_parameterized_and_values_never_inlined(bq, mem):
    ledger, fake = bq
    ids = [sc.A, sc.C, "OB99-0000000000' OR 1=1 --"]
    start, end = sc._ist(9, 10, 0, 0), sc._ist(9, 12, 0, 0)
    ledger.transactions_touching(ids, start, end)
    ledger.consortium_transactions(ids, start, end)
    ledger.balances_at(ids, end)
    ledger.accounts(ids)
    ledger.complaint(sc.N1)
    assert fake.calls
    for sql, params, job_config in fake.calls:
        assert "@" in sql or not params
        for value in (sc.A, sc.C, sc.N1, "OR 1=1", "2026-09"):
            assert value not in sql, (value, sql)
        names = {p.name for p in job_config.query_parameters}
        assert names == set(re.findall(r"@(\w+)", sql))
    # the injection-shaped id is just an unknown account
    assert ledger.accounts(["x' OR '1'='1"]) == {}


def test_query_parameter_types(bq):
    ledger, fake = bq
    ledger.transactions_touching([sc.A], sc._ist(9, 10, 0, 0), sc._ist(9, 11, 0, 0))
    cfg = fake.calls[-1][2]
    by_name = {p.name: p for p in cfg.query_parameters}
    assert by_name["ids"].array_type == "STRING" and list(by_name["ids"].values) == [sc.A]
    assert by_name["start"].type_ == "TIMESTAMP" and by_name["start"].value.tzinfo is not None
    assert by_name["home_bank"].value == "HB01"
    assert cfg.maximum_bytes_billed == 20 * 1024**3
    assert cfg.labels == {"app": "wapsi"}


def test_transaction_queries_filter_on_partition_column():
    for name in ("transactions_touching", "consortium_transactions"):
        sql = SQL[name]
        assert "t.ts >= @start" in sql and "t.ts <= @end" in sql
    assert "ts < @ts" in SQL["balances_at"]


def test_invalid_identifiers_rejected():
    with pytest.raises(ValueError):
        BigQueryLedger("proj", "wapsi`; DROP TABLE x; --", client=object())
    with pytest.raises(ValueError):
        BigQueryLedger("", "wapsi", client=object())


def _key(txns):
    return [t.txn_id for t in txns]


def test_visibility_semantics_match_in_memory(bq, mem):
    ledger, _ = bq
    everyone = list(mem.all_accounts())
    windows = [(sc._ist(9, 1, 0, 0), sc._ist(9, 30, 0, 0)), (sc._ist(9, 10, 11, 2), sc._ist(9, 10, 12, 5)),
               (sc._ist(9, 10, 11, 9), sc._ist(9, 10, 11, 9))]
    probe_sets = [everyone, [sc.A], [sc.C, sc.E], [sc.V1], [sc.M, sc.V2, sc.V3], ["NOPE-0000000000"]]
    for start, end in windows:
        for ids in probe_sets:
            assert _key(ledger.transactions_touching(ids, start, end)) == _key(
                mem.transactions_touching(ids, start, end)), (ids, start)
            assert _key(ledger.consortium_transactions(ids, start, end)) == _key(
                mem.consortium_transactions(ids, start, end)), (ids, start)
    # other-bank internal hop (E -> ATM) is only visible through the consortium view
    day = (sc._ist(9, 10, 0, 0), sc._ist(9, 11, 0, 0))
    assert "TX-011" not in _key(ledger.transactions_touching([sc.E], *day))
    assert "TX-011" in _key(ledger.consortium_transactions([sc.E], *day))
    assert ledger.consortium_transactions([sc.A], *day) == []


def test_models_round_trip_identically(bq, mem):
    ledger, _ = bq
    everyone = list(mem.all_accounts())
    start, end = sc._ist(8, 1, 0, 0), sc._ist(9, 30, 0, 0)
    assert ledger.transactions_touching(everyone, start, end) == mem.transactions_touching(everyone, start, end)
    assert ledger.accounts(everyone) == mem.accounts(everyone)
    assert ledger.banks() == mem.banks()
    assert ledger.home_bank_id() == "HB01"
    assert ledger.complaints() == mem.complaints()
    assert ledger.complaints(since=sc._ist(9, 11, 0, 0)) == mem.complaints(since=sc._ist(9, 11, 0, 0))
    for n in (sc.N1, sc.N2, sc.N3):
        assert ledger.complaint(n) == mem.complaint(n)
        assert ledger.complaint_transactions(n) == mem.complaint_transactions(n)
    with pytest.raises(KeyError):
        ledger.complaint("00000000000000")
    assert ledger.external_signals(everyone) == mem.external_signals(everyone)
    assert ledger.gst_records([sc.K_GSTIN, "nope"]) == mem.gst_records([sc.K_GSTIN, "nope"])
    assert ledger.invoices_for_transactions(["TX-007"]) == mem.invoices_for_transactions(["TX-007"])
    assert sorted(ledger.device_links([sc.A, sc.B]), key=repr) == sorted(mem.device_links([sc.A, sc.B]), key=repr)
    assert sorted(ledger.device_links_for_devices(["DEV-7781"]), key=repr) == sorted(
        mem.device_links_for_devices(["DEV-7781"]), key=repr)
    custs = {a.customer_id for a in mem.all_accounts().values() if a.customer_id}
    assert ledger.customers(custs) == mem.customers(custs)
    assert ledger.customers([]) == {} and ledger.transactions_touching([], start, end) == []


def test_balances_match_in_memory(bq, mem):
    ledger, _ = bq
    everyone = list(mem.all_accounts())
    stamps = [t.ts for t in mem.all_transactions()]
    probes = sorted({*stamps, *(s + timedelta(microseconds=1) for s in stamps), sc._ist(9, 30, 0, 0)})
    for ts in probes:
        got, want = ledger.balances_at(everyone, ts), mem.balances_at(everyone, ts)
        assert got.keys() == want.keys()
        for a in want:
            assert eq(got[a], want[a]), (a, ts)
    # strictly before ts: the victim transfer at 11:02 is not yet in A's balance at 11:02
    t1 = mem.complaint_transactions(sc.N1)[0].ts
    assert eq(ledger.balances_at([sc.A], t1)[sc.A] + 480000.0, ledger.balances_at([sc.A], t1 + timedelta(seconds=1))[sc.A])


def test_pipeline_on_bigquery_ledger_reproduces_showcase(bq):
    ledger, _ = bq
    for exp in (sc.SHOWCASE_1, sc.SHOWCASE_2):
        b = process_case(ledger, exp["ncrp_ids"], llm=FakeLLM())
        for field, value in exp["totals"].items():
            assert eq(getattr(b.trace.totals, field), value), field
        holds = {d.account_id: d.hold_amount for d in b.plan.decisions}
        for acct, value in exp["hold"].items():
            assert eq(holds[acct], value), acct
        assert b.plan.restoration_process == exp["process"]
        assert all(r.passed for r in b.verifications.values())


def test_reachability_prefilter_recursive_cte(bq):
    ledger, fake = bq
    got = ledger.reachable_accounts([sc.N1], sc._ist(9, 13, 11, 2), max_depth=6)
    for acct, layer in sc.SHOWCASE_1["layers"].items():
        if layer >= 1:
            assert got.get(acct) == layer, acct
    assert sc.M not in got  # showcase 2 accounts are not reachable from N1
    sql = fake.calls[-1][0]
    assert "WITH RECURSIVE" in sql and "r.depth < @max_depth" in sql and "STRPOS(r.path" in sql
    shallow = ledger.reachable_accounts([sc.N1], sc._ist(9, 13, 11, 2), max_depth=1)
    assert shallow == {sc.A: 1}
    with pytest.raises(ValueError):
        ledger.reachable_accounts([sc.N1], sc._ist(9, 13, 0, 0), max_depth=0)


def test_translation_helper_is_explicit():
    assert translate("SELECT * FROM `p.d.accounts` WHERE account_id IN UNNEST(@ids)") == (
        "SELECT * FROM accounts WHERE account_id IN (SELECT value FROM json_each(:ids))")


class _LoadClient:
    def __init__(self):
        self.loads = []
        self.datasets = []

    def create_dataset(self, ds, exists_ok=False):
        self.datasets.append((ds, exists_ok))

    def load_table_from_file(self, fh, table_id, job_config=None):
        data = fh.read()
        self.loads.append((table_id, job_config, len(data)))

        class _J:
            output_rows = 3

            def result(self_inner):
                return self_inner

        return _J()


def _write_world(tmp_path: Path) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    for table in ("banks", "accounts", "transactions", "labels_accounts"):
        cols = {c: pa.array([None], type=pa.string()) for c, _, _ in SCHEMAS[table]}
        pq.write_table(pa.table(cols), tmp_path / f"{table}.parquet")
    return tmp_path


def test_load_world_uses_partitioning_and_clustering(tmp_path):
    world = _write_world(tmp_path)
    client = _LoadClient()
    counts = load_world(world, "wapsi-test", "wapsi", client=client, location="asia-south1")
    assert counts == {"banks": 3, "accounts": 3, "transactions": 3}  # labels skipped by default
    assert client.datasets and client.datasets[0][1] is True
    tables = {t: cfg for t, cfg, _ in client.loads}
    assert set(tables) == {"wapsi-test.wapsi.banks", "wapsi-test.wapsi.accounts", "wapsi-test.wapsi.transactions"}
    tx = tables["wapsi-test.wapsi.transactions"]
    assert tx.source_format == "PARQUET" and tx.write_disposition == "WRITE_TRUNCATE"
    assert tx.time_partitioning.field == "ts" and tx.time_partitioning.type_ == "DAY"
    assert tx.clustering_fields == ["from_account", "to_account"]
    assert [f.name for f in tx.schema] == [c for c, _, _ in SCHEMAS["transactions"]]
    assert tables["wapsi-test.wapsi.banks"].time_partitioning is None
    with_labels = load_world(world, "wapsi-test", "wapsi", client=_LoadClient(), include_labels=True)
    assert "labels_accounts" in with_labels
    with pytest.raises(FileNotFoundError):
        load_world(tmp_path / "missing", "p", "d", client=_LoadClient())


def test_load_job_config_every_table():
    for table in SCHEMAS:
        cfg = load_job_config(table)
        assert cfg.clustering_fields == CLUSTERING.get(table)


def test_setup_sql_and_deploy_file_agree():
    ddl = setup_sql("PROJECT_ID", "wapsi")
    assert "PARTITION BY DATE(ts)\nCLUSTER BY from_account, to_account" in ddl
    deploy = (ROOT / "editions" / "gcp" / "deploy" / "bigquery_setup.sql").read_text(encoding="utf-8")
    for table in SCHEMAS:
        assert f"`PROJECT_ID.wapsi.{table}`" in deploy, table
    assert "PARTITION BY DATE(ts)" in deploy and "CLUSTER BY from_account, to_account" in deploy
    assert "WITH RECURSIVE" in deploy and "@max_depth" in deploy


@pytest.mark.skipif(not (ROOT / "data" / "generated" / "demo" / "transactions.parquet").exists(),
                    reason="demo world not generated")
def test_parity_with_parquet_demo_world_sample():
    import pyarrow.parquet as pq

    from wapsi.adapters.parquet_ledger import ParquetLedger

    pl = ParquetLedger(ROOT / "data" / "generated" / "demo")
    fake = SqliteBigQuery()
    for table in SCHEMAS:
        f = ROOT / "data" / "generated" / "demo" / f"{table}.parquet"
        if f.exists() and not table.startswith("labels"):
            fake.insert(table, pq.read_table(f).to_pylist())
    bq = BigQueryLedger("p", "d", client=fake)
    complaints = pl.complaints()[:5]
    for c in complaints:
        assert bq.complaint_transactions(c.ncrp_id) == pl.complaint_transactions(c.ncrp_id)
        firsts = [t.to_account for t in pl.complaint_transactions(c.ncrp_id) if t.to_account]
        start, end = c.reported_at - timedelta(days=3), c.reported_at + timedelta(days=3)
        assert _key(bq.transactions_touching(firsts, start, end)) == _key(pl.transactions_touching(firsts, start, end))
        got, want = bq.balances_at(firsts, end), pl.balances_at(firsts, end)
        assert all(eq(got[a], want[a]) for a in want)
