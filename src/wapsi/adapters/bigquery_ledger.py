"""``BigQueryLedger``: ``wapsi.ports.Ledger`` over a generated world loaded into BigQuery (GCP edition).

Tables follow ``docs/data-contract.md`` one-to-one (``<project>.<dataset>.<table>``). Every query is a static SQL
string; user-supplied values (account ids, NCRP ids, timestamps) are always passed as BigQuery query parameters,
never formatted into the SQL text. Only the fully-qualified table names are substituted, and those are validated
identifiers from the constructor.

Visibility semantics match ``wapsi.adapters.memory.InMemoryLedger``:
- ``transactions_touching`` returns transactions where either side is a *home-bank account* (per the ``accounts``
  table, not the transaction's bank columns);
- ``consortium_transactions`` exposes transactions of known *other-bank* accounts (simulated CFCFRMS responses);
- ``balances_at`` = opening_balance + credits − debits strictly before ``ts`` (self-transfers net to zero).

``transactions`` is partitioned by ``DATE(ts)`` and clustered by ``from_account, to_account`` (see
:func:`load_world` and ``editions/gcp/deploy/bigquery_setup.sql``), so the ``ts`` window predicates prune
partitions and the account predicates prune blocks. ``maximum_bytes_billed`` caps each query's cost.

``python -m wapsi.adapters.bigquery_ledger load --world data/generated/demo --project P --dataset wapsi`` loads a
generated Parquet world (needs credentials; nothing here runs at import time).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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

_IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-.:]*$")
DEFAULT_MAX_BYTES_BILLED = 20 * 1024**3  # 20 GiB per query: a hard cost guardrail

# --------------------------------------------------------------------------- schema (docs/data-contract.md)

SCHEMAS: dict[str, list[tuple[str, str, str]]] = {
    # table -> [(column, BigQuery type, mode)]
    "banks": [("bank_id", "STRING", "REQUIRED"), ("name", "STRING", "NULLABLE"), ("is_home", "BOOL", "NULLABLE")],
    "customers": [
        ("customer_id", "STRING", "REQUIRED"), ("name", "STRING", "NULLABLE"), ("mobile", "STRING", "NULLABLE"),
        ("pan_masked", "STRING", "NULLABLE"), ("kyc_tier", "STRING", "NULLABLE"), ("dob", "DATE", "NULLABLE"),
        ("city", "STRING", "NULLABLE"),
    ],
    "accounts": [
        ("account_id", "STRING", "REQUIRED"), ("bank_id", "STRING", "REQUIRED"),
        ("customer_id", "STRING", "NULLABLE"), ("account_type", "STRING", "NULLABLE"),
        ("opened_at", "TIMESTAMP", "NULLABLE"), ("gstin", "STRING", "NULLABLE"), ("status", "STRING", "NULLABLE"),
        ("opening_balance", "FLOAT64", "NULLABLE"),
    ],
    "transactions": [
        ("txn_id", "STRING", "REQUIRED"), ("ts", "TIMESTAMP", "REQUIRED"), ("from_account", "STRING", "NULLABLE"),
        ("to_account", "STRING", "NULLABLE"), ("from_bank_id", "STRING", "NULLABLE"),
        ("to_bank_id", "STRING", "NULLABLE"), ("amount", "FLOAT64", "REQUIRED"), ("channel", "STRING", "NULLABLE"),
        ("utr", "STRING", "NULLABLE"), ("remark", "STRING", "NULLABLE"),
        ("counterparty_label", "STRING", "NULLABLE"),
    ],
    "invoices": [
        ("invoice_id", "STRING", "REQUIRED"), ("merchant_account", "STRING", "NULLABLE"),
        ("payer_account", "STRING", "NULLABLE"), ("txn_id", "STRING", "NULLABLE"), ("amount", "FLOAT64", "NULLABLE"),
        ("ordered_at", "TIMESTAMP", "NULLABLE"), ("delivered_at", "TIMESTAMP", "NULLABLE"),
        ("order_status", "STRING", "NULLABLE"),
    ],
    "gst_registry": [
        ("gstin", "STRING", "REQUIRED"), ("legal_name", "STRING", "NULLABLE"), ("status", "STRING", "NULLABLE"),
        ("registered_at", "DATE", "NULLABLE"),
    ],
    "external_signals": [
        ("account_id", "STRING", "REQUIRED"), ("fri_risk", "STRING", "NULLABLE"),
        ("mulehunter_score", "FLOAT64", "NULLABLE"), ("ncrp_prior_reports", "INT64", "NULLABLE"),
    ],
    "device_links": [
        ("device_id", "STRING", "REQUIRED"), ("account_id", "STRING", "REQUIRED"),
        ("first_seen", "TIMESTAMP", "NULLABLE"),
    ],
    "complaints": [
        ("ncrp_id", "STRING", "REQUIRED"), ("reported_at", "TIMESTAMP", "NULLABLE"),
        ("victim_account", "STRING", "NULLABLE"), ("victim_name", "STRING", "NULLABLE"),
        ("victim_mobile", "STRING", "NULLABLE"), ("amount_claimed", "FLOAT64", "NULLABLE"),
        ("narrative_text", "STRING", "NULLABLE"), ("language", "STRING", "NULLABLE"),
        ("category", "STRING", "NULLABLE"), ("fir_number", "STRING", "NULLABLE"), ("source", "STRING", "NULLABLE"),
    ],
    "complaint_txns": [("ncrp_id", "STRING", "REQUIRED"), ("txn_id", "STRING", "REQUIRED")],
    "meta": [("key", "STRING", "REQUIRED"), ("value", "STRING", "NULLABLE")],
    # ground truth: evals / classifier training only; never read by the product
    "labels_accounts": [
        ("account_id", "STRING", "REQUIRED"), ("role_truth", "STRING", "NULLABLE"),
        ("ring_id", "STRING", "NULLABLE"), ("typology", "STRING", "NULLABLE"),
    ],
    "labels_flows": [
        ("txn_id", "STRING", "REQUIRED"), ("ncrp_id", "STRING", "NULLABLE"),
        ("tainted_amount_truth", "FLOAT64", "NULLABLE"),
    ],
    "labels_complaints": [
        ("ncrp_id", "STRING", "REQUIRED"), ("case_id", "STRING", "NULLABLE"), ("ring_id", "STRING", "NULLABLE"),
        ("typology", "STRING", "NULLABLE"), ("is_adversarial", "BOOL", "NULLABLE"),
    ],
}
LABEL_TABLES = ("labels_accounts", "labels_flows", "labels_complaints")
PARTITION_FIELD: dict[str, str] = {"transactions": "ts"}  # DAY partitioning on a TIMESTAMP = DATE(ts)
CLUSTERING: dict[str, list[str]] = {
    "transactions": ["from_account", "to_account"],
    "accounts": ["bank_id", "account_id"],
    "device_links": ["account_id", "device_id"],
    "complaint_txns": ["ncrp_id"],
    "invoices": ["txn_id"],
}

# --------------------------------------------------------------------------- SQL (static; parameters only)
# ``{t}`` is replaced by the validated fully-qualified table name map; every value is an @parameter.

SQL: dict[str, str] = {
    "banks": "SELECT bank_id, name, is_home FROM `{banks}` ORDER BY bank_id",
    "complaint": "SELECT * FROM `{complaints}` WHERE ncrp_id = @ncrp_id LIMIT 1",
    "complaints_all": "SELECT * FROM `{complaints}` ORDER BY reported_at, ncrp_id",
    "complaints_since": "SELECT * FROM `{complaints}` WHERE reported_at >= @since ORDER BY reported_at, ncrp_id",
    "complaint_transactions": (
        "SELECT t.* FROM `{transactions}` AS t "
        "JOIN `{complaint_txns}` AS ct ON ct.txn_id = t.txn_id "
        "WHERE ct.ncrp_id = @ncrp_id ORDER BY t.ts, t.txn_id"
    ),
    "accounts": "SELECT * FROM `{accounts}` WHERE account_id IN UNNEST(@ids)",
    "customers": "SELECT * FROM `{customers}` WHERE customer_id IN UNNEST(@ids)",
    "balances_at": """
