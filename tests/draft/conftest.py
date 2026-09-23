"""Shared fixtures and an independent draft-contract self-check (docs/draft-contract.md).

The resolvers below are written from the contract, not imported from ``wapsi.draft``, so the tests catch
drafter bugs. ``wapsi.verify`` is deliberately not used here.
"""

from __future__ import annotations

import json
import re

import pytest

from wapsi import money
from wapsi.citations import KNOWN_IDS
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.fixtures.showcase import showcase_ledger
from wapsi.models import CaseBundle, Draft
from wapsi.timeutil import fmt_ist

ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}-\d{10}(?![0-9])")
UTR_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d{12}|ATM\d{9}|CSH\d{9})(?![A-Za-z0-9])")
DATE_RE = re.compile(r"(?<![0-9])\d{2}-\d{2}-\d{4}(?![0-9])")
CITE_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\]")
LEFTOVER_RE = re.compile(r"⟦|⟧|\{\{|\{%|\bUndefined\b")

CONTRACT_SIMPLE = {
    "account_id",
    "bank_id",
    "disposition",
    "hold_amount",
    "forward_hold_amount",
    "tainted_amount",
    "balance_now",
    "disputed_amount",
    "utrs",
    "inflow_dates",
    "ncrp_ids",
    "total_stolen",
    "total_cashed_out",
    "total_outbound",
    "total_hold",
    "total_forward_hold",
    "total_recoverable",
    "baseline_freeze_amount",
    "restoration_process",
    "str_ground",
    "holder_name",
}
CONTRACT_PREFIXED = {"victim_amount", "complaint_amount", "reported_at"}


def resolve_slot(key: str, draft: Draft, bundle: CaseBundle, ledger) -> str:
    """Contract resolvers plus the proposed extensions (``<acct>/<key>``, ``edge_*:``, ``victim_account:``)."""
    acct = draft.account_id
    head, sep, rest = key.partition("/")
    if sep and ACCOUNT_RE.fullmatch(head):
        acct, key = head, rest
    trace, plan = bundle.trace, bundle.plan
    fmt = money.fmt_inr
    base, _, arg = key.partition(":")
    complaints = {c.ncrp_id: c for c in bundle.complaints}
    edges = {e.txn_id: e for e in trace.edges}

    def dec():
        return next(d for d in plan.decisions if d.account_id == acct)

    def st():
        return trace.accounts[acct]

    def inflow():
        return sorted(
            (e for e in trace.edges if e.to_account == acct and e.tainted_total > 0),
            key=lambda e: (e.ts, e.txn_id),
        )

    if key == "account_id":
        assert acct
        return acct
    simple = {
        "bank_id": lambda: st().bank_id,
        "disposition": lambda: dec().disposition.value,
        "hold_amount": lambda: fmt(dec().hold_amount),
        "forward_hold_amount": lambda: fmt(dec().forward_hold_amount),
        "tainted_amount": lambda: fmt(dec().tainted_amount),
        "balance_now": lambda: fmt(dec().balance_now),
        "disputed_amount": lambda: fmt(st().tainted_received_total),
        "utrs": lambda: ", ".join(sorted(e.utr for e in inflow())),
        "inflow_dates": lambda: ", ".join(dict.fromkeys(fmt_ist(e.ts, with_time=False) for e in inflow())),
        "ncrp_ids": lambda: ", ".join(sorted(bundle.case.ncrp_ids)),
        "total_stolen": lambda: fmt(trace.totals.stolen),
        "total_cashed_out": lambda: fmt(trace.totals.cashed_out),
        "total_outbound": lambda: fmt(trace.totals.outbound),
        "total_hold": lambda: fmt(plan.total_hold),
        "total_forward_hold": lambda: fmt(plan.total_forward_hold),
        "total_recoverable": lambda: fmt(plan.total_recoverable),
        "baseline_freeze_amount": lambda: fmt(plan.baseline_freeze_amount),
        "restoration_process": lambda: plan.restoration_process.value,
        "str_ground": lambda: plan.str_ground.value if plan.str_ground else "",
        "holder_name": lambda: (
            ledger.customers([ledger.accounts([acct])[acct].customer_id]).popitem()[1].name
        ),
    }
    if key in simple:
        return simple[key]()
    if base == "victim_amount":
        return fmt(dec().per_victim[arg]) if acct else fmt(plan.per_victim_recoverable[arg])
    if base == "complaint_amount":
        return fmt(complaints[arg].amount_claimed)
    if base == "reported_at":
        return fmt_ist(complaints[arg].reported_at)
    if base == "victim_account":
        return complaints[arg].victim_account
    if base == "edge_amount":
        return fmt(edges[arg].amount)
    if base == "edge_tainted":
        return fmt(edges[arg].tainted_total)
    if base == "edge_ts":
        return fmt_ist(edges[arg].ts)
    if base == "edge_utr":
        return edges[arg].utr
    raise KeyError(f"unknown slot key {key!r}")


