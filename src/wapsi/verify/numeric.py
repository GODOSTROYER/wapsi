"""Numeric checks: every slot is recomputed from the CaseBundle (docs/draft-contract.md) and every amount,
account number, UTR and date in the draft must trace back to the bundle.

- slots: each value equals its resolver's value;
- positional binding: title and body equal ``structured["skeleton"]`` filled with the recomputed values, so a
  correct value in the wrong place (a swapped hold / disputed amount, a reordered UTR list) fails;
- amounts in title/body (``wapsi.money.find_money_mentions``: digits or words, any currency marker or scale word)
  equal a verified slot amount;
- account numbers, UTR-like references and dates in any spelling (``wapsi.money``) belong to the case, in the
  title/body and in the structured payload;
- every money value in the structured payload is an amount of the case.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from wapsi import money
from wapsi.citations import registry
from wapsi.models import (
    AccountState,
    CaseBundle,
    CheckResult,
    Complaint,
    Draft,
    HoldDecision,
    HoldPlan,
    TraceResult,
)
from wapsi.ports import Ledger
from wapsi.timeutil import fmt_ist
from wapsi.verify.report import fail, ok, warn

NAME = "numeric"

ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}-\d{10}(?![0-9])")
PLACEHOLDER_RE = re.compile(r"⟦[^⟧]*⟧|\{\{.*?\}\}|\{%.*?%\}")
SKELETON_SLOT_RE = re.compile(r"⟦([^⟦⟧\n]{1,160})⟧")  # same grammar as wapsi.draft.render.PLACEHOLDER_RE


class SlotError(Exception):
    """The slot key is known but cannot be resolved for this draft (error)."""


class UnknownSlot(Exception):
    """The slot key is not in the draft contract (warning)."""


def join(items) -> str:
    return ", ".join(items)


@dataclass
class SlotResolver:
    draft: Draft
    bundle: CaseBundle
    ledger: Ledger | None = None
    _cache: dict[str, str] = field(default_factory=dict)

    # -- context
    @property
    def acct(self) -> str | None:
        return self.draft.account_id

    def _need_acct(self) -> str:
        if not self.acct:
            raise SlotError("draft has no account_id")
        return self.acct

    @property
    def trace(self) -> TraceResult:
        if self.bundle.trace is None:
            raise SlotError("bundle has no trace")
        return self.bundle.trace

    @property
    def plan(self) -> HoldPlan:
        if self.bundle.plan is None:
            raise SlotError("bundle has no hold plan")
        return self.bundle.plan

    @property
    def dec(self) -> HoldDecision:
        acct = self._need_acct()
        for d in self.plan.decisions:
            if d.account_id == acct:
                return d
        raise SlotError(f"no plan decision for {acct}")

    @property
    def st(self) -> AccountState:
        acct = self._need_acct()
        st = self.trace.accounts.get(acct)
        if st is None:
            raise SlotError(f"{acct} is not in the trace")
        return st

    def complaint(self, ncrp_id: str) -> Complaint:
        for c in self.bundle.complaints:
            if c.ncrp_id == ncrp_id:
                return c
        if ncrp_id in self.bundle.case.ncrp_ids and self.ledger is not None:
            try:
                return self.ledger.complaint(ncrp_id)
            except LookupError:
                pass
        raise SlotError(f"complaint {ncrp_id} is not part of this case")

    def inflow_edges(self):
        acct = self._need_acct()
        return sorted(
            (e for e in self.trace.edges if e.to_account == acct and e.tainted_total > 0),
            key=lambda e: (e.ts, e.txn_id),
        )

    def holder_name(self) -> str:
        acct = self._need_acct()
        if self.ledger is None:
            raise SlotError("holder_name needs the ledger")
        account = self.ledger.accounts([acct]).get(acct)
        if account is None or not account.customer_id:
            raise SlotError(f"{acct} has no customer in the ledger")
        cust = self.ledger.customers([account.customer_id]).get(account.customer_id)
        if cust is None:
            raise SlotError(f"customer {account.customer_id} not found in the ledger")
        return cust.name

    def _victim_amount(self, ncrp_id: str) -> str:
        if self.acct:
            per = self.dec.per_victim
            if ncrp_id not in per:
                raise SlotError(f"decision for {self.acct} has no amount for {ncrp_id}")
            return money.fmt_inr(per[ncrp_id])
        per = self.plan.per_victim_recoverable
        if ncrp_id not in per:
            raise SlotError(f"plan has no recoverable amount for {ncrp_id}")
        return money.fmt_inr(per[ncrp_id])

    # -- resolvers
    def _simple(self) -> dict[str, Callable[[], str]]:
        fmt = money.fmt_inr
        return {
            "account_id": self._need_acct,
            "bank_id": lambda: self.st.bank_id,
            "disposition": lambda: str(self.dec.disposition),
            "hold_amount": lambda: fmt(self.dec.hold_amount),
            "forward_hold_amount": lambda: fmt(self.dec.forward_hold_amount),
            "tainted_amount": lambda: fmt(self.dec.tainted_amount),
            "balance_now": lambda: fmt(self.dec.balance_now),
            "disputed_amount": lambda: fmt(self.st.tainted_received_total),
            "utrs": lambda: join(sorted(e.utr for e in self.inflow_edges())),
            "inflow_dates": lambda: join(
                dict.fromkeys(fmt_ist(e.ts, with_time=False) for e in self.inflow_edges())
            ),
            "ncrp_ids": lambda: join(sorted(self.bundle.case.ncrp_ids)),
            "total_stolen": lambda: fmt(self.trace.totals.stolen),
            "total_cashed_out": lambda: fmt(self.trace.totals.cashed_out),
            "total_outbound": lambda: fmt(self.trace.totals.outbound),
            "total_hold": lambda: fmt(self.plan.total_hold),
            "total_forward_hold": lambda: fmt(self.plan.total_forward_hold),
            "total_recoverable": lambda: fmt(self.plan.total_recoverable),
            "baseline_freeze_amount": lambda: fmt(self.plan.baseline_freeze_amount),
            "restoration_process": lambda: str(self.plan.restoration_process),
            "str_ground": lambda: str(self.plan.str_ground) if self.plan.str_ground else "",
            "holder_name": self.holder_name,
        }

    def _edge(self, txn_id: str):
        for e in self.trace.edges:
            if e.txn_id == txn_id:
                return e
        raise SlotError(f"{txn_id} is not a trail edge")

    def _prefixed(self) -> dict[str, Callable[[str], str]]:
        return {
            "victim_amount": self._victim_amount,
            "complaint_amount": lambda n: money.fmt_inr(self.complaint(n).amount_claimed),
            "reported_at": lambda n: fmt_ist(self.complaint(n).reported_at),
            "victim_account": lambda n: self.complaint(n).victim_account,
            "edge_amount": lambda t: money.fmt_inr(self._edge(t).amount),
            "edge_tainted": lambda t: money.fmt_inr(self._edge(t).tainted_total),
            "edge_ts": lambda t: fmt_ist(self._edge(t).ts),
            "edge_utr": lambda t: self._edge(t).utr,
        }

    def _qualified(self, account_id: str, key: str) -> str:
        """``<account_id>/<key>``: resolve an account-scoped key inside a case-level draft."""
        if account_id not in self.trace.accounts:
            raise SlotError(f"{account_id} is not in the trace")
        scoped = SlotResolver(
            self.draft.model_copy(update={"account_id": account_id}), self.bundle, self.ledger
        )
        return scoped.resolve(key)

    def resolve(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        simple = self._simple()
        head, qsep, rest = key.partition("/")
        if qsep and ACCOUNT_RE.fullmatch(head):
            value = self._qualified(head, rest)
        elif key in simple:
            value = simple[key]()
        else:
            prefix, sep, arg = key.partition(":")
            fn = self._prefixed().get(prefix)
            if not sep or fn is None or not arg:
                raise UnknownSlot(key)
            value = fn(arg)
        self._cache[key] = value
        return value


def resolve_slot(key: str, draft: Draft, bundle: CaseBundle, ledger: Ledger | None = None) -> str:
    """Recompute one slot value exactly as the drafter must render it."""
    return SlotResolver(draft, bundle, ledger).resolve(key)


def draft_text(draft: Draft) -> str:
    return f"{draft.title}\n{draft.body}"


def known_accounts(draft: Draft, bundle: CaseBundle) -> set[str]:
    out = {c.victim_account for c in bundle.complaints}
    if draft.account_id:
        out.add(draft.account_id)
    if bundle.trace is not None:
        out.update(bundle.trace.accounts)
    return {a.upper() for a in out if a}


def known_utrs(bundle: CaseBundle) -> set[str]:
    """UTRs of trail edges (and their transaction ids, which STR sheets carry), upper-case."""
    if bundle.trace is None:
        return set()
    return {x.upper() for e in bundle.trace.edges for x in (e.utr, e.txn_id) if x}


@lru_cache(maxsize=1)
def _citation_dates() -> dict[str, frozenset[str]]:
    """Dates written in each cited source's title / section ("FIU-IND circular dated 02.06.2025")."""
    out = {}
    for cid, cit in registry().items():
        found = money.find_dates(f"{cit.source_title} {cit.section}")
        out[cid] = frozenset(canon for _, canon in found if canon)
    return out