WITH acct AS (
  SELECT account_id, opening_balance FROM `{accounts}` WHERE account_id IN UNNEST(@ids)
),
moves AS (
  SELECT to_account AS account_id, amount AS delta
  FROM `{transactions}`
  WHERE ts < @ts AND to_account IN UNNEST(@ids)
    AND (from_account IS NULL OR from_account != to_account)
  UNION ALL
  SELECT from_account AS account_id, -amount AS delta
  FROM `{transactions}`
  WHERE ts < @ts AND from_account IN UNNEST(@ids)
    AND (to_account IS NULL OR from_account != to_account)
)
SELECT a.account_id AS account_id,
       ROUND(COALESCE(a.opening_balance, 0) + COALESCE(SUM(m.delta), 0), 2) AS balance
FROM acct AS a
LEFT JOIN moves AS m ON m.account_id = a.account_id
GROUP BY a.account_id, a.opening_balance
""",
    "transactions_touching": """
WITH home AS (
  SELECT account_id FROM `{accounts}` WHERE bank_id = @home_bank
)
SELECT t.*
FROM `{transactions}` AS t
WHERE t.ts >= @start AND t.ts <= @end
  AND (t.from_account IN UNNEST(@ids) OR t.to_account IN UNNEST(@ids))
  AND (t.from_account IN (SELECT account_id FROM home) OR t.to_account IN (SELECT account_id FROM home))
