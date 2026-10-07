"""``SnowflakeLedger``: ``wapsi.ports.Ledger`` over a generated world loaded into Snowflake (``WAPSI.RAW``).

Snowflake edition twin of :mod:`wapsi.adapters.bigquery_ledger` (one parameterized query per protocol call,
pushed down to the warehouse) with the snapshot option of :mod:`wapsi.adapters.parquet_ledger`
(:func:`snapshot_ledger` pulls a whole schema into a local ``ParquetLedger``).

``session`` is a Snowpark ``Session`` (inside a stored procedure: the handler's session) or anything with
``sql(query, params=[...]).collect()``. Every value (account ids, NCRP ids, timestamps) is a ``?`` bind
parameter; only validated identifiers (database, schema) are formatted into the SQL text. Timestamps are bound as
UTC strings and cast with ``TO_TIMESTAMP_NTZ`` (the world stores UTC wall-clock time in ``TIMESTAMP_NTZ``).

Visibility semantics match ``wapsi.adapters.memory.InMemoryLedger``:
- ``transactions_touching`` returns transactions where either side is a *home-bank account* (per ``ACCOUNTS``);
- ``consortium_transactions`` exposes transactions of known *other-bank* accounts (simulated CFCFRMS responses);
- ``balances_at`` = opening_balance + credits − debits strictly before ``ts`` (self-transfers net to zero).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wapsi.models import (
    Account,
    Bank,
    Complaint,
    Customer,
    DeviceLink,
    ExternalSignal,
    GstRecord,
    Invoice,
    Transaction,
)
from wapsi.timeutil import ensure_utc

if TYPE_CHECKING:  # pyarrow is imported only when meta is read
    from wapsi.adapters.parquet_ledger import LedgerMeta

DATABASE = "WAPSI"
DEFAULT_SCHEMA = "RAW"
WORLD_TABLES = (
    "banks",
    "customers",
    "accounts",
    "transactions",
    "invoices",
    "gst_registry",
    "external_signals",
    "device_links",
    "complaints",
    "complaint_txns",
    "meta",
)
LABEL_TABLES = ("labels_accounts", "labels_flows", "labels_complaints")
MAX_IN_LIST = 1000  # ids per IN (...) list; longer lists are chunked into several queries
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_PARAM = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)")
TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# --------------------------------------------------------------------------- SQL (static; binds only)
# ``{name}`` is a validated fully-qualified table name; ``:param`` is expanded to ``?`` binds by render_binds.

SQL: dict[str, str] = {
    "banks": "SELECT bank_id, name, is_home FROM {banks} ORDER BY bank_id",
    "complaint": "SELECT * FROM {complaints} WHERE ncrp_id = :ncrp_id LIMIT 1",
    "complaints_all": "SELECT * FROM {complaints} ORDER BY reported_at, ncrp_id",
    "complaints_since": (
        "SELECT * FROM {complaints} WHERE reported_at >= TO_TIMESTAMP_NTZ(:since) ORDER BY reported_at, ncrp_id"
    ),
    "complaint_transactions": (
        "SELECT t.* FROM {transactions} t JOIN {complaint_txns} ct ON ct.txn_id = t.txn_id "
        "WHERE ct.ncrp_id = :ncrp_id ORDER BY t.ts, t.txn_id"
    ),
    "complaint_transactions_all": (
        "SELECT ct.ncrp_id AS ct_ncrp_id, t.* FROM {transactions} t JOIN {complaint_txns} ct "
        "ON ct.txn_id = t.txn_id ORDER BY ct.ncrp_id, t.ts, t.txn_id"
    ),
    "accounts": "SELECT * FROM {accounts} WHERE account_id IN (:ids)",
    "customers": "SELECT * FROM {customers} WHERE customer_id IN (:ids)",
    "balances_at": """
