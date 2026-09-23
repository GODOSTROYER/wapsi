"""Parquet layout of a world (docs/data-contract.md): write, load, and convert to/from ``InMemoryLedger``."""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from wapsi.adapters.memory import InMemoryLedger
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

_S = pa.string()
_TS = pa.timestamp("us", tz="UTC")
_F = pa.float64()

SCHEMAS: dict[str, pa.Schema] = {
    "banks": pa.schema([("bank_id", _S), ("name", _S), ("is_home", pa.bool_())]),
    "customers": pa.schema(
        [
            ("customer_id", _S),
            ("name", _S),
            ("mobile", _S),
            ("pan_masked", _S),
            ("kyc_tier", _S),
            ("dob", pa.date32()),
            ("city", _S),
        ]
    ),
    "accounts": pa.schema(
        [
            ("account_id", _S),
            ("bank_id", _S),
            ("customer_id", _S),
            ("account_type", _S),
            ("opened_at", _TS),
            ("gstin", _S),
            ("status", _S),
            ("opening_balance", _F),
        ]
    ),
    "transactions": pa.schema(
        [
            ("txn_id", _S),
            ("ts", _TS),
            ("from_account", _S),
            ("to_account", _S),
            ("from_bank_id", _S),
            ("to_bank_id", _S),
            ("amount", _F),
            ("channel", _S),
            ("utr", _S),
            ("remark", _S),
            ("counterparty_label", _S),
        ]
    ),
    "invoices": pa.schema(
        [
            ("invoice_id", _S),
            ("merchant_account", _S),
            ("payer_account", _S),
            ("txn_id", _S),
            ("amount", _F),
            ("ordered_at", _TS),
            ("delivered_at", _TS),
            ("order_status", _S),
        ]
    ),
    "gst_registry": pa.schema(
        [("gstin", _S), ("legal_name", _S), ("status", _S), ("registered_at", pa.date32())]
    ),
    "external_signals": pa.schema(
        [("account_id", _S), ("fri_risk", _S), ("mulehunter_score", _F), ("ncrp_prior_reports", pa.int64())]
    ),
    "device_links": pa.schema([("device_id", _S), ("account_id", _S), ("first_seen", _TS)]),
    "complaints": pa.schema(
        [
            ("ncrp_id", _S),
            ("reported_at", _TS),
            ("victim_account", _S),
            ("victim_name", _S),
            ("victim_mobile", _S),
            ("amount_claimed", _F),
            ("narrative_text", _S),
            ("language", _S),
            ("category", _S),
            ("fir_number", _S),
            ("source", _S),
        ]
    ),
    "complaint_txns": pa.schema([("ncrp_id", _S), ("txn_id", _S)]),
    "meta": pa.schema([("key", _S), ("value", _S)]),
    "labels_accounts": pa.schema([("account_id", _S), ("role_truth", _S), ("ring_id", _S), ("typology", _S)]),
    "labels_flows": pa.schema([("txn_id", _S), ("ncrp_id", _S), ("tainted_amount_truth", _F)]),
    "labels_complaints": pa.schema(
        [("ncrp_id", _S), ("case_id", _S), ("ring_id", _S), ("typology", _S), ("is_adversarial", pa.bool_())]
    ),
}
PRODUCT_TABLES = [
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
]
LABEL_TABLES = ["labels_accounts", "labels_flows", "labels_complaints"]

MODELS = {
    "banks": Bank,
    "customers": Customer,
    "accounts": Account,
    "transactions": Transaction,
    "invoices": Invoice,
    "gst_registry": GstRecord,
    "external_signals": ExternalSignal,
    "device_links": DeviceLink,
    "complaints": Complaint,
}


def empty_frame(table: str) -> pd.DataFrame:
    return SCHEMAS[table].empty_table().to_pandas()


def to_arrow(table: str, df: pd.DataFrame) -> pa.Table:
    schema = SCHEMAS[table]
    df = df[[f.name for f in schema]]
    return pa.Table.from_pandas(df, schema=schema, preserve_index=False).replace_schema_metadata(None)


def write_frames(frames: dict[str, pd.DataFrame], path: str | Path) -> dict[str, Path]:
    """One Parquet file per table (zstd, no pandas metadata, deterministic bytes)."""
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for table in [*PRODUCT_TABLES, *LABEL_TABLES]:
        if table not in frames:
            continue
        p = out / f"{table}.parquet"
        pq.write_table(to_arrow(table, frames[table]), p, compression="zstd", row_group_size=1 << 20)
        written[table] = p
    return written