ORDER BY t.ts, t.txn_id
""",
    "consortium_transactions": """
WITH other AS (
  SELECT account_id FROM `{accounts}`
  WHERE account_id IN UNNEST(@ids) AND bank_id != @home_bank
)
SELECT t.*
FROM `{transactions}` AS t
WHERE t.ts >= @start AND t.ts <= @end
  AND (t.from_account IN (SELECT account_id FROM other) OR t.to_account IN (SELECT account_id FROM other))
ORDER BY t.ts, t.txn_id
""",
    "invoices": "SELECT * FROM `{invoices}` WHERE txn_id IN UNNEST(@ids) ORDER BY invoice_id",
    "external_signals": "SELECT * FROM `{external_signals}` WHERE account_id IN UNNEST(@ids)",
    "device_links": (
        "SELECT * FROM `{device_links}` WHERE account_id IN UNNEST(@ids) "
        "ORDER BY account_id, device_id, first_seen"
    ),
    "device_links_for_devices": (
        "SELECT * FROM `{device_links}` WHERE device_id IN UNNEST(@ids) "
        "ORDER BY device_id, account_id, first_seen"
    ),
    "gst_records": "SELECT * FROM `{gst_registry}` WHERE gstin IN UNNEST(@ids)",
    # Optional pre-filter: accounts reachable from the complaint transfers within the trace window.
    # Recursive CTE with a depth guard (@max_depth), a temporal guard (a hop never precedes the hop that
    # funded it), a minimum amount and a cycle guard (the path string). BigQuery stops a recursive CTE after
    # 500 iterations; @max_depth (TraceConfig.max_depth, default 6) keeps us far below that. Path counts can
    # grow quickly on dense graphs, so this is a candidate filter, not the taint computation.
    "reachable_accounts": """