def known_dates(draft: Draft, bundle: CaseBundle, ledger: Ledger | None = None) -> set[str]:
    from wapsi.domain.clocks import clocks_for_bundle

    stamps: list[datetime | None] = [bundle.case.opened_at, draft.created_at]
    stamps += [c.reported_at for c in bundle.complaints]
    if bundle.trace is not None:
        t = bundle.trace
        stamps += [t.computed_at, t.window_start, t.window_end]
        stamps += [e.ts for e in t.edges]
        stamps += [s.first_tainted_at for s in t.accounts.values()]
    stamps += [d.created_at for d in bundle.drafts]
    stamps += [a.at for a in bundle.approvals]
    stamps += [x for a in bundle.actions for x in (a.created_at, a.executed_at)]
    for c in list(bundle.clocks) + clocks_for_bundle(bundle, draft.created_at):
        stamps += [c.start, c.due, c.warn_at]
    out = {fmt_ist(s, with_time=False) for s in stamps if s is not None}
    dates = _citation_dates()
    for cid in draft.citation_ids:
        out |= dates.get(cid, frozenset())
    return out


def ledger_dates(bundle: CaseBundle, ledger: Ledger | None) -> set[str]:
    """Account opening dates and holder dates of birth for the traced accounts (STR sheets carry them)."""
    if ledger is None or bundle.trace is None:
        return set()
    out: set[str] = set()
    accounts = ledger.accounts(sorted(bundle.trace.accounts))
    for a in accounts.values():
        if a.opened_at is not None:
            out.add(fmt_ist(a.opened_at, with_time=False))
    customers = ledger.customers({a.customer_id for a in accounts.values() if a.customer_id})
    for c in customers.values():
        if c.dob is not None:
            out.add(c.dob.strftime("%d-%m-%Y"))
    return out


