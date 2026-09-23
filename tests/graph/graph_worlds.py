"""Tiny builder for hand-made InMemoryLedger worlds used by the graph tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from wapsi.adapters.memory import InMemoryLedger
from wapsi.models import (
    Account,
    AccountType,
    Bank,
    Channel,
    ClassificationResult,
    Complaint,
    ExternalSignal,
    Invoice,
    OrderStatus,
    Role,
    Transaction,
)
from wapsi.timeutil import IST

HOME = "HB01"
BASE = datetime(2026, 9, 10, 10, 0, tzinfo=IST)


def at(minute: float) -> datetime:
    return BASE + timedelta(minutes=minute)


def bank_of(account_id: str | None) -> str | None:
    return account_id.split("-")[0] if account_id else None


class World:
    def __init__(self) -> None:
        self.banks = [
            Bank(bank_id=HOME, name="Home", is_home=True),
            Bank(bank_id="OB02", name="Other 2"),
            Bank(bank_id="OB03", name="Other 3"),
        ]
        self.accounts: dict[str, Account] = {}
        self.txns: list[Transaction] = []
        self.complaints: list[Complaint] = []
        self.complaint_txns: list[tuple[str, str]] = []
        self.invoices: list[Invoice] = []
        self.signals: list[ExternalSignal] = []
        self._n = 0

    def acct(self, account_id: str, opening: float = 0.0, kind: AccountType = AccountType.INDIVIDUAL) -> str:
        self.accounts[account_id] = Account(
            account_id=account_id,
            bank_id=bank_of(account_id),
            account_type=kind,
            opened_at=datetime(2020, 1, 1, tzinfo=IST),
            opening_balance=opening,
        )
        return account_id

    def txn(
        self,
        minute: float,
        frm: str | None,
        to: str | None,
        amount: float,
        channel: Channel = Channel.IMPS,
        txn_id: str | None = None,
    ) -> str:
        self._n += 1
        tid = txn_id or f"T{self._n:05d}"
        self.txns.append(
            Transaction(
                txn_id=tid,
                ts=at(minute),
                from_account=frm,
                to_account=to,
                from_bank_id=bank_of(frm),
                to_bank_id=bank_of(to),
                amount=amount,
                channel=channel,
                utr=f"{self._n:012d}",
            )
        )
        return tid

    def seed(
        self,
        ncrp_id: str,
        minute: float,
        victim: str,
        to: str | None,
        amount: float,
        fir: str | None = None,
        channel: Channel = Channel.IMPS,
        txn_id: str | None = None,
    ) -> str:
        if victim not in self.accounts:
            self.acct(victim, opening=10_000_000.0)
        tid = self.txn(minute, victim, to, amount, channel, txn_id=txn_id)
        if not any(c.ncrp_id == ncrp_id for c in self.complaints):
            self.complaints.append(
                Complaint(
                    ncrp_id=ncrp_id,
                    reported_at=at(minute + 60),
                    victim_account=victim,
                    victim_name="Synthetic Victim",
                    victim_mobile="90XXXXXX00",
                    amount_claimed=amount,
                    narrative_text="synthetic",
                    fir_number=fir,
                )
            )
        self.complaint_txns.append((ncrp_id, tid))
        return tid

    def invoice(
        self,
        merchant: str,
        payer: str,
        txn_id: str,
        amount: float,
        delivered: bool,
        status: OrderStatus | None = None,
    ) -> None:
        status = status or (OrderStatus.DELIVERED if delivered else OrderStatus.UNDELIVERED)
        self.invoices.append(
            Invoice(
                invoice_id=f"INV-{txn_id}-{len(self.invoices)}",
                merchant_account=merchant,
                payer_account=payer,
                txn_id=txn_id,
                amount=amount,
                ordered_at=BASE,
                delivered_at=BASE if status == OrderStatus.DELIVERED else None,
                order_status=status,
            )
        )

    def signal(self, account_id: str, prior_reports: int) -> None:
        self.signals.append(ExternalSignal(account_id=account_id, ncrp_prior_reports=prior_reports))

    def ledger(self) -> InMemoryLedger:
        return InMemoryLedger(
            banks=self.banks,
            accounts=self.accounts.values(),
            transactions=self.txns,
            complaints=self.complaints,
            complaint_txns=self.complaint_txns,
            invoices=self.invoices,
            signals=self.signals,
        )


def roles(mapping: dict[str, Role]) -> dict[str, ClassificationResult]:
    return {a: ClassificationResult(account_id=a, role=r, score=0.5) for a, r in mapping.items()}


def parts_sum(totals) -> float:
    return (
        totals.holdable
        + totals.cashed_out
        + totals.outbound
        + totals.intermediary
        + totals.merchant_settled
        + totals.unlocated
    )