WITH acct AS (
  SELECT account_id, opening_balance FROM {accounts} WHERE account_id IN (:ids)
),
moves AS (
  SELECT to_account AS account_id, amount AS delta
  FROM {transactions}
  WHERE ts < TO_TIMESTAMP_NTZ(:ts) AND to_account IN (:ids)
    AND (from_account IS NULL OR from_account <> to_account)
  UNION ALL
  SELECT from_account AS account_id, -amount AS delta
  FROM {transactions}
  WHERE ts < TO_TIMESTAMP_NTZ(:ts) AND from_account IN (:ids)
    AND (to_account IS NULL OR from_account <> to_account)
)
SELECT a.account_id AS account_id,
       ROUND(COALESCE(a.opening_balance, 0) + COALESCE(SUM(m.delta), 0), 2) AS balance
FROM acct a
LEFT JOIN moves m ON m.account_id = a.account_id
GROUP BY a.account_id, a.opening_balance
""",
    "transactions_touching": """
WITH home AS (
  SELECT account_id FROM {accounts} WHERE bank_id = :home_bank
)
SELECT t.*
FROM {transactions} t
WHERE t.ts >= TO_TIMESTAMP_NTZ(:start) AND t.ts <= TO_TIMESTAMP_NTZ(:end)
  AND (t.from_account IN (:ids) OR t.to_account IN (:ids))
  AND (t.from_account IN (SELECT account_id FROM home) OR t.to_account IN (SELECT account_id FROM home))
ORDER BY t.ts, t.txn_id
""",
    "consortium_transactions": """
WITH other AS (
  SELECT account_id FROM {accounts}
  WHERE account_id IN (:ids) AND bank_id <> :home_bank
)
SELECT t.*
FROM {transactions} t
WHERE t.ts >= TO_TIMESTAMP_NTZ(:start) AND t.ts <= TO_TIMESTAMP_NTZ(:end)
  AND (t.from_account IN (SELECT account_id FROM other) OR t.to_account IN (SELECT account_id FROM other))
ORDER BY t.ts, t.txn_id
""",
    "invoices": "SELECT * FROM {invoices} WHERE txn_id IN (:ids) ORDER BY invoice_id",
    "external_signals": "SELECT * FROM {external_signals} WHERE account_id IN (:ids)",
    "device_links": (
        "SELECT * FROM {device_links} WHERE account_id IN (:ids) ORDER BY account_id, device_id, first_seen"
    ),
    "device_links_for_devices": (
        "SELECT * FROM {device_links} WHERE device_id IN (:ids) ORDER BY device_id, account_id, first_seen"
    ),
    "gst_records": "SELECT * FROM {gst_registry} WHERE gstin IN (:ids)",
    "meta": "SELECT key, value FROM {meta}",
    # Optional pre-filter (not used by trace_case): accounts reachable from the complaint transfers. Snowflake
    # recursive CTEs have no engine depth cap, so the query carries a depth guard (:max_depth), a temporal
    # guard (a hop never precedes the hop that funded it), a window end, a minimum amount and a visited-path
    # guard (the '|a|b|' path string) against cycles. The warehouse STATEMENT_TIMEOUT is the backstop.
    "reachable_accounts": """
