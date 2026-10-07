"""Offline fakes for Google Cloud clients (no network, no credentials).

``SqliteBigQuery``   executes the adapter's BigQuery SQL on SQLite after a small, explicit dialect translation
                     (table names, ``IN UNNEST(@p)``, ``STRPOS``, ``@p``) using the *real* query parameter objects
                     from ``google.cloud.bigquery``, so SQL logic and visibility semantics are really exercised.
``FakeFirestore``    in-memory documents/collections/batches with Firestore's 1 MiB document and 500-write limits,
                     and serializable transactions driven by the real ``google.cloud.firestore.transactional``.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import sqlite3
import threading
from collections.abc import Callable
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
MAX_COMMIT_WRITES = 500


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

    def get(self, field_paths: Any = None, transaction: FakeTransaction | None = None) -> FakeSnapshot:
        if transaction is not None:
            return FakeSnapshot(self.id, transaction._read_doc(self.path))
        with self._client.lock:
            self._client.reads += 1
            return FakeSnapshot(self.id, copy.deepcopy(self._client.docs.get(self.path)))

    def set(self, data: dict[str, Any]) -> None:
        with self._client.lock:
            self._client._write(self.path, data)

    def delete(self) -> None:
        with self._client.lock:
            self._client._delete(self.path)

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._client, f"{self.path}/{name}")


class FakeCollection:
    def __init__(self, client: FakeFirestore, path: str) -> None:
        self._client = client
        self.path = path

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(self._client, f"{self.path}/{doc_id}")

    def stream(self, transaction: FakeTransaction | None = None) -> list[FakeSnapshot]:
        """Direct children in document-id order (Firestore's default order)."""
        if transaction is not None:
            rows = transaction._read_collection(self.path)
        else:
            with self._client.lock:
                rows = self._client._children(self.path)
                self._client.reads += max(len(rows), 1)
        prefix = self.path + "/"
        return [FakeSnapshot(p[len(prefix):], d) for p, d in rows]


class FakeBatch:
    def __init__(self, client: FakeFirestore) -> None:
        self._client = client
        self._ops: list[tuple[str, str, Any]] = []

    def set(self, ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._ops.append(("set", ref.path, copy.deepcopy(data)))

    def delete(self, ref: FakeDocRef) -> None:
        self._ops.append(("delete", ref.path, None))

    def commit(self) -> None:
        if len(self._ops) > MAX_COMMIT_WRITES:
            raise ValueError("a batch can contain at most 500 writes")
        with self._client.lock:
            self._client._apply(self._ops)
            self._client.commits += 1
        self._ops = []


class FakeTransaction:
    """A Firestore transaction with the surface ``google.cloud.firestore.transactional`` drives (``_begin``,
    ``_commit``, ``_rollback``, ``_clean_up``, ``_max_attempts``, ``_read_only``), so the store runs under the
    real decorator and its real retry loop.

    Serializable, optimistic concurrency: ``_commit`` raises ``google.api_core.exceptions.Aborted`` (which the
    decorator retries) when any document or collection the transaction read has changed since it was read, and
    ``AlreadyExists`` (not retried) when ``create`` targets an existing document. Writes are buffered and applied
    atomically; reads after a write raise, as in the real client. Hooks on the client let tests interleave a
    competing commit (``before_commit``) or lose the response of a commit that went through (``after_commit``).
    """

    _ids = itertools.count(1)

    def __init__(self, client: FakeFirestore, max_attempts: int = 5, read_only: bool = False) -> None:
        self._client = client
        self._max_attempts = max_attempts
        self._read_only = read_only
        self._id: bytes | None = None
        self._write_pbs: list[tuple[str, str, Any]] = []
        self._doc_reads: dict[str, int] = {}
        self._coll_reads: dict[str, frozenset[tuple[str, int]]] = {}

    @property
    def in_progress(self) -> bool:
        return self._id is not None

    @property
    def id(self) -> bytes | None:
        return self._id

    def _clean_up(self) -> None:
        self._write_pbs = []
        self._doc_reads = {}
        self._coll_reads = {}
        self._id = None

    def _begin(self, retry_id: bytes | None = None) -> None:
        if self.in_progress:
            raise ValueError(f"The transaction has already begun. Current transaction ID: {self._id!r}.")
        self._id = f"txn-{next(self._ids)}".encode()
        with self._client.lock:
            self._client.txn_begins += 1

    def _check_read(self) -> None:
        if not self.in_progress:
            raise ValueError("read in a transaction that has not begun")
        if self._write_pbs:
            raise ValueError("Attempted read after write in a transaction.")

    def _read_doc(self, path: str) -> dict[str, Any] | None:
        self._check_read()
        with self._client.lock:
            self._client.reads += 1
            self._doc_reads.setdefault(path, self._client.versions.get(path, 0))
            return copy.deepcopy(self._client.docs.get(path))

    def _read_collection(self, path: str) -> list[tuple[str, dict[str, Any]]]:
        self._check_read()
        with self._client.lock:
            rows = self._client._children(path)
            self._client.reads += max(len(rows), 1)
            self._coll_reads.setdefault(path, self._client._signature(path))
            return rows

    def _buffer(self, op: str, ref: FakeDocRef, data: Any) -> None:
        if self._read_only:
            raise ValueError("Cannot perform write operation in read-only transaction.")
        self._write_pbs.append((op, ref.path, copy.deepcopy(data)))

    def create(self, ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._buffer("create", ref, data)

    def set(self, ref: FakeDocRef, data: dict[str, Any], merge: bool = False) -> None:
        if merge:
            raise NotImplementedError("merge writes are not used by Wapsi")
        self._buffer("set", ref, data)

    def delete(self, ref: FakeDocRef) -> None:
        self._buffer("delete", ref, None)

    def _commit(self) -> list[Any]:
        from google.api_core import exceptions

        if not self.in_progress:
            raise ValueError("The transaction has no transaction ID, so it cannot be committed.")
        hook = self._client.before_commit
        if hook is not None:
            hook(self)  # outside the lock: the hook may commit a competing transaction
        with self._client.lock:
            if len(self._write_pbs) > MAX_COMMIT_WRITES:
                raise exceptions.InvalidArgument("a transaction can contain at most 500 writes")
            changed = [p for p, v in self._doc_reads.items() if self._client.versions.get(p, 0) != v]
            changed += [p for p, sig in self._coll_reads.items() if self._client._signature(p) != sig]
            if changed:
                self._client.aborts += 1
                raise exceptions.Aborted(f"Transaction contention on {changed[0]}; retry the transaction")
            for op, path, _ in self._write_pbs:
                if op == "create" and path in self._client.docs:
                    raise exceptions.AlreadyExists(f"Document already exists: {path}")
            self._client._apply(self._write_pbs)
            self._client.commits += 1
            self._client.txn_commits += 1
        lost = self._client.after_commit
        if lost is not None:
            lost(self)  # e.g. raise ServiceUnavailable: the writes went through, the caller never heard back
        self._clean_up()
        return []

    def _rollback(self) -> None:
        if not self.in_progress:
            raise ValueError("The transaction has no transaction ID, so it cannot be rolled back.")
        with self._client.lock:
            self._client.rollbacks += 1
        self._clean_up()


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, int] = {}  # path -> version of its last write (deletes bump it too)
        self.lock = threading.RLock()
        self.commits = 0
        self.txn_begins = 0
        self.txn_commits = 0
        self.aborts = 0
        self.rollbacks = 0
        self.reads = 0
        self.max_doc_bytes_seen = 0
        self.before_commit: Callable[[FakeTransaction], None] | None = None
        self.after_commit: Callable[[FakeTransaction], None] | None = None
        self._clock = itertools.count(1)

    # -- storage (callers hold self.lock)
    def _write(self, path: str, data: dict[str, Any]) -> None:
        size = doc_size(data) + len(path.encode("utf-8")) + 16
        if size > MAX_DOC_BYTES:
            raise ValueError(f"document {path} is {size} bytes (> 1 MiB)")
        self.max_doc_bytes_seen = max(self.max_doc_bytes_seen, size)
        self.docs[path] = copy.deepcopy(data)
        self.versions[path] = next(self._clock)

    def _delete(self, path: str) -> None:
        if self.docs.pop(path, None) is not None:
            self.versions[path] = next(self._clock)

    def _apply(self, ops: list[tuple[str, str, Any]]) -> None:
        """All or nothing: validate every write first, then apply."""
        for op, path, data in ops:
            if op != "delete" and doc_size(data) + len(path.encode("utf-8")) + 16 > MAX_DOC_BYTES:
                raise ValueError(f"document {path} is larger than 1 MiB")
        for op, path, data in ops:
            if op == "delete":
                self._delete(path)
            else:
                self._write(path, data)

    def _children(self, path: str) -> list[tuple[str, dict[str, Any]]]:
        prefix = path + "/"
        return [(p, copy.deepcopy(d)) for p, d in sorted(self.docs.items())
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def _signature(self, path: str) -> frozenset[tuple[str, int]]:
        prefix = path + "/"
        return frozenset((p, self.versions.get(p, 0)) for p in self.docs
                         if p.startswith(prefix) and "/" not in p[len(prefix):])

    # -- client surface
    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)

    def batch(self) -> FakeBatch:
        return FakeBatch(self)

    def transaction(self, max_attempts: int = 5, read_only: bool = False) -> FakeTransaction:
        return FakeTransaction(self, max_attempts=max_attempts, read_only=read_only)