WITH RECURSIVE reach AS (
  SELECT
    t.to_account AS account_id,
    1 AS depth,
    t.ts AS reached_at,
    CONCAT('|', COALESCE(t.from_account, ''), '|', t.to_account, '|') AS path
  FROM `{transactions}` AS t
  JOIN `{complaint_txns}` AS ct ON ct.txn_id = t.txn_id
  WHERE ct.ncrp_id IN UNNEST(@ncrp_ids)
    AND t.to_account IS NOT NULL
  UNION ALL
  SELECT
    t.to_account AS account_id,
    r.depth + 1 AS depth,
    t.ts AS reached_at,
    CONCAT(r.path, t.to_account, '|') AS path
  FROM reach AS r
  JOIN `{transactions}` AS t ON t.from_account = r.account_id
  WHERE r.depth < @max_depth
    AND t.to_account IS NOT NULL
    AND t.ts >= r.reached_at
    AND t.ts <= @window_end
    AND t.amount >= @min_amount
    AND STRPOS(r.path, CONCAT('|', t.to_account, '|')) = 0
)
SELECT account_id, MIN(depth) AS layer, MIN(reached_at) AS first_reached_at, COUNT(*) AS paths
FROM reach
GROUP BY account_id
ORDER BY layer, account_id
""",
}


@dataclass(frozen=True)
class Param:
    """A typed query parameter (converted to ``google.cloud.bigquery`` parameter objects at call time)."""

    name: str
    type_: str  # STRING | TIMESTAMP | INT64 | FLOAT64 | BOOL
    value: Any
    array: bool = False


def _bq_params(params: Sequence[Param]) -> list[Any]:
    from google.cloud import bigquery  # lazy: optional dependency

    out: list[Any] = []
    for p in params:
        if p.array:
            out.append(bigquery.ArrayQueryParameter(p.name, p.type_, list(p.value)))
        else:
            out.append(bigquery.ScalarQueryParameter(p.name, p.type_, p.value))
    return out


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "items"):
        return dict(row.items())
    raise TypeError(f"unsupported BigQuery row type {type(row).__name__}")


def _model(cls: type, row: Mapping[str, Any]) -> Any:
    fields = cls.model_fields
    data = {k: v for k, v in row.items() if k in fields}
    return cls.model_validate(data)


def _ids(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def validate_identifier(value: str, what: str) -> str:
    if not value or not _IDENT.match(value) or "`" in value:
        raise ValueError(f"invalid BigQuery {what}: {value!r}")
    return value


class BigQueryLedger:
    """Implements ``wapsi.ports.Ledger`` with parameterized BigQuery SQL.

    ``client`` is any object with ``query(sql, job_config=...)`` returning a job with ``result()`` (a
    ``google.cloud.bigquery.Client`` in production, a fake in tests). ``job_config_factory`` builds the job
    config from ``(params, maximum_bytes_billed, labels)``; the default uses ``bigquery.QueryJobConfig``.
    """

    def __init__(
        self,
        project: str,
        dataset: str,
        client: Any | None = None,
        *,
        location: str | None = None,
        maximum_bytes_billed: int | None = DEFAULT_MAX_BYTES_BILLED,
        labels: Mapping[str, str] | None = None,
        job_config_factory: Any | None = None,
    ) -> None:
        self.project = validate_identifier(project, "project")
        self.dataset = validate_identifier(dataset, "dataset")
        self.location = location
        self.maximum_bytes_billed = maximum_bytes_billed
        self.labels = dict(labels or {"app": "wapsi"})
        self._client = client
        self._job_config_factory = job_config_factory or self._default_job_config
        self._tables = {t: f"{self.project}.{self.dataset}.{t}" for t in SCHEMAS}
        self._banks: dict[str, Bank] | None = None
        self._home: str | None = None
        self._account_cache: dict[str, Account] = {}
        self.queries: list[tuple[str, list[Param]]] = []  # (query name, params): observability + tests

    # ------------------------------------------------------------------ plumbing
    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery  # lazy

            self._client = bigquery.Client(project=self.project, location=self.location)
        return self._client

    def table(self, name: str) -> str:
        return self._tables[name]

    def sql(self, name: str) -> str:
        return SQL[name].format(**self._tables)

    def _default_job_config(self, params: Sequence[Param]) -> Any:
        from google.cloud import bigquery  # lazy

        return bigquery.QueryJobConfig(
            query_parameters=_bq_params(params),
            maximum_bytes_billed=self.maximum_bytes_billed,
            labels=self.labels,
            use_query_cache=True,
        )

    def _query(self, name: str, params: Sequence[Param] = ()) -> list[dict[str, Any]]:
        params = list(params)
        self.queries.append((name, params))
        job = self.client.query(self.sql(name), job_config=self._job_config_factory(params))
        return [_row_dict(r) for r in job.result()]

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
        rows = self._query("complaint", [Param("ncrp_id", "STRING", ncrp_id)])
        if not rows:
            raise KeyError(ncrp_id)
        return _model(Complaint, rows[0])

    def complaints(self, since: datetime | None = None) -> list[Complaint]:
        if since is None:
            rows = self._query("complaints_all")
        else:
            rows = self._query("complaints_since", [Param("since", "TIMESTAMP", ensure_utc(since))])
        return [_model(Complaint, r) for r in rows]

    def complaint_transactions(self, ncrp_id: str) -> list[Transaction]:
        rows = self._query("complaint_transactions", [Param("ncrp_id", "STRING", ncrp_id)])
        return [_model(Transaction, r) for r in rows]

    def accounts(self, account_ids: Iterable[str]) -> dict[str, Account]:
        ids = _ids(account_ids)
        missing = [a for a in ids if a not in self._account_cache]
        if missing:
            for r in self._query("accounts", [Param("ids", "STRING", missing, array=True)]):
                acct = _model(Account, r)
                self._account_cache[acct.account_id] = acct
        return {a: self._account_cache[a] for a in ids if a in self._account_cache}

    def customers(self, customer_ids: Iterable[str]) -> dict[str, Customer]:
        ids = _ids(customer_ids)
        if not ids:
            return {}
        rows = self._query("customers", [Param("ids", "STRING", ids, array=True)])
        return {r["customer_id"]: _model(Customer, r) for r in rows}

    def balances_at(self, account_ids: Iterable[str], ts: datetime) -> dict[str, float]:
        ids = _ids(account_ids)
        if not ids:
            return {}
        rows = self._query(
            "balances_at", [Param("ids", "STRING", ids, array=True), Param("ts", "TIMESTAMP", ensure_utc(ts))]
        )
        return {r["account_id"]: round(float(r["balance"]), 2) for r in rows}

    def transactions_touching(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        ids = _ids(account_ids)
        if not ids:
            return []
        rows = self._query(
            "transactions_touching",
            [
                Param("ids", "STRING", ids, array=True),
                Param("start", "TIMESTAMP", ensure_utc(start)),
                Param("end", "TIMESTAMP", ensure_utc(end)),
                Param("home_bank", "STRING", self.home_bank_id()),
            ],
        )
        return [_model(Transaction, r) for r in rows]

    def consortium_transactions(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        ids = _ids(account_ids)
        if not ids:
            return []
        rows = self._query(
            "consortium_transactions",
            [
                Param("ids", "STRING", ids, array=True),
                Param("start", "TIMESTAMP", ensure_utc(start)),
                Param("end", "TIMESTAMP", ensure_utc(end)),
                Param("home_bank", "STRING", self.home_bank_id()),
            ],
        )
        return [_model(Transaction, r) for r in rows]

    def invoices_for_transactions(self, txn_ids: Iterable[str]) -> list[Invoice]:
        ids = _ids(txn_ids)
        if not ids:
            return []
        return [_model(Invoice, r) for r in self._query("invoices", [Param("ids", "STRING", ids, array=True)])]

    def external_signals(self, account_ids: Iterable[str]) -> dict[str, ExternalSignal]:
        ids = _ids(account_ids)
        if not ids:
            return {}
        rows = self._query("external_signals", [Param("ids", "STRING", ids, array=True)])
        return {r["account_id"]: _model(ExternalSignal, r) for r in rows}

    def device_links(self, account_ids: Iterable[str]) -> list[DeviceLink]:
        ids = _ids(account_ids)
        if not ids:
            return []
        rows = self._query("device_links", [Param("ids", "STRING", ids, array=True)])
        return [_model(DeviceLink, r) for r in rows]

    def device_links_for_devices(self, device_ids: Iterable[str]) -> list[DeviceLink]:
        ids = _ids(device_ids)
        if not ids:
            return []
        rows = self._query("device_links_for_devices", [Param("ids", "STRING", ids, array=True)])
        return [_model(DeviceLink, r) for r in rows]

    def gst_records(self, gstins: Iterable[str]) -> dict[str, GstRecord]:
        ids = _ids(gstins)
        if not ids:
            return {}
        rows = self._query("gst_records", [Param("ids", "STRING", ids, array=True)])
        return {r["gstin"]: _model(GstRecord, r) for r in rows}

    # ------------------------------------------------------------------ extras (not part of the protocol)
    def reachable_accounts(
        self,
        ncrp_ids: Sequence[str],
        window_end: datetime,
        *,
        max_depth: int = 6,
        min_amount: float = 0.0,
    ) -> dict[str, int]:
        """account_id -> minimum hop depth reachable from the complaints' transfers (optional pre-filter)."""
        if not 1 <= int(max_depth) <= 50:
            raise ValueError("max_depth must be between 1 and 50")
        rows = self._query(
            "reachable_accounts",
            [
                Param("ncrp_ids", "STRING", _ids(ncrp_ids), array=True),
                Param("window_end", "TIMESTAMP", ensure_utc(window_end)),
                Param("max_depth", "INT64", int(max_depth)),
                Param("min_amount", "FLOAT64", float(min_amount)),
            ],
        )
        return {r["account_id"]: int(r["layer"]) for r in rows}


