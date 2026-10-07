"""Reconcile a voice note's claims with the ledger. The recording is never trusted for money.

Each claim becomes a row: ``MATCHED`` (the ledger agrees), ``MISMATCH`` (a matching complaint exists but this
claim differs; the ledger value is shown and is the one used) or ``NOT_FOUND`` (nothing in the ledger to compare
with). Which complaint a voice note belongs to is decided by the ledger only:

- a claimed UTR that appears on a reported transfer identifies its complaint (strongest evidence);
- without a UTR match, a complaint matches only on at least three independent agreements, one of them an amount,
  the account's last four digits or the masked phone, and only if exactly one complaint scores best (an amount
  alone, or a tie, never identifies a complaint).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from wapsi.models import Complaint, Transaction
from wapsi.money import eq, fmt_inr, r2
from wapsi.ports import Ledger
from wapsi.timeutil import IST, ensure_utc, fmt_ist, to_ist

if TYPE_CHECKING:  # pragma: no cover
    from wapsi.intake.voice import ClaimedAmount, TxnRef

MATCHED, MISMATCH, NOT_FOUND = "MATCHED", "MISMATCH", "NOT_FOUND"
TIME_TOLERANCE = timedelta(minutes=45)
MIN_AGREEMENTS = 3
_MODE_CHANNELS = {
    "UPI": {"UPI"},
    "IMPS": {"IMPS"},
    "NEFT": {"NEFT"},
    "RTGS": {"RTGS"},
    "CARD": {"POS"},
    "WALLET": {"UPI", "INTERNAL"},
}
_BANK_STOP = {
    "bank",
    "ltd",
    "limited",
    "co",
    "operative",
    "cooperative",
    "co-operative",
    "synthetic",
    "the",
    "of",
    "india",
    "and",
}
_NO_MATCH = "no NCRP complaint in the ledger matches this voice note"


class ReconRow(BaseModel):
    key: str  # utr | amount | date_time | payment_mode | account_last4 | victim_name | phone | bank | fraud_type
    claim: str  # label shown to the officer
    claimed: str  # what the complainant said (masked where personal)
    ledger: str  # what the ledger holds ("—" when there is nothing to compare with)
    status: Literal["MATCHED", "MISMATCH", "NOT_FOUND"]
    ncrp_id: str | None = None
    note: str = ""
    money: bool = False  # an amount or a UTR: decided by the ledger, never by the recording


@dataclass
class _Index:
    complaints: dict[str, Complaint]
    transfers: dict[str, list[Transaction]]
    by_utr: dict[str, list[tuple[str, Transaction]]]
    bank_names: dict[str, str]  # account_id -> bank name


_CACHE: dict[int, tuple[Ledger, _Index]] = {}


def ledger_index(ledger: Ledger) -> _Index:
    """Complaints, their reported transfers, a UTR index and victims' bank names (cached per ledger object)."""
    hit = _CACHE.get(id(ledger))
    if hit is not None and hit[0] is ledger:
        return hit[1]
    complaints = {c.ncrp_id: c for c in ledger.complaints()}
    transfers = {n: list(ledger.complaint_transactions(n)) for n in complaints}
    by_utr: dict[str, list[tuple[str, Transaction]]] = {}
    for n, txns in transfers.items():
        for t in txns:
            by_utr.setdefault(_norm_ref(t.utr), []).append((n, t))
    try:
        banks = {b_id: b.name for b_id, b in ledger.banks().items()}
        accounts = ledger.accounts({c.victim_account for c in complaints.values()})
        bank_names = {a_id: banks.get(a.bank_id, a.bank_id) for a_id, a in accounts.items()}
    except Exception:  # noqa: BLE001 - bank names are a nicety
        bank_names = {}
    idx = _Index(complaints, transfers, by_utr, bank_names)
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[id(ledger)] = (ledger, idx)
    return idx


