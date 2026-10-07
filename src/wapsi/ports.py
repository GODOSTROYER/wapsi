"""Provider-agnostic interfaces. Editions supply adapters (BigQuery, Firestore, Gemini, vLLM, ...)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from wapsi.models import (
    Account,
    Action,
    AuditEvent,
    Bank,
    Case,
    CaseBundle,
    Citation,
    Complaint,
    Customer,
    DeviceLink,
    ExternalSignal,
    GstRecord,
    GuardResult,
    Invoice,
    SearchHit,
    Transaction,
)


@runtime_checkable
class Ledger(Protocol):
    """Read-only view of the home bank's data plus simulated CFCFRMS consortium responses."""

    def home_bank_id(self) -> str: ...

    def banks(self) -> dict[str, Bank]: ...

    def complaint(self, ncrp_id: str) -> Complaint: ...

    def complaints(self, since: datetime | None = None) -> list[Complaint]: ...

    def complaint_transactions(self, ncrp_id: str) -> list[Transaction]:
        """The victim transfers reported in the complaint (victim -> first-layer accounts)."""
        ...

    def accounts(self, account_ids: Iterable[str]) -> dict[str, Account]: ...

    def customers(self, customer_ids: Iterable[str]) -> dict[str, Customer]: ...

    def balances_at(self, account_ids: Iterable[str], ts: datetime) -> dict[str, float]:
        """opening_balance + credits - debits for transactions strictly before ts (any bank; the consortium
        simulation answers for other-bank accounts)."""
        ...

    def transactions_touching(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        """All transactions visible to the home bank where from_account or to_account is in account_ids,
        with start <= ts <= end, sorted by (ts, txn_id)."""
        ...

    def consortium_transactions(self, account_ids: Iterable[str], start: datetime, end: datetime) -> list[Transaction]:
        """Simulated CFCFRMS response: transactions of *other-bank* accounts (from or to), as if the other bank
        answered a hold/trail request. Sorted by (ts, txn_id). Empty for home-bank accounts."""
        ...

    def invoices_for_transactions(self, txn_ids: Iterable[str]) -> list[Invoice]: ...

    def external_signals(self, account_ids: Iterable[str]) -> dict[str, ExternalSignal]: ...

    def device_links(self, account_ids: Iterable[str]) -> list[DeviceLink]: ...

    def device_links_for_devices(self, device_ids: Iterable[str]) -> list[DeviceLink]: ...

    def gst_records(self, gstins: Iterable[str]) -> dict[str, GstRecord]: ...


@runtime_checkable
class DocSearch(Protocol):
    def search(self, query: str, k: int = 5) -> list[SearchHit]: ...

    def citation(self, citation_id: str) -> Citation | None: ...


@runtime_checkable
class LLM(Protocol):
    name: str

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        """messages: [{"role": "system"|"user"|"assistant", "content": str}]. Returns text (JSON text if a
        json_schema is given)."""
        ...


@runtime_checkable
class Guard(Protocol):
    def screen(self, text: str) -> GuardResult: ...


@runtime_checkable
class CaseStore(Protocol):
    def put_bundle(self, bundle: CaseBundle) -> None: ...

    def get_bundle(self, case_id: str) -> CaseBundle | None: ...

    def list_cases(self) -> list[Case]: ...

    def append_audit(self, case_id: str, event: AuditEvent) -> None: ...


@runtime_checkable
class ActionSink(Protocol):
    def submit(self, action: Action) -> Action: ...