WITH RECURSIVE reach (account_id, depth, reached_at, path) AS (
  SELECT t.to_account, 1, t.ts, '|' || COALESCE(t.from_account, '') || '|' || t.to_account || '|'
  FROM {transactions} t
  JOIN {complaint_txns} ct ON ct.txn_id = t.txn_id
  WHERE ct.ncrp_id IN (:ncrp_ids) AND t.to_account IS NOT NULL
  UNION ALL
  SELECT t.to_account, r.depth + 1, t.ts, r.path || t.to_account || '|'
  FROM reach r
  JOIN {transactions} t ON t.from_account = r.account_id
  WHERE r.depth < :max_depth
    AND t.to_account IS NOT NULL
    AND t.ts >= r.reached_at
    AND t.ts <= TO_TIMESTAMP_NTZ(:window_end)
    AND t.amount >= :min_amount
    AND NOT CONTAINS(r.path, '|' || t.to_account || '|')
)
SELECT account_id, MIN(depth) AS layer, MIN(reached_at) AS first_reached_at, COUNT(*) AS paths
FROM reach
GROUP BY account_id
ORDER BY layer, account_id
""",
}

# --------------------------------------------------------------------------- helpers (shared by wapsi_sf)


def validate_identifier(value: str, what: str = "identifier") -> str:
    if not value or not _IDENT.match(value):
        raise ValueError(f"invalid Snowflake {what}: {value!r}")
    return value.upper()


def ts_param(ts: datetime) -> str:
    """UTC wall-clock string for ``TO_TIMESTAMP_NTZ(?)`` binds (the world stores UTC in TIMESTAMP_NTZ)."""
    return ensure_utc(ts).replace(tzinfo=None).strftime(TS_FORMAT)


def _bind_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return ts_param(value)
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, int | float | str | bool):  # enums
        return value.value
    return value


def render_binds(template: str, params: Mapping[str, Any] | None = None) -> tuple[str, list[Any]]:
    """Expand ``:name`` placeholders (left to right) into ``?`` binds and the matching parameter list.

    A list or tuple value becomes ``?, ?, ...`` (an empty one becomes ``NULL``, which matches nothing); a
    datetime becomes a UTC string. ``::TYPE`` casts and ``col:path`` JSON paths are not placeholders.
    """
    params = dict(params or {})
    out: list[Any] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing bind parameter :{name}")
        v = params[name]
        if isinstance(v, list | tuple | set | frozenset):
            items = list(v)
            if not items:
                return "NULL"
            out.extend(_bind_value(x) for x in items)
            return ", ".join("?" * len(items))
        out.append(_bind_value(v))
        return "?"

    return _PARAM.sub(repl, template), out


def to_py(value: Any) -> Any:
    """Snowflake -> Python: Decimal -> float, naive TIMESTAMP_NTZ -> aware UTC."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value


def row_dict(row: Any) -> dict[str, Any]:
    """A Snowpark ``Row`` (upper-case keys) as a lower-case dict of Python values."""
    if hasattr(row, "as_dict"):
        data = row.as_dict()
    elif isinstance(row, Mapping):
        data = dict(row)
    elif hasattr(row, "_asdict"):
        data = row._asdict()
    else:
        raise TypeError(f"unsupported row type {type(row).__name__}")
    return {str(k).lower(): to_py(v) for k, v in data.items()}


def _model(cls: type, row: Mapping[str, Any]) -> Any:
    fields = cls.model_fields
    return cls.model_validate({k: v for k, v in row.items() if k in fields and v is not None})