# --------------------------------------------------------------------------- loading a generated world


def bq_schema(table: str) -> list[Any]:
    from google.cloud import bigquery  # lazy

    return [bigquery.SchemaField(name, type_, mode=mode) for name, type_, mode in SCHEMAS[table]]


def load_job_config(table: str) -> Any:
    from google.cloud import bigquery  # lazy

    cfg = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=bq_schema(table),
    )
    if table in PARTITION_FIELD:
        cfg.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=PARTITION_FIELD[table]
        )
    if table in CLUSTERING:
        cfg.clustering_fields = CLUSTERING[table]
    return cfg


def load_world(
    path: str | Path,
    project: str,
    dataset: str,
    *,
    client: Any | None = None,
    location: str = "asia-south1",
    include_labels: bool = False,
    job_config_factory: Any | None = None,
    readers: Sequence[str] = (),
) -> dict[str, int]:
    """Load ``<path>/<table>.parquet`` files into ``project.dataset`` (creating the dataset if needed).

    Returns ``{table: rows loaded}``. Label tables are skipped unless ``include_labels`` (evals only).
    ``readers`` (``serviceAccount:<email>`` or ``<email>``) get dataset-level READER access (least privilege: the
    runtime service accounts read this dataset only; their jobs run under ``roles/bigquery.jobUser``).
    """
    project = validate_identifier(project, "project")
    dataset = validate_identifier(dataset, "dataset")
    root = Path(path)
    if not (root / "transactions.parquet").exists():
        raise FileNotFoundError(f"no world at {root} (transactions.parquet missing)")
    if client is None:
        from google.cloud import bigquery  # lazy

        client = bigquery.Client(project=project, location=location)
    factory = job_config_factory or load_job_config
    dataset_id = f"{project}.{dataset}"
    if hasattr(client, "create_dataset"):
        try:
            from google.cloud import bigquery  # lazy

            ds = bigquery.Dataset(dataset_id)
            ds.location = location
        except ImportError:  # pragma: no cover - fakes without the library
            ds = dataset_id
        client.create_dataset(ds, exists_ok=True)
    if readers:
        grant_dataset_readers(client, dataset_id, readers)
    loaded: dict[str, int] = {}
    for table in SCHEMAS:
        if table in LABEL_TABLES and not include_labels:
            continue
        f = root / f"{table}.parquet"
        if not f.exists():
            continue
        with f.open("rb") as fh:
            job = client.load_table_from_file(fh, f"{dataset_id}.{table}", job_config=factory(table))
        result = job.result()
        loaded[table] = int(getattr(result, "output_rows", None) or getattr(job, "output_rows", 0) or 0)
    return loaded