def bundle_amounts(bundle: CaseBundle) -> set[float]:
    """Every amount the bundle holds (trace, plan, complaints), rounded to paise."""
    vals: list[float] = [0.0]
    vals += [c.amount_claimed for c in bundle.complaints]
    if bundle.trace is not None:
        t = bundle.trace
        vals += list(t.totals.model_dump().values()) + list(t.victim_seed.values())
        for e in t.edges:
            vals += [e.amount, e.tainted_total, *e.tainted.values()]
        for s in t.accounts.values():
            vals += [s.balance_now, s.tainted_now_total, s.tainted_received_total, s.tainted_sent_total]
            vals += list(s.tainted_now.values())
    if bundle.plan is not None:
        p = bundle.plan
        vals += [p.total_hold, p.total_forward_hold, p.total_recoverable, p.over_hold_amount]
        vals += [p.baseline_freeze_amount, *p.per_victim_recoverable.values()]
        for d in p.decisions:
            vals += [
                d.hold_amount,
                d.forward_hold_amount,
                d.tainted_amount,
                d.balance_now,
                *d.per_victim.values(),
            ]
    return {money.r2(abs(v)) for v in vals if isinstance(v, int | float)}


def _slot_amounts(values: dict[str, str]) -> list[tuple[float, str]]:
    out = []
    for key, value in values.items():
        for _, amount in money.find_amounts(value):
            out.append((amount, key))
    return out


# --------------------------------------------------------------------------- skeleton (positional binding)


def _diff_window(actual: str, expected: str) -> tuple[str, str]:
    """The first differing stretch of two strings, widened to whitespace boundaries."""
    p = 0
    while p < min(len(actual), len(expected)) and actual[p] == expected[p]:
        p += 1
    q = 0
    while q < min(len(actual), len(expected)) - p and actual[-1 - q] == expected[-1 - q]:
        q += 1
    while p > 0 and not actual[p - 1].isspace():
        p -= 1
    a_end, e_end = len(actual) - q, len(expected) - q
    while a_end < len(actual) and not actual[a_end].isspace():
        a_end += 1
    while e_end < len(expected) and not expected[e_end].isspace():
        e_end += 1
    return actual[p:a_end][:120], expected[p:e_end][:120]