def _norm_ref(ref: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", ref or "").upper()


def _tokens(s: str, stop: Iterable[str] = ()) -> set[str]:
    drop = set(stop)
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").casefold()) if w not in drop}


def name_match(claimed: str, actual: str) -> bool:
    a, b = _tokens(claimed), _tokens(actual)
    return bool(a) and bool(b) and (a <= b or b <= a)


def bank_match(claimed: str, actual: str) -> bool:
    a, b = _tokens(claimed, _BANK_STOP), _tokens(actual, _BANK_STOP)
    return bool(a) and bool(b) and (a <= b or b <= a)


def parse_claimed_dt(raw: str) -> tuple[datetime | None, bool]:
    """(UTC datetime, has_time). Naive values are IST; a bare date means 'that day'."""
    s = (raw or "").strip()
    if not s:
        return None, False
    has_time = bool(re.search(r"\d{1,2}:\d{2}", s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return ensure_utc(dt), has_time


def _txn_text(t: Transaction) -> str:
    return f"{fmt_inr(t.amount)} · {t.channel.value} · {fmt_ist(t.ts)} · UTR {t.utr}"


def _candidates(
    idx: _Index, fields: dict[str, str], amounts: Sequence[ClaimedAmount], refs: Sequence[TxnRef]
) -> list[str]:
    by_utr = sorted({n for r in refs for n, _ in idx.by_utr.get(_norm_ref(r.ref), [])})
    if by_utr:
        return by_utr
    last4 = fields.get("account_last4", "")
    phone = fields.get("phone", "")
    name = fields.get("victim_name", "")
    when, _ = parse_claimed_dt(fields.get("date_time", ""))
    scores: dict[str, int] = {}
    for n, c in idx.complaints.items():
        txns = idx.transfers.get(n, [])
        hits: set[str] = set()
        if last4 and c.victim_account.endswith(last4):
            hits.add("last4")
        if any(eq(a.value, t.amount) for a in amounts for t in txns) or any(
            eq(a.value, c.amount_claimed) for a in amounts
        ):
            hits.add("amount")
        if phone and phone == c.victim_mobile:
            hits.add("phone")
        if name and name_match(name, c.victim_name):
            hits.add("name")
        if when is not None and any(to_ist(t.ts).date() == to_ist(when).date() for t in txns):
            hits.add("date")
        if hits & {"last4", "amount", "phone"} and len(hits) >= MIN_AGREEMENTS:
            scores[n] = len(hits)
    if not scores:
        return []
    best = max(scores.values())
    picks = [n for n, s in scores.items() if s == best]
    return picks if len(picks) == 1 else []


def reconcile(
    ledger: Ledger, *, fields: dict[str, str], amounts: Sequence[ClaimedAmount], refs: Sequence[TxnRef]
) -> tuple[list[ReconRow], list[str]]:
    """(rows, matched NCRP ids). ``fields`` holds display values (phone already masked, last-4 digits only)."""
    idx = ledger_index(ledger)
    matched = _candidates(idx, fields, amounts, refs)
    txns = [(n, t) for n in matched for t in idx.transfers.get(n, [])]
    complaints = [idx.complaints[n] for n in matched]
    first = matched[0] if len(matched) == 1 else None
    rows: list[ReconRow] = []

    for r in refs:
        hits = idx.by_utr.get(_norm_ref(r.ref), [])
        if hits:
            n, t = hits[0]
            rows.append(
                ReconRow(
                    key="utr",
                    claim=f"{r.kind.replace('_', ' ')}",
                    claimed=r.ref,
                    ledger=_txn_text(t),
                    status=MATCHED,
                    ncrp_id=n,
                    money=True,
                )
            )
        else:
            rows.append(
                ReconRow(
                    key="utr",
                    claim=f"{r.kind.replace('_', ' ')}",
                    claimed=r.ref,
                    ledger="—",
                    status=NOT_FOUND,
                    money=True,
                    note="No reported transfer carries this reference: confirm with the remitting bank "
                    "before acting on it.",
                )
            )

    labels = {"TRANSFER": "Amount paid", "TOTAL_LOSS": "Total lost", "OTHER": "Amount mentioned"}
    for a in amounts:
        claimed = (
            fmt_inr(a.value) if a.currency.upper() in ("INR", "") else f"{a.currency} {r2(a.value):,.2f}"
        )
        label = labels.get(a.kind, "Amount")
        if a.currency.upper() not in ("INR", ""):
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger="—",
                    status=NOT_FOUND,
                    money=True,
                    note="Only rupee amounts are reconciled.",
                )
            )
            continue
        if not matched:
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger="—",
                    status=NOT_FOUND,
                    money=True,
                    note=f"{_NO_MATCH}; an amount alone never identifies a complaint.",
                )
            )
            continue
        if a.kind == "TOTAL_LOSS":
            total = r2(sum(c.amount_claimed for c in complaints))
            ok = eq(a.value, total)
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger=f"{fmt_inr(total)} on the NCRP complaint",
                    status=MATCHED if ok else MISMATCH,
                    ncrp_id=first,
                    money=True,
                    note="" if ok else "The ledger figure is the one used.",
                )
            )
            continue
        hit = next(((n, t) for n, t in txns if eq(t.amount, a.value)), None)
        if hit is not None:
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger=_txn_text(hit[1]),
                    status=MATCHED,
                    ncrp_id=hit[0],
                    money=True,
                )
            )
        elif any(eq(a.value, c.amount_claimed) for c in complaints):
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger=f"{claimed} on the NCRP complaint",
                    status=MATCHED,
                    ncrp_id=first,
                    money=True,
                )
            )
        else:
            seen = ", ".join(dict.fromkeys(fmt_inr(t.amount) for _, t in txns)) or "—"
            rows.append(
                ReconRow(
                    key="amount",
                    claim=label,
                    claimed=claimed,
                    ledger=seen,
                    status=MISMATCH,
                    ncrp_id=first,
                    money=True,
                    note="No reported transfer has this amount; the ledger figures are the ones used.",
                )
            )

    raw_dt = fields.get("date_time", "")
    if raw_dt:
        when, has_time = parse_claimed_dt(raw_dt)
        shown = fmt_ist(when, with_time=has_time) if when is not None else raw_dt
        if when is None:
            rows.append(
                ReconRow(
                    key="date_time",
                    claim="Date / time",
                    claimed=raw_dt,
                    ledger="—",
                    status=NOT_FOUND,
                    note="The date could not be read.",
                )
            )
        elif not txns:
            rows.append(
                ReconRow(
                    key="date_time",
                    claim="Date / time",
                    claimed=shown,
                    ledger="—",
                    status=NOT_FOUND,
                    note=_NO_MATCH + ".",
                )
            )
        else:
            n, t = min(txns, key=lambda x: abs(ensure_utc(x[1].ts) - when))
            same_day = to_ist(t.ts).date() == to_ist(when).date()
            ok = abs(ensure_utc(t.ts) - when) <= TIME_TOLERANCE if has_time else same_day
            note = "" if ok else ("Same day, different time." if same_day else "Different day.")
            rows.append(
                ReconRow(
                    key="date_time",
                    claim="Date / time",
                    claimed=shown,
                    ledger=fmt_ist(t.ts),
                    status=MATCHED if ok else MISMATCH,
                    ncrp_id=n,
                    note=note,
                )
            )

    mode = fields.get("payment_mode", "")
    if mode and mode not in ("UNKNOWN", "OTHER"):
        channels = sorted({t.channel.value for _, t in txns})
        if not txns:
            rows.append(
                ReconRow(
                    key="payment_mode",
                    claim="Payment mode",
                    claimed=mode,
                    ledger="—",
                    status=NOT_FOUND,
                    note=_NO_MATCH + ".",
                )
            )
        else:
            ok = bool(_MODE_CHANNELS.get(mode, {mode}) & set(channels))
            rows.append(
                ReconRow(
                    key="payment_mode",
                    claim="Payment mode",
                    claimed=mode,
                    ledger=", ".join(channels),
                    status=MATCHED if ok else MISMATCH,
                    ncrp_id=first,
                )
            )

    def identity(key: str, label: str, claimed: str, actual: list[str], match, *, show=lambda v: v) -> None:
        if not claimed:
            return
        if not complaints:
            rows.append(
                ReconRow(
                    key=key, claim=label, claimed=claimed, ledger="—", status=NOT_FOUND, note=_NO_MATCH + "."
                )
            )
            return
        ok = any(match(claimed, v) for v in actual if v)
        rows.append(
            ReconRow(
                key=key,
                claim=label,
                claimed=claimed,
                ledger=", ".join(show(v) for v in actual if v) or "—",
                status=MATCHED if ok else MISMATCH,
                ncrp_id=first,
            )
        )

    identity(
        "account_last4",
        "Account (last 4)",
        fields.get("account_last4", ""),
        [c.victim_account for c in complaints],
        lambda a, b: b.endswith(a),
    )
    identity(
        "victim_name",
        "Victim name",
        fields.get("victim_name", ""),
        [c.victim_name for c in complaints],
        name_match,
    )
    identity(
        "phone",
        "Phone (masked)",
        fields.get("phone", ""),
        [c.victim_mobile for c in complaints],
        lambda a, b: a == b,
    )
    identity(
        "bank",
        "Bank",
        fields.get("bank", ""),
        [idx.bank_names.get(c.victim_account, "") for c in complaints],
        bank_match,
    )
    ftype = fields.get("fraud_type", "")
    if ftype and ftype not in ("UNKNOWN", "OTHER"):
        identity("fraud_type", "Fraud type", ftype, [c.category for c in complaints], lambda a, b: a == b)
    return rows, matched
