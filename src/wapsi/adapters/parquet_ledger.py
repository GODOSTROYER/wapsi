"""``ParquetLedger``: ``wapsi.ports.Ledger`` over a generated world directory (``data/generated/<world>``).

Built for the ~2M-transaction world: transactions are sorted once by (ts, txn_id) and kept as an Arrow table;
per-account row indices live in a CSR structure; balances use per-account cumulative sums (computed lazily, in
the same order and association as ``InMemoryLedger``) plus ``searchsorted``. Pydantic models are only built for
the rows a call returns. Visibility semantics are identical to ``wapsi.adapters.memory.InMemoryLedger``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

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

LABEL_TABLES = ("labels_accounts", "labels_flows", "labels_complaints")
_US = pa.timestamp("us", tz="UTC")


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _us(ts: datetime) -> int:
    d = ensure_utc(ts) - _EPOCH
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def _read(path: Path, table: str) -> pa.Table | None:
    p = path / f"{table}.parquet"
    return pq.read_table(p) if p.exists() else None


def _ts_col(t: pa.Table, col: str) -> pa.Table:
    i = t.schema.get_field_index(col)
    return t.set_column(i, col, pc.cast(t[col], _US)) if t.schema.field(col).type != _US else t


def _index(values: pa.ChunkedArray | pa.Array) -> dict[str, int]:
    return {v: i for i, v in enumerate(values.to_pylist())}


class LedgerMeta(dict[str, str]):
    """``ParquetLedger.meta``: a plain ``dict`` that can also be called, so the older ``ledger.meta()`` call keeps
    working and returns a copy."""

    def __call__(self) -> dict[str, str]:
        return dict(self)


class ParquetLedger:
    """Read-only ledger over the Parquet layout written by ``wapsi.synth.export``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not (self.path / "transactions.parquet").exists():
            raise FileNotFoundError(f"no world at {self.path} (transactions.parquet missing)")
        banks = _read(self.path, "banks")
        self._banks = {r["bank_id"]: Bank.model_validate(r) for r in banks.to_pylist()}
        homes = [b.bank_id for b in self._banks.values() if b.is_home]
        if len(homes) != 1:
            raise ValueError(f"exactly one home bank required, got {homes}")
        self._home = homes[0]
        meta = _read(self.path, "meta")
        self._meta = (
            {} if meta is None else dict(zip(meta["key"].to_pylist(), meta["value"].to_pylist(), strict=True))
        )

        # accounts (+ ids referenced by transactions but missing from the accounts table)
        self._acc = _ts_col(_read(self.path, "accounts"), "opened_at")
        known = self._acc["account_id"].to_pylist()
        tx = _ts_col(_read(self.path, "transactions"), "ts")
        order = pc.sort_indices(tx, sort_keys=[("ts", "ascending"), ("txn_id", "ascending")])
        if not np.array_equal(order.to_numpy(), np.arange(tx.num_rows)):
            tx = tx.take(order)
        self._tx = tx.combine_chunks()
        refs = pc.unique(
            pa.chunked_array([self._tx["from_account"], self._tx["to_account"]]).combine_chunks()
        )
        refs = pc.drop_null(refs)
        known_arr = pa.array(known, pa.string())
        extra = pc.filter(refs, pc.invert(pc.is_in(refs, value_set=known_arr)))
        extra = sorted(extra.to_pylist())
        self._ids = known + extra
        self._n_known = len(known)
        self._code = {a: i for i, a in enumerate(self._ids)}
        ids_arr = pa.array(self._ids, pa.string())
        bank = self._acc["bank_id"].to_pylist()
        self._is_home = np.array([b == self._home for b in bank] + [False] * len(extra), dtype=bool)
        self._opening = np.concatenate([self._acc["opening_balance"].to_numpy(), np.zeros(len(extra))])

        def codes(col: str) -> np.ndarray:
            c = pc.index_in(self._tx[col], value_set=ids_arr)
            return pc.fill_null(c, -1).to_numpy(zero_copy_only=False).astype(np.int64)

        self._frm = codes("from_account")
        self._to = codes("to_account")
        self._ts = pc.cast(self._tx["ts"], pa.int64()).to_numpy(zero_copy_only=False)
        self._amt = self._tx["amount"].to_numpy(zero_copy_only=False)
        rows = np.arange(self._tx.num_rows, dtype=np.int64)
        m_from = self._frm >= 0
        m_to = (self._to >= 0) & (self._to != self._frm)
        e_acct = np.concatenate([self._frm[m_from], self._to[m_to]])
        e_row = np.concatenate([rows[m_from], rows[m_to]])
        o = np.lexsort((e_row, e_acct))
        self._rows = e_row[o]
        self._row_ts = self._ts[self._rows]
        self._starts = np.searchsorted(e_acct[o], np.arange(len(self._ids) + 1))
        self._cum: dict[int, np.ndarray] = {}
        self._acct_cache: dict[str, Account] = {}

        # side tables
        self._cust = _read(self.path, "customers")
        self._cust_idx = _index(self._cust["customer_id"]) if self._cust is not None else {}
        inv = _read(self.path, "invoices")
        self._inv = None
        self._inv_by_txn: dict[str, list[int]] = {}
        if inv is not None and inv.num_rows:
            self._inv = _ts_col(_ts_col(inv, "ordered_at"), "delivered_at")
            for i, t in enumerate(self._inv["txn_id"].to_pylist()):
                if t is not None:
                    self._inv_by_txn.setdefault(t, []).append(i)
        self._sig = _read(self.path, "external_signals")
        self._sig_idx = _index(self._sig["account_id"]) if self._sig is not None else {}
        links = _read(self.path, "device_links")
        self._links: list[DeviceLink] = (
            []
            if links is None
            else [DeviceLink.model_validate(r) for r in _ts_col(links, "first_seen").to_pylist()]
        )
        self._links_by_account: dict[str, list[int]] = {}
        self._links_by_device: dict[str, list[int]] = {}
        for i, link in enumerate(self._links):
            self._links_by_account.setdefault(link.account_id, []).append(i)
            self._links_by_device.setdefault(link.device_id, []).append(i)
        gst = _read(self.path, "gst_registry")
        self._gst = {} if gst is None else {r["gstin"]: GstRecord.model_validate(r) for r in gst.to_pylist()}
        comp = _read(self.path, "complaints")
        self._complaints: dict[str, Complaint] = {}
        if comp is not None:
            for r in _ts_col(comp, "reported_at").to_pylist():
                c = Complaint.model_validate(r)
                self._complaints[c.ncrp_id] = c
        ct = _read(self.path, "complaint_txns")
        self._complaint_txns: dict[str, list[str]] = {}
        self._txn_row: dict[str, int] = {}
        if ct is not None and ct.num_rows:
            for n, t in zip(ct["ncrp_id"].to_pylist(), ct["txn_id"].to_pylist(), strict=True):
                self._complaint_txns.setdefault(n, []).append(t)
            mask = pc.is_in(self._tx["txn_id"], value_set=pa.array(sorted(set(ct["txn_id"].to_pylist()))))
            hit = np.flatnonzero(mask.to_numpy(zero_copy_only=False))
            for r, t in zip(hit.tolist(), self._tx["txn_id"].take(pa.array(hit)).to_pylist(), strict=True):
                self._txn_row[t] = r

    # ------------------------------------------------------------------ internals
    def _slice(self, code: int) -> slice:
        return slice(int(self._starts[code]), int(self._starts[code + 1]))

    def _cumulative(self, code: int) -> np.ndarray:
        c = self._cum.get(code)
        if c is None:
            rows = self._rows[self._slice(code)]
            signed = np.where(self._to[rows] == code, self._amt[rows], 0.0) - np.where(
                self._frm[rows] == code, self._amt[rows], 0.0
            )
            c = np.cumsum(np.concatenate([[self._opening[code]], signed]))
            self._cum[code] = c
        return c

    def _window_rows(self, code: int, lo_us: int, hi_us: int) -> np.ndarray:
        s = self._slice(code)
        ts = self._row_ts[s]
        a = int(np.searchsorted(ts, lo_us, "left"))
        b = int(np.searchsorted(ts, hi_us, "right"))
        return self._rows[s][a:b]

    def _txns(self, rows: np.ndarray) -> list[Transaction]:
        if rows.size == 0:
            return []
        return [Transaction.model_validate(r) for r in self._tx.take(pa.array(rows)).to_pylist()]

    def _account_model(self, account_id: str) -> Account | None:
        m = self._acct_cache.get(account_id)
        if m is None:
            code = self._code.get(account_id)
            if code is None or code >= self._n_known:
                return None
            m = Account.model_validate(self._acc.slice(code, 1).to_pylist()[0])
            self._acct_cache[account_id] = m
        return m

    # ------------------------------------------------------------------ Ledger protocol
    def home_bank_id(self) -> str:
        return self._home

    def banks(self) -> dict[str, Bank]:
        return dict(self._banks)

    def complaint(self, ncrp_id: str) -> Complaint:
        return self._complaints[ncrp_id]

    def complaints(self, since: datetime | None = None) -> list[Complaint]:
        out = list(self._complaints.values())
        if since is not None:
            s = ensure_utc(since)
            out = [c for c in out if ensure_utc(c.reported_at) >= s]
        return sorted(out, key=lambda c: (c.reported_at, c.ncrp_id))

    def complaint_transactions(self, ncrp_id: str) -> list[Transaction]:
        rows = sorted({self._txn_row[t] for t in self._complaint_txns.get(ncrp_id, []) if t in self._txn_row})
        return self._txns(np.array(rows, dtype=np.int64))

    def accounts(self, account_ids: Iterable[str]) -> dict[str, Account]:
        out: dict[str, Account] = {}
        for a in account_ids:
            m = self._account_model(a)
            if m is not None:
                out[a] = m
        return out

    def customers(self, customer_ids: Iterable[str]) -> dict[str, Customer]:
        out: dict[str, Customer] = {}
        for c in customer_ids:
            i = self._cust_idx.get(c)
            if i is not None:
                out[c] = Customer.model_validate(self._cust.slice(i, 1).to_pylist()[0])
        return out

    def balances_at(self, account_ids: Iterable[str], ts: datetime) -> dict[str, float]:
        t = _us(ts)
        out: dict[str, float] = {}
        for a in dict.fromkeys(account_ids):
            code = self._code.get(a)
            if code is None or code >= self._n_known:
                continue
            pos = int(np.searchsorted(self._row_ts[self._slice(code)], t, "left"))
            out[a] = round(float(self._cumulative(code)[pos]), 2)
        return out

    def transactions_touching(
        self, account_ids: Iterable[str], start: datetime, end: datetime
    ) -> list[Transaction]:
        lo, hi = _us(start), _us(end)
        parts = []
        for a in dict.fromkeys(account_ids):
            code = self._code.get(a)
            if code is None:
                continue
            rows = self._window_rows(code, lo, hi)
            f, t = self._frm[rows], self._to[rows]
            vis = ((f >= 0) & self._is_home[np.maximum(f, 0)]) | ((t >= 0) & self._is_home[np.maximum(t, 0)])
            parts.append(rows[vis])
        return self._txns(np.unique(np.concatenate(parts)) if parts else np.array([], dtype=np.int64))

    def consortium_transactions(
        self, account_ids: Iterable[str], start: datetime, end: datetime
    ) -> list[Transaction]:
        lo, hi = _us(start), _us(end)
        parts = []
        for a in dict.fromkeys(account_ids):
            code = self._code.get(a)
            if code is None or code >= self._n_known or self._is_home[code]:
                continue
            parts.append(self._window_rows(code, lo, hi))
        return self._txns(np.unique(np.concatenate(parts)) if parts else np.array([], dtype=np.int64))

    def invoices_for_transactions(self, txn_ids: Iterable[str]) -> list[Invoice]:
        if self._inv is None:
            return []
        rows = [i for t in txn_ids for i in self._inv_by_txn.get(t, [])]
        if not rows:
            return []
        return [Invoice.model_validate(r) for r in self._inv.take(pa.array(rows, pa.int64())).to_pylist()]

    def external_signals(self, account_ids: Iterable[str]) -> dict[str, ExternalSignal]:
        out: dict[str, ExternalSignal] = {}
        for a in account_ids:
            i = self._sig_idx.get(a)
            if i is not None:
                out[a] = ExternalSignal.model_validate(self._sig.slice(i, 1).to_pylist()[0])
        return out

    def device_links(self, account_ids: Iterable[str]) -> list[DeviceLink]:
        """Sorted by (account_id, device_id, first_seen) for determinism."""
        idx = [i for a in dict.fromkeys(account_ids) for i in self._links_by_account.get(a, [])]
        return sorted((self._links[i] for i in idx), key=lambda x: (x.account_id, x.device_id, x.first_seen))

    def device_links_for_devices(self, device_ids: Iterable[str]) -> list[DeviceLink]:
        """Sorted by (device_id, account_id, first_seen) for determinism."""
        idx = [i for d in dict.fromkeys(device_ids) for i in self._links_by_device.get(d, [])]
        return sorted((self._links[i] for i in idx), key=lambda x: (x.device_id, x.account_id, x.first_seen))

    def gst_records(self, gstins: Iterable[str]) -> dict[str, GstRecord]:
        return {g: self._gst[g] for g in gstins if g in self._gst}

    # ------------------------------------------------------------------ convenience (not part of the protocol)
    @property
    def meta(self) -> LedgerMeta:
        """World metadata from ``meta.parquet`` (``world``, ``size``, ``seed``, ``generator_version``, ...) as a fresh
        copy: ``ledger.meta["world"]``. ``ledger.meta()`` works too (a plain dict)."""
        return LedgerMeta(self._meta)

    def all_accounts(self) -> dict[str, Account]:
        return {r["account_id"]: Account.model_validate(r) for r in self._acc.to_pylist()}

    def all_transactions(self) -> list[Transaction]:
        """Every transaction as a model (slow for the 2M-row world; prefer ``transactions_frame``)."""
        return self._txns(np.arange(self._tx.num_rows, dtype=np.int64))

    def transactions_frame(self) -> pd.DataFrame:
        return self._tx.to_pandas()

    def account_ids(self) -> list[str]:
        return list(self._ids[: self._n_known])

    def labels(self) -> dict[str, pd.DataFrame]:
        """Ground-truth tables (evals and classifier training only; the product must never read these)."""
        out = {}
        for t in LABEL_TABLES:
            tbl = _read(self.path, t)
            if tbl is not None:
                out[t] = tbl.to_pandas()
        return out