def is_contract_key(key: str) -> bool:
    base, sep, _ = key.partition(":")
    return key in CONTRACT_SIMPLE or (bool(sep) and base in CONTRACT_PREFIXED)


def contract_problems(draft: Draft, bundle: CaseBundle, ledger, *, strict: bool = False) -> list[str]:
    """Everything that would make the draft fail the contract (empty list = pass)."""
    problems: list[str] = []
    for key, value in draft.slots.items():
        if strict and not is_contract_key(key):
            problems.append(f"non-contract slot key {key}")
        try:
            expected = resolve_slot(key, draft, bundle, ledger)
        except (KeyError, StopIteration, AssertionError) as exc:
            problems.append(f"slot {key} unresolvable: {exc!r}")
            continue
        if value != expected:
            problems.append(f"slot {key}={value!r} != {expected!r}")

    text = f"{draft.title}\n{draft.body}"
    slot_values = list(draft.slots.values())
    slot_amounts = [a for v in slot_values for _, a in money.find_amounts(v)]
    for span, amount in money.find_amounts(text):
        if not any(money.eq(amount, a) for a in slot_amounts):
            problems.append(f"body amount {span!r} matches no slot")

    trace = bundle.trace
    known_accounts = set(trace.accounts) | {c.victim_account for c in bundle.complaints}
    if draft.account_id:
        known_accounts.add(draft.account_id)
    slot_accounts = {a for v in slot_values for a in ACCOUNT_RE.findall(v)}
    for token in set(ACCOUNT_RE.findall(text)):
        if token not in known_accounts:
            problems.append(f"account {token} not in case")
        if token not in slot_accounts:
            problems.append(f"account {token} not from a slot")
    edge_utrs = {e.utr for e in trace.edges}
    slot_utrs = {u for v in slot_values for u in UTR_RE.findall(v)}
    for token in set(UTR_RE.findall(text)):
        if token not in edge_utrs:
            problems.append(f"UTR {token} not on the trail")
        if token not in slot_utrs:
            problems.append(f"UTR {token} not from a slot")
    slot_dates = {d for v in slot_values for d in DATE_RE.findall(v)}
    for token in set(DATE_RE.findall(text)):
        if token not in slot_dates:
            problems.append(f"date {token} not from a slot")

    tokens = [t for t in CITE_RE.findall(text) if not ACCOUNT_RE.fullmatch(t)]
    for t in tokens:
        if t not in KNOWN_IDS:
            problems.append(f"unknown citation {t}")
    if set(tokens) != set(draft.citation_ids):
        problems.append(f"citation_ids {draft.citation_ids} != body tokens {sorted(set(tokens))}")
    for c in draft.citation_ids:
        if c not in KNOWN_IDS:
            problems.append(f"declared unknown citation {c}")

    for m in LEFTOVER_RE.finditer(text):
        problems.append(f"template leftover {m.group(0)!r}")

    blob = json.dumps(draft.structured, ensure_ascii=False, default=str)
    for token in set(ACCOUNT_RE.findall(blob)) - known_accounts:
        problems.append(f"structured account {token} not in case")
    for token in set(UTR_RE.findall(blob)) - edge_utrs:
        problems.append(f"structured UTR {token} not on the trail")
    return problems


@pytest.fixture
def ledger():
    return showcase_ledger()


@pytest.fixture
def bundle1():
    return showcase_bundle(1)


@pytest.fixture
def bundle2():
    return showcase_bundle(2)


@pytest.fixture
def check_contract():
    def _check(drafts, bundle, ledger):
        for d in drafts:
            strict = d.account_id is not None  # account drafts must use contract keys only
            problems = contract_problems(d, bundle, ledger, strict=strict)
            assert not problems, f"{d.draft_id}: {problems}"

    return _check