def fill_skeleton(template: str, resolver: SlotResolver) -> tuple[str, list[str]]:
    """``template`` with every ``⟦key⟧`` replaced by the *recomputed* slot value; problems for keys that cannot be
    recomputed."""
    problems: list[str] = []

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        try:
            return resolver.resolve(key)
        except UnknownSlot:
            problems.append(f"'{key}' is not in the draft contract")
        except SlotError as exc:
            problems.append(f"'{key}' cannot be recomputed: {exc}")
        return m.group(0)

    return SKELETON_SLOT_RE.sub(sub, template), problems


def check_skeleton(draft: Draft, resolver: SlotResolver) -> list[CheckResult]:
    """Positional binding: the title and body must equal the draft's skeleton filled with recomputed values."""
    skel = draft.structured.get("skeleton") if isinstance(draft.structured, dict) else None
    if skel is None:
        if draft.structured.get("hand_written") is True:
            return []
        return [
            fail(
                NAME,
                "body_skeleton_missing",
                "the draft has no skeleton, so its values cannot be bound to their positions "
                "(only hand-written drafts may omit it)",
            )
        ]
    if not isinstance(skel, dict) or not all(isinstance(skel.get(p), str) for p in ("title", "body")):
        return [fail(NAME, "body_skeleton_malformed", "structured.skeleton needs 'title' and 'body' strings")]
    out: list[CheckResult] = []
    for part, actual in (("title", draft.title), ("body", draft.body)):
        filled, problems = fill_skeleton(skel[part], resolver)
        if problems:
            out.append(
                fail(NAME, "skeleton_unresolvable", f"{part} skeleton slots: " + "; ".join(problems), [])
            )
            continue
        if filled != actual:
            got, want = _diff_window(actual, filled)
            out.append(
                fail(
                    NAME,
                    "body_skeleton_mismatch",
                    f"the {part} differs from its skeleton filled with recomputed values: "
                    f"'{got}' where '{want}' is expected",
                    [got],
                )
            )
    return out


# --------------------------------------------------------------------------- structured payload

_SKIP_STRUCTURED = frozenset({"skeleton", "citations"})
_MONEY_KEYS = frozenset(
    {
        "amount", "amounts", "balance", "balance_now", "tainted", "tainted_now", "tainted_received",
        "tainted_sent", "tainted_total", "total_hold", "total_forward_hold", "total_recoverable",
        "total_stolen", "total_outbound", "total_cashed_out", "stolen", "hold", "forward_hold", "outbound",
        "cashed_out", "recoverable", "holdable", "merchant_settled", "unlocated", "held", "requested",
    }
)  # fmt: skip
_MONEY_PARENTS = frozenset(
    {"per_victim", "per_victim_recoverable", "tainted_by_complaint", "forward_holds", "totals", "victim_seed"}
)
_NON_MONEY_PARENTS = frozenset({"timelines_days", "timeline", "signals", "network"})


def _is_money_key(key: str, parents: tuple[str, ...]) -> bool:
    if any(p in _NON_MONEY_PARENTS for p in parents):
        return False
    k = key.lower()
    return (
        k in _MONEY_KEYS
        or k.endswith("_amount")
        or k.startswith("amount_")
        or (bool(parents) and parents[-1].lower() in _MONEY_PARENTS)
    )