def load_frames(path: str | Path, tables: Iterable[str] | None = None) -> dict[str, pd.DataFrame]:
    base = Path(path)
    names = list(tables) if tables is not None else [*PRODUCT_TABLES, *LABEL_TABLES]
    out = {}
    for t in names:
        p = base / f"{t}.parquet"
        if p.exists():
            out[t] = pq.read_table(p).to_pandas()
    return out


def records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Rows as dicts with NaN/NaT turned into None and pandas timestamps into datetimes."""
    rows = df.to_dict("records")
    for row in rows:
        for k, v in row.items():
            if v is None:
                continue
            if isinstance(v, float) and math.isnan(v):
                row[k] = None
            elif v is pd.NaT:
                row[k] = None
            elif isinstance(v, pd.Timestamp):
                row[k] = v.to_pydatetime()
    return rows


def _frame(table: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return empty_frame(table)
    return pa.Table.from_pylist(rows, schema=SCHEMAS[table]).to_pandas()


def ledger_to_frames(ledger: InMemoryLedger) -> dict[str, pd.DataFrame]:
    """Product tables of an ``InMemoryLedger`` (reads its internal stores so nothing is dropped)."""
    dump = lambda models: [m.model_dump(mode="python") for m in models]  # noqa: E731
    frames = {
        "banks": _frame("banks", dump(ledger.banks().values())),
        "customers": _frame("customers", dump(ledger._customers.values())),
        "accounts": _frame("accounts", dump(ledger.all_accounts().values())),
        "transactions": _frame("transactions", dump(ledger.all_transactions())),
        "invoices": _frame(
            "invoices", dump(inv for invs in ledger._invoices_by_txn.values() for inv in invs)
        ),
        "gst_registry": _frame("gst_registry", dump(ledger._gst.values())),
        "external_signals": _frame("external_signals", dump(ledger._signals.values())),
        "device_links": _frame(
            "device_links", dump(link for links in ledger._links_by_account.values() for link in links)
        ),
        "complaints": _frame("complaints", dump(ledger._complaints.values())),
        "complaint_txns": _frame(
            "complaint_txns",
            [{"ncrp_id": n, "txn_id": t} for n, ts in ledger._complaint_txns.items() for t in ts],
        ),
    }
    for t in ("accounts", "transactions"):
        for col in frames[t].columns:
            if frames[t][col].dtype == object and col in ("account_type", "status", "channel"):
                frames[t][col] = frames[t][col].astype(str)
    return frames


def frames_to_memory_ledger(frames: dict[str, pd.DataFrame]) -> InMemoryLedger:
    """Materialise a (small) world as an ``InMemoryLedger``. Not meant for the 2M-row world."""

    def models(table: str) -> list:
        df = frames.get(table)
        if df is None:
            return []
        return [MODELS[table].model_validate(r) for r in records(df)]

    ct = frames.get("complaint_txns")
    return InMemoryLedger(
        banks=models("banks"),
        accounts=models("accounts"),
        transactions=models("transactions"),
        complaints=models("complaints"),
        complaint_txns=[] if ct is None else list(zip(ct["ncrp_id"], ct["txn_id"], strict=True)),
        customers=models("customers"),
        invoices=models("invoices"),
        signals=models("external_signals"),
        device_links=models("device_links"),
        gst=models("gst_registry"),
    )


def meta_frame(values: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame({"key": list(values), "value": [str(v) for v in values.values()]})


def export_ledger(
    ledger: InMemoryLedger,
    path: str | Path,
    *,
    name: str = "world",
    labels: dict[str, pd.DataFrame] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Write an ``InMemoryLedger`` (e.g. the showcase world) in the standard Parquet layout."""
    frames = ledger_to_frames(ledger)
    txns = frames["transactions"]
    m = {
        "world": name,
        "home_bank_id": ledger.home_bank_id(),
        "n_accounts": len(frames["accounts"]),
        "n_transactions": len(txns),
        "n_complaints": len(frames["complaints"]),
    }
    if len(txns):
        m["world_start"] = txns["ts"].min().isoformat()
        m["world_end"] = txns["ts"].max().isoformat()
    m.update(meta or {})
    frames["meta"] = meta_frame(m)
    frames.update(labels or {})
    return write_frames(frames, path)