def grant_dataset_readers(client: Any, dataset_id: str, members: Sequence[str]) -> list[str]:
    """Add dataset-level READER entries for ``members`` (idempotent); returns the emails added."""
    from google.cloud import bigquery  # lazy

    emails = []
    for m in members:
        email = m.split(":", 1)[1] if ":" in m else m
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
            raise ValueError(f"not a principal email: {m!r}")
        emails.append(email.lower())
    ds = client.get_dataset(dataset_id)
    entries = list(ds.access_entries)
    have = {(e.role, (e.entity_id or "").lower()) for e in entries}
    added = [e for e in emails if ("READER", e) not in have]
    if added:
        entries += [bigquery.AccessEntry(role="READER", entity_type="userByEmail", entity_id=e) for e in added]
        ds.access_entries = entries
        client.update_dataset(ds, ["access_entries"])
    return added


def setup_sql(project: str, dataset: str, location: str = "asia-south1") -> str:
    """DDL equivalent of :func:`load_world`'s table layout (kept in sync with deploy/bigquery_setup.sql)."""
    project = validate_identifier(project, "project")
    dataset = validate_identifier(dataset, "dataset")
    parts = [f"CREATE SCHEMA IF NOT EXISTS `{project}.{dataset}` OPTIONS (location = '{location}');"]
    for table, cols in SCHEMAS.items():
        body = ",\n  ".join(f"{c} {t}{' NOT NULL' if m == 'REQUIRED' else ''}" for c, t, m in cols)
        ddl = f"CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table}` (\n  {body}\n)"
        if table in PARTITION_FIELD:
            ddl += f"\nPARTITION BY DATE({PARTITION_FIELD[table]})"
        if table in CLUSTERING:
            ddl += f"\nCLUSTER BY {', '.join(CLUSTERING[table])}"
        parts.append(ddl + ";")
    return "\n\n".join(parts) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wapsi.adapters.bigquery_ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load", help="load a generated Parquet world into BigQuery")
    ld.add_argument("--world", required=True)
    ld.add_argument("--project", required=True)
    ld.add_argument("--dataset", default="wapsi")
    ld.add_argument("--location", default="asia-south1")
    ld.add_argument("--include-labels", action="store_true")
    ld.add_argument("--reader", action="append", default=[],
                    help="principal granted dataset-level READER (serviceAccount:<email>); repeatable")
    ddl = sub.add_parser("ddl", help="print the CREATE TABLE statements")
    ddl.add_argument("--project", required=True)
    ddl.add_argument("--dataset", default="wapsi")
    ddl.add_argument("--location", default="asia-south1")
    args = ap.parse_args(argv)
    if args.cmd == "ddl":
        print(setup_sql(args.project, args.dataset, args.location), end="")
        return 0
    counts = load_world(args.world, args.project, args.dataset, location=args.location,
                        include_labels=args.include_labels, readers=args.reader)
    for table, n in counts.items():
        print(f"{table}: {n} rows")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