def _walk(obj: Any, parents: tuple[str, ...] = ()):
    """(parents, key, value) for every leaf; dict keys are yielded as string leaves too."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            if not parents and ks in _SKIP_STRUCTURED:
                continue
            yield parents, "", ks
            if isinstance(v, dict | list | tuple):
                yield from _walk(v, (*parents, ks))
            else:
                yield parents, ks, v
    elif isinstance(obj, list | tuple):
        for v in obj:
            if isinstance(v, dict | list | tuple):
                yield from _walk(v, parents)
            else:
                yield parents, parents[-1] if parents else "", v


@dataclass
class _Found:
    accounts: set[str] = field(default_factory=set)
    utrs: set[str] = field(default_factory=set)
    amounts: list[tuple[str, float]] = field(default_factory=list)
    dates: list[tuple[str, str | None]] = field(default_factory=list)


def _scan_text(text: str, found: _Found, *, amounts: bool = True) -> None:
    found.accounts |= {canon for _, canon in money.find_account_ids(text)}
    found.utrs |= {canon for _, canon in money.find_utrs(text)}
    found.dates += money.find_dates(text)
    if amounts:
        found.amounts += [(m.text, m.value) for m in money.find_money_mentions(text)]


def _scan_structured(structured: dict[str, Any]) -> _Found:
    found = _Found()
    for parents, key, value in _walk(structured):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            _scan_text(value, found, amounts=bool(key))
        elif isinstance(value, int | float):
            if isinstance(value, int):
                _scan_text(str(value), found, amounts=False)
            if key and _is_money_key(key, parents):
                found.amounts.append((f"{'.'.join((*parents, key))} = {value}", float(value)))
    return found


def _identifier_checks(where: str, found: _Found, accounts: set[str], utrs: set[str]) -> list[CheckResult]:
    out: list[CheckResult] = []
    bad_accts = sorted(found.accounts - accounts)
    if bad_accts:
        out.append(
            fail(
                NAME,
                "unknown_account",
                f"account numbers in {where} not in the case trace or complaints: " + ", ".join(bad_accts),
                bad_accts,
            )
        )
    bad_utrs = sorted(found.utrs - utrs)
    if bad_utrs:
        out.append(
            fail(
                NAME,
                "unknown_utr",
                f"UTR-shaped tokens in {where} not on any trail edge: " + ", ".join(bad_utrs),
                bad_utrs,
            )
        )
    return out


def _date_check(where: str, found: _Found, dates: set[str]) -> list[CheckResult]:
    bad = [span for span, canon in found.dates if canon is None or canon not in dates]
    if not bad:
        return []
    uniq = list(dict.fromkeys(bad))
    return [
        fail(NAME, "unknown_date", f"dates in {where} not found in the case record: " + ", ".join(uniq), uniq)
    ]


def check_numeric(draft: Draft, bundle: CaseBundle, ledger: Ledger | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    resolver = SlotResolver(draft, bundle, ledger)
    verified: dict[str, str] = {}

    # 1. slots
    for key, value in sorted(draft.slots.items()):
        try:
            expected = resolver.resolve(key)
        except UnknownSlot:
            results.append(
                warn(
                    NAME,
                    "unknown_slot",
                    f"slot '{key}' is not in the draft contract; its value is not trusted",
                    [value],
                )
            )
            continue
        except SlotError as exc:
            results.append(
                fail(NAME, "slot_unresolvable", f"slot '{key}' cannot be recomputed: {exc}", [value])
            )
            continue
        verified[key] = expected
        if value != expected:
            results.append(
                fail(NAME, "slot_mismatch", f"slot '{key}' is '{value}', recomputed '{expected}'", [value])
            )

    # 2. positional binding: title/body == skeleton filled with recomputed values
    results += check_skeleton(draft, resolver)

    text = draft_text(draft)
    body = _Found()
    _scan_text(text, body)

    # 3. amounts in title/body (digits or words, any marker) must equal a verified slot amount
    allowed = _slot_amounts(verified)
    for span, value in body.amounts:
        if any(money.eq(value, a) for a, _ in allowed):
            continue
        if allowed:
            nearest, key = min(allowed, key=lambda p: abs(p[0] - value))
            hint = f"nearest slot amount is {money.fmt_inr(nearest)} ('{key}')"
        else:
            hint = "the draft has no verified amount slots"
        results.append(
            fail(
                NAME,
                "body_amount_unmatched",
                f"'{span}' ({money.fmt_inr(value)}) matches no slot amount; {hint}",
                [span],
            )
        )

    # 4. identifiers in title/body and structured payload
    accounts = known_accounts(draft, bundle)
    utrs = known_utrs(bundle)
    dates = known_dates(draft, bundle, ledger)
    results += _identifier_checks("body", body, accounts, utrs)
    results += _date_check("body", body, dates)

    structured = _scan_structured(draft.structured or {})
    results += _identifier_checks("structured", structured, accounts, utrs)
    results += _date_check("structured", structured, dates | ledger_dates(bundle, ledger))
    # every money value in the structured payload must be an amount of the case
    known = bundle_amounts(bundle) | {money.r2(a) for a, _ in allowed}
    bad_amounts = [span for span, value in structured.amounts if money.r2(abs(value)) not in known]
    if bad_amounts:
        uniq = list(dict.fromkeys(bad_amounts))
        results.append(
            fail(
                NAME,
                "structured_amount_unmatched",
                "amounts in the structured payload match no slot or case amount: " + "; ".join(uniq[:10]),
                uniq,
            )
        )

    # 5. leftovers from templating
    leftovers = [m.group(0) for m in PLACEHOLDER_RE.finditer(text)]
    if leftovers:
        results.append(
            fail("format", "unfilled_placeholder", "template placeholders left in the draft", leftovers)
        )

    if not any(r.name == NAME and not r.passed and r.severity == "error" for r in results):
        results.append(
            ok(
                NAME,
                f"{len(verified)} slot(s) recomputed; positions, amounts, accounts, UTRs and dates trace to the case",
            )
        )
    return results