def _ids(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _chunks(ids: list[str], size: int | None = None) -> Iterable[list[str]]:
    size = size or MAX_IN_LIST
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


# --------------------------------------------------------------------------- ledger


class SnowflakeLedger:
    """Implements ``wapsi.ports.Ledger`` with parameterized Snowflake SQL over ``<database>.<schema>``."""

    def __init__(self, session: Any, schema: str = DEFAULT_SCHEMA, database: str = DATABASE) -> None:
        self.session = session
        self.database = validate_identifier(database, "database")
        self.schema = validate_identifier(schema, "schema")
        self._tables = {t: f"{self.database}.{self.schema}.{t.upper()}" for t in (*WORLD_TABLES, *LABEL_TABLES)}
        self._banks: dict[str, Bank] | None = None
        self._home: str | None = None
        self._account_cache: dict[str, Account] = {}
        self._complaints: dict[str, Complaint] | None = None
        self._complaint_txns: dict[str, list[Transaction]] | None = None
        self._meta: dict[str, str] | None = None
        self.queries: list[tuple[str, list[Any]]] = []  # (query name, binds): observability + tests

    # ------------------------------------------------------------------ plumbing
    def table(self, name: str) -> str:
        return self._tables[name]

    def sql(self, name: str, params: Mapping[str, Any] | None = None) -> tuple[str, list[Any]]:
        return render_binds(SQL[name].format(**self._tables), params)

    def _query(self, name: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        text, binds = self.sql(name, params)
        self.queries.append((name, binds))
        rows = self.session.sql(text, params=binds or None).collect()
        return [row_dict(r) for r in rows]

    def _query_ids(self, name: str, ids: list[str], **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for chunk in _chunks(ids):
            out.extend(self._query(name, {"ids": chunk, **params}))
        return out

    def prefetch_complaints(self) -> None:
        """Load every complaint and its reported transfers in two queries (case grouping reads all of them)."""
        self._complaints = {c.ncrp_id: c for c in (_model(Complaint, r) for r in self._query("complaints_all"))}
        by_id: dict[str, list[Transaction]] = {}
        for r in self._query("complaint_transactions_all"):
            by_id.setdefault(r.pop("ct_ncrp_id"), []).append(_model(Transaction, r))
        self._complaint_txns = by_id

    # ------------------------------------------------------------------ Ledger protocol
    def banks(self) -> dict[str, Bank]:
        if self._banks is None:
            self._banks = {r["bank_id"]: _model(Bank, r) for r in self._query("banks")}
        return dict(self._banks)

    def home_bank_id(self) -> str:
        if self._home is None:
            homes = [b.bank_id for b in self.banks().values() if b.is_home]
            if len(homes) != 1:
                raise ValueError(f"exactly one home bank required, got {homes}")
            self._home = homes[0]
        return self._home

    def complaint(self, ncrp_id: str) -> Complaint:
        if self._complaints is not None:
            return self._complaints[ncrp_id]
        rows = self._query("complaint", {"ncrp_id": ncrp_id})
        if not rows:
            raise KeyError(ncrp_id)
        return _model(Complaint, rows[0])

    def complaints(self, since: datetime | None = None) -> list[Complaint]:
        if self._complaints is not None:
            out = list(self._complaints.values())
            if since is not None:
                out = [c for c in out if ensure_utc(c.reported_at) >= ensure_utc(since)]
            return sorted(out, key=lambda c: (c.reported_at, c.ncrp_id))
        if since is None:
            rows = self._query("complaints_all")
        else:
            rows = self._query("complaints_since", {"since": since})
        return [_model(Complaint, r) for r in rows]

    def complaint_transactions(self, ncrp_id: str) -> list[Transaction]:
        if self._complaint_txns is not None:
            return list(self._complaint_txns.get(ncrp_id, []))
        return [_model(Transaction, r) for r in self._query("complaint_transactions", {"ncrp_id": ncrp_id})]

    def accounts(self, account_ids: Iterable[str]) -> dict[str, Account]:
        ids = _ids(account_ids)
        missing = [a for a in ids if a not in self._account_cache]
        if missing:
            for r in self._query_ids("accounts", missing):
                acct = _model(Account, r)
                self._account_cache[acct.account_id] = acct
        return {a: self._account_cache[a] for a in ids if a in self._account_cache}

    def customers(self, customer_ids: Iterable[str]) -> dict[str, Customer]:
        ids = _ids(customer_ids)
        if not ids:
            return {}
        return {r["customer_id"]: _model(Customer, r) for r in self._query_ids("customers", ids)}

    def balances_at(self, account_ids: Iterable[str], ts: datetime) -> dict[str, float]:
        ids = _ids(account_ids)
        if not ids:
            return {}
        rows = self._query_ids("balances_at", ids, ts=ts)
        return {r["account_id"]: round(float(r["balance"]), 2) for r in rows}

    def _windowed(self, name: str, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        ids = _ids(account_ids)
        if not ids:
            return []
        rows = self._query_ids(name, ids, start=start, end=end, home_bank=self.home_bank_id())
        seen: dict[str, Transaction] = {}
        for r in rows:
            seen.setdefault(r["txn_id"], _model(Transaction, r))
        return sorted(seen.values(), key=lambda t: (ensure_utc(t.ts), t.txn_id))

    def transactions_touching(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        return self._windowed("transactions_touching", account_ids, start, end)

    def consortium_transactions(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        return self._windowed("consortium_transactions", account_ids, start, end)

    def invoices_for_transactions(self, txn_ids: Iterable[str]) -> list[Invoice]:
        ids = _ids(txn_ids)
        if not ids:
            return []
        rows = self._query_ids("invoices", ids)
        return [_model(Invoice, r) for r in sorted(rows, key=lambda r: r["invoice_id"])]

    def external_signals(self, account_ids: Iterable[str]) -> dict[str, ExternalSignal]:
        ids = _ids(account_ids)
        if not ids:
            return {}
        return {r["account_id"]: _model(ExternalSignal, r) for r in self._query_ids("external_signals", ids)}

    def device_links(self, account_ids: Iterable[str]) -> list[DeviceLink]:
        ids = _ids(account_ids)
        if not ids:
            return []
        links = [_model(DeviceLink, r) for r in self._query_ids("device_links", ids)]
        return sorted(links, key=lambda x: (x.account_id, x.device_id, x.first_seen))

    def device_links_for_devices(self, device_ids: Iterable[str]) -> list[DeviceLink]:
        ids = _ids(device_ids)
        if not ids:
            return []
        links = [_model(DeviceLink, r) for r in self._query_ids("device_links_for_devices", ids)]
        return sorted(links, key=lambda x: (x.device_id, x.account_id, x.first_seen))

    def gst_records(self, gstins: Iterable[str]) -> dict[str, GstRecord]:
        ids = _ids(gstins)
        if not ids:
            return {}
        return {r["gstin"]: _model(GstRecord, r) for r in self._query_ids("gst_records", ids)}

    # ------------------------------------------------------------------ extras (not part of the protocol)
    @property
    def meta(self) -> LedgerMeta:
        """World metadata from ``<schema>.META`` (``world``, ``size``, ``seed``, ...), the same shape as
        ``ParquetLedger.meta``: a fresh dict copy that is indexable (``ledger.meta["world"]``) and callable
        (``ledger.meta()``). One query per ledger; later reads are served from the cache."""
        from wapsi.adapters.parquet_ledger import LedgerMeta  # noqa: F811 - runtime import of the shared type

        if self._meta is None:
            self._meta = {str(r["key"]): str(r["value"]) for r in self._query("meta")}
        return LedgerMeta(self._meta)

    def reachable_accounts(
        self,
        ncrp_ids: Sequence[str],
        window_end: datetime,
        *,
        max_depth: int = 6,
        min_amount: float = 0.0,
    ) -> dict[str, int]:
        """account_id -> minimum hop depth reachable from the complaints' transfers (optional pre-filter)."""
        if not 1 <= int(max_depth) <= 10:
            raise ValueError("max_depth must be between 1 and 10")
        rows = self._query(
            "reachable_accounts",
            {"ncrp_ids": _ids(ncrp_ids), "window_end": window_end, "max_depth": int(max_depth),
             "min_amount": float(min_amount)},
        )
        return {r["account_id"]: int(r["layer"]) for r in rows}


# --------------------------------------------------------------------------- snapshot mode (ParquetLedger)

_TS_COLUMNS = {
    "accounts": ["opened_at"],
    "transactions": ["ts"],
    "invoices": ["ordered_at", "delivered_at"],
    "device_links": ["first_seen"],
    "complaints": ["reported_at"],
}
_FLOAT_COLUMNS = {
    "accounts": ["opening_balance"],
    "transactions": ["amount"],
    "invoices": ["amount"],
    "complaints": ["amount_claimed"],
    "external_signals": ["mulehunter_score"],
}


def export_world(session: Any, out_dir: str | Path, schema: str = DEFAULT_SCHEMA, database: str = DATABASE) -> Path:
    """Write ``<database>.<schema>`` as ``<table>.parquet`` files in the ``wapsi.synth.export`` layout
    (lower-case columns, UTC-aware timestamps, float money). Labels are never exported."""
    import pandas as pd

    db = validate_identifier(database, "database")
    sc = validate_identifier(schema, "schema")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for table in WORLD_TABLES:
        df = session.sql(f"SELECT * FROM {db}.{sc}.{table.upper()}").to_pandas()
        df.columns = [str(c).lower() for c in df.columns]
        for col in _TS_COLUMNS.get(table, []):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True)
        for col in _FLOAT_COLUMNS.get(table, []):
            if col in df.columns:
                df[col] = df[col].astype("float64")
        if table == "external_signals" and "ncrp_prior_reports" in df.columns:
            df["ncrp_prior_reports"] = df["ncrp_prior_reports"].fillna(0).astype("int64")
        df.to_parquet(out / f"{table}.parquet", index=False)
    return out


def snapshot_ledger(session: Any, out_dir: str | Path, schema: str = DEFAULT_SCHEMA, database: str = DATABASE):
    """Pull the schema once and trace locally with ``ParquetLedger`` (the adapter the local golden run uses)."""
    from wapsi.adapters.parquet_ledger import ParquetLedger

    return ParquetLedger(export_world(session, out_dir, schema, database))
