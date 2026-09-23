"""Tiny hand-built worlds for classifier tests (imported by tests/classify/test_classify_*.py)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from wapsi.adapters.memory import InMemoryLedger
from wapsi.models import (
    Account,
    AccountType,
    Bank,
    Channel,
    Customer,
    DeviceLink,
    ExternalSignal,
    GstRecord,
    Invoice,
    Transaction,
)
from wapsi.timeutil import IST

HOME = "HB01"
OTHER = "OB02"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=IST)  # the "fraud" instant used by feature tests

GUILT_WORDS = re.compile(r"\b(guilty|criminal|fraudster|culprit|is a mule|convicted)\b", re.IGNORECASE)
INR_RE = re.compile(r"₹-?[0-9][0-9,]*(?:\.[0-9]{2})?")


def acct_id(n: int, bank: str = HOME) -> str:
    return f"{bank}-{n:010d}"


def account(
    n: int,
    *,
    bank: str = HOME,
    opened_days_before: float = 400.0,
    kind: AccountType = AccountType.INDIVIDUAL,
    gstin: str | None = None,
    balance: float = 10_000.0,
    customer: str | None = "auto",
) -> Account:
    return Account(
        account_id=acct_id(n, bank),
        bank_id=bank,
        customer_id=(f"CUST-{bank}-{n}" if customer == "auto" else customer),
        account_type=kind,
        opened_at=T0 - timedelta(days=opened_days_before),
        gstin=gstin,
        opening_balance=balance,
    )


_seq = iter(range(1, 10**9))


def txn(
    ts: datetime,
    frm: str | None,
    to: str | None,
    amount: float,
    channel: Channel = Channel.UPI,
    remark: str = "",
    txn_id: str | None = None,
) -> Transaction:
    k = next(_seq)
    return Transaction(
        txn_id=txn_id or f"TX-T{k:07d}",
        ts=ts,
        from_account=frm,
        to_account=to,
        from_bank_id=frm.split("-")[0] if frm else None,
        to_bank_id=to.split("-")[0] if to else None,
        amount=amount,
        channel=channel,
        utr=f"{k:012d}",
        remark=remark,
    )


def world(
    accounts: Iterable[Account],
    txns: Iterable[Transaction],
    *,
    links: Iterable[DeviceLink] = (),
    signals: Iterable[ExternalSignal] = (),
    customers: Iterable[Customer] = (),
    invoices: Iterable[Invoice] = (),
    gst: Iterable[GstRecord] = (),
) -> InMemoryLedger:
    return InMemoryLedger(
        banks=[Bank(bank_id=HOME, name="Home (synthetic)", is_home=True), Bank(bank_id=OTHER, name="Other")],
        accounts=list(accounts),
        transactions=list(txns),
        device_links=list(links),
        signals=list(signals),
        customers=list(customers),
        invoices=list(invoices),
        gst=list(gst),
    )


def link(device: str, acct: str, first_seen: datetime | None = None) -> DeviceLink:
    return DeviceLink(device_id=device, account_id=acct, first_seen=first_seen or T0 - timedelta(days=30))
