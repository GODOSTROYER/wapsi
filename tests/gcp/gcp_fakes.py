"""Offline fakes for Google Cloud clients (no network, no credentials).

``SqliteBigQuery``   executes the adapter's BigQuery SQL on SQLite after a small, explicit dialect translation
                     (table names, ``IN UNNEST(@p)``, ``STRPOS``, ``@p``) using the *real* query parameter objects
                     from ``google.cloud.bigquery``, so SQL logic and visibility semantics are really exercised.
``FakeFirestore``    in-memory documents/collections/batches with Firestore's 1 MiB document and 500-write limits.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from wapsi.adapters.bigquery_ledger import SCHEMAS
from wapsi.timeutil import ensure_utc

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
TS_COLS = {"ts", "opened_at", "reported_at", "ordered_at", "delivered_at", "first_seen", "first_reached_at",
           "since", "start", "end", "window_end"}
DATE_COLS = {"dob", "registered_at"}
BOOL_COLS = {"is_home", "is_adversarial"}


def _to_sql_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return ensure_utc(v).strftime(TS_FMT)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    if hasattr(v, "value") and not isinstance(v, (int, float, str)):  # enums
        return v.value
    return v


def _from_sql(col: str, v: Any) -> Any:
    if v is None:
        return None
    if col in TS_COLS:
        return datetime.strptime(v, TS_FMT).replace(tzinfo=UTC)
    if col in DATE_COLS:
        return date.fromisoformat(v)
    if col in BOOL_COLS:
        return bool(v)
    return v


def translate(sql: str) -> str:
    sql = re.sub(r"`[^`]*\.([A-Za-z0-9_]+)`", r"\1", sql)
    sql = re.sub(r"IN\s+UNNEST\(@(\w+)\)", r"IN (SELECT value FROM json_each(:\1))", sql)
    sql = sql.replace("STRPOS(", "instr(")
    return re.sub(r"@(\w+)", r":\1", sql)


class _Job:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.output_rows = len(rows)

    def result(self) -> list[dict[str, Any]]:
        return self._rows


class SqliteBigQuery:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.calls: list[tuple[str, dict[str, Any], Any]] = []  # (sql, params, job_config)
        for table, cols in SCHEMAS.items():
            self.db.execute(f"CREATE TABLE {table} ({', '.join(c for c, _, _ in cols)})")

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        cols = [c for c, _, _ in SCHEMAS[table]]
        self.db.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [[_to_sql_value(r.get(c)) for c in cols] for r in rows],
        )

    @staticmethod
    def params(job_config: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in getattr(job_config, "query_parameters", []) or []:
            if hasattr(p, "values"):  # ArrayQueryParameter
                out[p.name] = json.dumps([_to_sql_value(v) for v in p.values])
            else:
                out[p.name] = _to_sql_value(p.value)
        return out

    def query(self, sql: str, job_config: Any = None) -> _Job:
        params = self.params(job_config)
        self.calls.append((sql, params, job_config))
        cur = self.db.execute(translate(sql), params)
        rows = [{k: _from_sql(k, r[k]) for k in r.keys()} for r in cur.fetchall()]
        return _Job(rows)


def load_ledger_into(fake: SqliteBigQuery, ledger: Any) -> None:
    """Copy an InMemoryLedger's world into the fake through its public API."""
    accounts = ledger.all_accounts()
    txns = ledger.all_transactions()
    complaints = ledger.complaints()
    fake.insert("banks", [b.model_dump() for b in ledger.banks().values()])
    fake.insert("accounts", [a.model_dump() for a in accounts.values()])
    fake.insert("transactions", [t.model_dump() for t in txns])
    fake.insert("complaints", [c.model_dump() for c in complaints])
    fake.insert("complaint_txns", [{"ncrp_id": c.ncrp_id, "txn_id": t.txn_id}
                                   for c in complaints for t in ledger.complaint_transactions(c.ncrp_id)])
    cust_ids = {a.customer_id for a in accounts.values() if a.customer_id}
    fake.insert("customers", [c.model_dump() for c in ledger.customers(cust_ids).values()])
    fake.insert("invoices", [i.model_dump() for i in ledger.invoices_for_transactions([t.txn_id for t in txns])])
    fake.insert("external_signals", [s.model_dump() for s in ledger.external_signals(accounts).values()])
    fake.insert("device_links", [d.model_dump() for d in ledger.device_links(accounts)])
    gstins = {a.gstin for a in accounts.values() if a.gstin}
    fake.insert("gst_registry", [g.model_dump() for g in ledger.gst_records(gstins).values()])


# --------------------------------------------------------------------------- Firestore

MAX_DOC_BYTES = 1_048_576


def doc_size(value: Any) -> int:
    """Approximate Firestore storage size (https://cloud.google.com/firestore/docs/storage-size)."""
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, (int, float, datetime)):
        return 8
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 1
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, list):
        return sum(doc_size(v) for v in value)
    if isinstance(value, dict):
        return sum(len(str(k).encode("utf-8")) + 1 + doc_size(v) for k, v in value.items())
    raise TypeError(f"unsupported Firestore value {type(value).__name__}")


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)


class FakeDocRef:
    def __init__(self, client: FakeFirestore, path: str) -> None:
        self._client = client
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self) -> FakeSnapshot:
        self._client.reads += 1
        return FakeSnapshot(self.id, self._client.docs.get(self.path))

    def set(self, data: dict[str, Any]) -> None:
        self._client._write(self.path, data)

    def delete(self) -> None:
        self._client.docs.pop(self.path, None)

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._client, f"{self.path}/{name}")


class FakeCollection:
    def __init__(self, client: FakeFirestore, path: str) -> None:
        self._client = client
        self.path = path

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(self._client, f"{self.path}/{doc_id}")

    def stream(self) -> list[FakeSnapshot]:
        prefix = self.path + "/"
        return [FakeSnapshot(p[len(prefix):], copy.deepcopy(d)) for p, d in sorted(self._client.docs.items())
                if p.startswith(prefix) and "/" not in p[len(prefix):]]


class FakeBatch:
    def __init__(self, client: FakeFirestore) -> None:
        self._client = client
        self._ops: list[tuple[str, str, Any]] = []

    def set(self, ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._ops.append(("set", ref.path, copy.deepcopy(data)))

    def delete(self, ref: FakeDocRef) -> None:
        self._ops.append(("delete", ref.path, None))

    def commit(self) -> None:
        if len(self._ops) > 500:
            raise ValueError("a batch can contain at most 500 writes")
        for op, path, data in self._ops:
            if op == "set":
                self._client._write(path, data)
            else:
                self._client.docs.pop(path, None)
        self._client.commits += 1
        self._ops = []


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.commits = 0
        self.reads = 0
        self.max_doc_bytes_seen = 0

    def _write(self, path: str, data: dict[str, Any]) -> None:
        size = doc_size(data) + len(path.encode("utf-8")) + 16
        if size > MAX_DOC_BYTES:
            raise ValueError(f"document {path} is {size} bytes (> 1 MiB)")
        self.max_doc_bytes_seen = max(self.max_doc_bytes_seen, size)
        self.docs[path] = copy.deepcopy(data)

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)

    def batch(self) -> FakeBatch:
        return FakeBatch(self)
