"""In-memory adapters for tests and small demos. Large worlds use ``wapsi.adapters.parquet_ledger``."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from wapsi.audit.chain import ChainAnchor, ChainReport, anchor_of, append_event, extend_stored, verify_chain
from wapsi.models import (
    Account,
    Action,
    ActionStatus,
    AuditEvent,
    Bank,
    Case,
    CaseBundle,
    Complaint,
    Customer,
    DeviceLink,
    ExternalSignal,
    GstRecord,
    Invoice,
    Transaction,
)
from wapsi.timeutil import ensure_utc, utcnow


class InMemoryLedger:
    """Implements ``wapsi.ports.Ledger`` over plain lists of models.

    Visibility: a transaction is visible to the home bank if either side is a home-bank account.
    ``consortium_transactions`` exposes other-bank accounts' transactions (simulated CFCFRMS responses).
    """

    def __init__(
        self,
        *,
        banks: Iterable[Bank],
        accounts: Iterable[Account],
        transactions: Iterable[Transaction],
        complaints: Iterable[Complaint] = (),
        complaint_txns: Iterable[tuple[str, str]] = (),
        customers: Iterable[Customer] = (),
        invoices: Iterable[Invoice] = (),
        signals: Iterable[ExternalSignal] = (),
        device_links: Iterable[DeviceLink] = (),
        gst: Iterable[GstRecord] = (),
    ) -> None:
        self._banks = {b.bank_id: b for b in banks}
        homes = [b.bank_id for b in self._banks.values() if b.is_home]
        if len(homes) != 1:
            raise ValueError(f"exactly one home bank required, got {homes}")
        self._home = homes[0]
        self._accounts = {a.account_id: a for a in accounts}
        self._txns = sorted(
            (t.model_copy(update={"ts": ensure_utc(t.ts)}) for t in transactions), key=lambda t: (t.ts, t.txn_id)
        )
        self._txn_by_id = {t.txn_id: t for t in self._txns}
        self._by_account: dict[str, list[Transaction]] = defaultdict(list)
        for t in self._txns:
            if t.from_account:
                self._by_account[t.from_account].append(t)
            if t.to_account and t.to_account != t.from_account:
                self._by_account[t.to_account].append(t)
        self._ts_index = {a: [t.ts for t in ts] for a, ts in self._by_account.items()}
        self._complaints = {c.ncrp_id: c for c in complaints}
        self._complaint_txns: dict[str, list[str]] = defaultdict(list)
        for ncrp_id, txn_id in complaint_txns:
            self._complaint_txns[ncrp_id].append(txn_id)
        self._customers = {c.customer_id: c for c in customers}
        self._invoices_by_txn: dict[str, list[Invoice]] = defaultdict(list)
        for inv in invoices:
            if inv.txn_id:
                self._invoices_by_txn[inv.txn_id].append(inv)
        self._signals = {s.account_id: s for s in signals}
        self._links_by_account: dict[str, list[DeviceLink]] = defaultdict(list)
        self._links_by_device: dict[str, list[DeviceLink]] = defaultdict(list)
        for link in device_links:
            self._links_by_account[link.account_id].append(link)
            self._links_by_device[link.device_id].append(link)
        self._gst = {g.gstin: g for g in gst}

    # -- helpers
    def _is_home_account(self, account_id: str | None) -> bool:
        if account_id is None:
            return False
        acct = self._accounts.get(account_id)
        return acct is not None and acct.bank_id == self._home

    def _visible(self, t: Transaction) -> bool:
        return self._is_home_account(t.from_account) or self._is_home_account(t.to_account)

    def _window(self, account_id: str, start: datetime, end: datetime) -> list[Transaction]:
        txns = self._by_account.get(account_id, [])
        idx = self._ts_index.get(account_id, [])
        lo = bisect_left(idx, ensure_utc(start))
        hi = bisect_right(idx, ensure_utc(end))
        return txns[lo:hi]

    # -- Ledger protocol
    def home_bank_id(self) -> str:
        return self._home

    def banks(self) -> dict[str, Bank]:
        return dict(self._banks)

    def complaint(self, ncrp_id: str) -> Complaint:
        return self._complaints[ncrp_id]

    def complaints(self, since: datetime | None = None) -> list[Complaint]:
        out = list(self._complaints.values())
        if since is not None:
            out = [c for c in out if ensure_utc(c.reported_at) >= ensure_utc(since)]
        return sorted(out, key=lambda c: (c.reported_at, c.ncrp_id))

    def complaint_transactions(self, ncrp_id: str) -> list[Transaction]:
        return sorted(
            (self._txn_by_id[t] for t in self._complaint_txns.get(ncrp_id, []) if t in self._txn_by_id),
            key=lambda t: (t.ts, t.txn_id),
        )

    def accounts(self, account_ids: Iterable[str]) -> dict[str, Account]:
        return {a: self._accounts[a] for a in account_ids if a in self._accounts}

    def customers(self, customer_ids: Iterable[str]) -> dict[str, Customer]:
        return {c: self._customers[c] for c in customer_ids if c in self._customers}

    def balances_at(self, account_ids: Iterable[str], ts: datetime) -> dict[str, float]:
        out: dict[str, float] = {}
        ts = ensure_utc(ts)
        for a in set(account_ids):
            acct = self._accounts.get(a)
            if acct is None:
                continue
            bal = acct.opening_balance
            idx = self._ts_index.get(a, [])
            for t in self._by_account.get(a, [])[: bisect_left(idx, ts)]:
                if t.to_account == a:
                    bal += t.amount
                if t.from_account == a:
                    bal -= t.amount
            out[a] = round(bal, 2)
        return out

    def transactions_touching(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        seen: dict[str, Transaction] = {}
        for a in set(account_ids):
            for t in self._window(a, start, end):
                if self._visible(t):
                    seen[t.txn_id] = t
        return sorted(seen.values(), key=lambda t: (t.ts, t.txn_id))

    def consortium_transactions(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        seen: dict[str, Transaction] = {}
        for a in set(account_ids):
            if self._is_home_account(a) or a not in self._accounts:
                continue
            for t in self._window(a, start, end):
                seen[t.txn_id] = t
        return sorted(seen.values(), key=lambda t: (t.ts, t.txn_id))

    def invoices_for_transactions(self, txn_ids: Iterable[str]) -> list[Invoice]:
        return [inv for t in txn_ids for inv in self._invoices_by_txn.get(t, [])]

    def external_signals(self, account_ids: Iterable[str]) -> dict[str, ExternalSignal]:
        return {a: self._signals[a] for a in account_ids if a in self._signals}

    def device_links(self, account_ids: Iterable[str]) -> list[DeviceLink]:
        return [link for a in set(account_ids) for link in self._links_by_account.get(a, [])]

    def device_links_for_devices(self, device_ids: Iterable[str]) -> list[DeviceLink]:
        return [link for d in set(device_ids) for link in self._links_by_device.get(d, [])]

    def gst_records(self, gstins: Iterable[str]) -> dict[str, GstRecord]:
        return {g: self._gst[g] for g in gstins if g in self._gst}

    # -- convenience (not part of the protocol)
    def all_accounts(self) -> dict[str, Account]:
        return dict(self._accounts)

    def all_transactions(self) -> list[Transaction]:
        return list(self._txns)


class InMemoryCaseStore:
    """Implements ``wapsi.ports.CaseStore``. Each case's audit log is append-only and hash-chained
    (``wapsi.audit``): ``put_bundle`` never drops, changes or reorders a recorded event, it only seals the
    bundle's new events onto the chain (``wapsi.audit.chain.extend_stored``); ``append_audit`` seals the event as
    the next link. The store also keeps an anchor per case (event count + head hash), outside the log, so
    ``verify_audit`` catches events removed from the end and a log whose hashes were all recomputed."""

    def __init__(self) -> None:
        self._bundles: dict[str, CaseBundle] = {}
        self._anchors: dict[str, ChainAnchor] = {}

    def put_bundle(self, bundle: CaseBundle) -> None:
        case_id = bundle.case.case_id
        stored = self._bundles.get(case_id)
        copy = bundle.model_copy(deep=True)
        copy.audit = extend_stored(stored.audit if stored else [], copy.audit, case_id=case_id)
        self._bundles[case_id] = copy
        self._anchors[case_id] = anchor_of(copy.audit)

    def get_bundle(self, case_id: str) -> CaseBundle | None:
        b = self._bundles.get(case_id)
        return b.model_copy(deep=True) if b else None

    def list_cases(self) -> list[Case]:
        return sorted((b.case for b in self._bundles.values()), key=lambda c: c.opened_at, reverse=True)

    def append_audit(self, case_id: str, event: AuditEvent) -> None:
        log = self._bundles[case_id].audit
        append_event(log, event, case_id=case_id)  # stamps the caller's event (its receipt)
        log[-1] = event.model_copy(deep=True)  # the store keeps its own copy
        self._anchors[case_id] = anchor_of(log)

    # -- audit chain (not part of the protocol)
    def audit_anchor(self, case_id: str) -> ChainAnchor | None:
        return self._anchors.get(case_id)

    def verify_audit(self, case_id: str) -> ChainReport:
        """Verify the stored audit chain of ``case_id`` against the store's anchor."""
        b = self._bundles[case_id]
        return verify_chain(b.audit, case_id=case_id, anchor=self._anchors.get(case_id))


class RecordingActionSink:
    """Marks actions DONE and keeps them. Both editions use it; a Jira/Slack/ticketing sink can plug in via ActionSink."""

    def __init__(self) -> None:
        self.actions: list[Action] = []

    def submit(self, action: Action) -> Action:
        done = action.model_copy(update={"status": ActionStatus.DONE, "executed_at": utcnow(), "result": "recorded"})
        self.actions.append(done)
        return done
