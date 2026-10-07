"""Regulatory change radar: a curated, human-reviewed diff between Wapsi's legal baseline and new or pending rules.

Read-only. The rows in ``data/docs/reg_radar.json`` are curated by hand from the primary documents, never generated
by an LLM at run time. Every quote is verbatim (at most 25 words) and is re-verified against the raw documents by
``uv run python -m wapsi.search.check_citations`` (fetch them with ``uv run python -m wapsi.search.radar fetch``;
``... radar verify --write`` refreshes each row's verification stamp).

This module loads and validates the registry and answers "what would change for this case if the pending rule were
adopted". It flags and suggests; a human decides. It never edits hold plans, clocks, drafts or citations. Every
number in a finding comes from the case bundle (amounts, dates) or from a check parameter that must itself appear in
the row's verified quote (``validate_radar``).

Check kinds (``Clause.check.kind``):

| kind | looks at | finding |
|---|---|---|
| ``lea_backed`` | holding decisions + NCRP ids | holds that rest on the CFCFRMS / LEA notice (draft carve-out) |
| ``hold_age_vs_max`` | hold start (``clocks.hold_start``) | day count against the draft maximum and the SOP lapse |
| ``innocent_holder_str`` | bona fide recipient / unwitting conduit holds | STR decision vs the draft's mule definition |
| ``intermediary_holds`` | ``HOLD_AMOUNT_INTERMEDIARY`` / ``HOLD_IN_POOL`` | nodal / pool / escrow holds (outside the draft) |
| ``amount_level_only`` | plan totals, account-level exceptions | amount-specific default vs the draft's last resort |
| ``below_floor`` | hold amounts | holds under the draft's minimum transaction amount |
| ``customer_notice`` | ``CUSTOMER_NOTICE`` drafts | held accounts without a drafted notice |
| ``retired_citations`` | draft + decision citation ids | references to repealed instruments |
| ``none`` | nothing | no case-specific effect |

Pending rules are a DRAFT (AGENTS.md): finding texts name the RBI draft "the RBI DRAFT", ``Radar.rests_on_draft``
tells the UI to label a row DRAFT, and nothing here treats a draft as in force.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wapsi.citations import KNOWN_IDS
from wapsi.domain.calendar import end_of_day_ist
from wapsi.domain.clocks import HOLD_LAPSE_DAYS, hold_start
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    AccountLevelReason,
    CaseBundle,
    ClockKind,
    Disposition,
    DraftKind,
    HoldDecision,
    Role,
)
from wapsi.money import eq, fmt_inr
from wapsi.search.normalize import quote_word_count
from wapsi.timeutil import ensure_utc, fmt_ist

REPO_ROOT = Path(__file__).resolve().parents[3]
REG_RADAR_JSON = REPO_ROOT / "data" / "docs" / "reg_radar.json"
MAX_QUOTE_WORDS = 25
APPROACHING_DAYS = 7  # a hold within this many days of the draft maximum is flagged for review
DRAFT = "the RBI DRAFT"  # how finding texts name the pending rule (AGENTS.md: always labelled DRAFT, never in force)
SUPERVISOR_APPROVED = "SUPERVISOR_APPROVED_EXCEPTION"  # reason code on an approved account-level exception

SourceStatus = Literal["draft", "final", "in-force", "repealed", "reference"]
ChangeKind = Literal["scope", "stricter", "new", "aligned", "repealed"]
WapsiStatus = Literal["open", "reflected", "no_change"]
Severity = Literal["info", "review", "action"]
CheckKind = Literal[
    "none",
    "lea_backed",
    "hold_age_vs_max",
    "innocent_holder_str",
    "intermediary_holds",
    "amount_level_only",
    "below_floor",
    "customer_notice",
    "retired_citations",
]

SEVERITY_RANK: dict[str, int] = {"info": 0, "review": 1, "action": 2}
INTERMEDIARY_DISPOSITIONS = frozenset({Disposition.HOLD_AMOUNT_INTERMEDIARY, Disposition.HOLD_IN_POOL})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Side(_Model):
    """One verbatim quote on one side of the diff."""

    source_id: str
    para: str
    page: int | None = None  # physical PDF page; None for HTML
    quote: str
    citation_id: str | None = None  # the Wapsi citation that already quotes this passage, if any


class RadarSource(_Model):
    id: str
    short: str
    status: SourceStatus
    corpus: bool = False  # True: fetched and indexed by wapsi.search (title / url / type come from there)
    title: str = ""
    publisher: str = ""
    type: Literal["pdf", "html"] | None = None
    url: str = ""
    landing_url: str = ""
    issued: date | None = None
    comments_close: date | None = None
    effective: date | None = None
    fetch_headers: Literal["polite", "browser"] = "polite"
    expect_text: str = ""
    start_marker: str = ""
    end_marker: str = ""
    licence: str = ""
    notes: str = ""
    evidence: list[Side] = Field(default_factory=list)  # quotes backing the dates / status above


class Affects(_Model):
    rules: list[str] = Field(default_factory=list)  # plain-English Wapsi rules (AGENTS.md hard rules)
    clocks: list[ClockKind] = Field(default_factory=list)
    dispositions: list[Disposition] = Field(default_factory=list)
    draft_kinds: list[DraftKind] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)


class Check(_Model):
    kind: CheckKind = "none"
    days: int | None = None
    amount: float | None = None
    retired_citation_ids: list[str] = Field(default_factory=list)


class Review(_Model):
    status: Literal["pending", "approved", "rejected"] = "pending"
    curated_by: str
    curated_on: date
    reviewed_by: str | None = None
    reviewed_on: date | None = None
    note: str = ""


class Verification(_Model):
    status: Literal["verified", "unverified"] = "unverified"
    checked_on: date | None = None
    detail: list[str] = Field(default_factory=list)


class Clause(_Model):
    id: str
    topic: str
    title: str
    change: ChangeKind
    new: list[Side] = Field(min_length=1)
    baseline: list[Side] = Field(default_factory=list)
    baseline_note: str = ""  # why there is no baseline quote, when there is none
    applies_to: str
    impact: str
    suggested_action: str
    affects: Affects = Field(default_factory=Affects)
    check: Check = Field(default_factory=Check)
    wapsi_status: WapsiStatus
    review: Review
    verification: Verification = Field(default_factory=Verification)

    @property
    def verified(self) -> bool:
        return self.verification.status == "verified"

    def sides(self) -> list[tuple[str, Side]]:
        """[("new[0]", side), ..., ("baseline[0]", side), ...]"""
        return [(f"new[{i}]", s) for i, s in enumerate(self.new)] + [
            (f"baseline[{i}]", s) for i, s in enumerate(self.baseline)
        ]


class Radar(_Model):
    schema_version: int = 1
    title: str
    note: str
    as_of: date
    baseline_source: str
    sources: list[RadarSource]
    clauses: list[Clause]

    def source(self, source_id: str) -> RadarSource | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def clause(self, clause_id: str) -> Clause | None:
        return next((c for c in self.clauses if c.id == clause_id), None)

    def rests_on_draft(self, clause: Clause) -> bool:
        """True if a new-side quote comes from a source still in draft (always labelled DRAFT, never in force)."""
        return any((s := self.source(side.source_id)) is not None and s.status == "draft" for side in clause.new)

    def visible(self, include_unverified: bool = False) -> list[Clause]:
        """Rows shown by default: only those whose every quote was verified against its source."""
        return [c for c in self.clauses if include_unverified or c.verified]


# --------------------------------------------------------------------------- load / validate


def radar_path() -> Path:
    """``WAPSI_REG_RADAR_JSON``, else ``$WAPSI_DOCS_DIR/reg_radar.json`` (a relocated docs folder), else the repo's."""
    env = os.environ.get("WAPSI_REG_RADAR_JSON")
    if env:
        return Path(env)
    docs = os.environ.get("WAPSI_DOCS_DIR")
    return Path(docs) / "reg_radar.json" if docs else REG_RADAR_JSON


def load_radar(path: Path | None = None) -> Radar:
    p = path or radar_path()
    return Radar.model_validate(json.loads(p.read_text(encoding="utf-8")))


@lru_cache(maxsize=4)
def _cached(path: str, mtime_ns: int) -> Radar:
    return load_radar(Path(path))


def load_radar_cached(path: Path | None = None) -> Radar | None:
    """For the UI: None when the registry file is absent."""
    p = path or radar_path()
    try:
        return _cached(str(p), p.stat().st_mtime_ns)
    except FileNotFoundError:
        return None


def _number_in(text: str, value: float) -> bool:
    """True if ``value`` is written in ``text`` (digits, optionally with Indian / Western grouping)."""
    wanted = f"{value:g}"
    return any(n.replace(",", "") == wanted for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text))


def validate_radar(radar: Radar, corpus_ids: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Structural problems (empty list = valid). ``corpus_ids``: ids of wapsi.search.sources.SOURCES."""
    problems: list[str] = []
    ids = [s.id for s in radar.sources]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate source id {dup}")
    known_sources = set(ids)
    for s in radar.sources:
        if s.corpus:
            if corpus_ids and s.id not in corpus_ids:
                problems.append(f"source {s.id}: marked corpus but not in wapsi.search.sources")
        else:
            if not (s.title and s.url and s.type):
                problems.append(f"source {s.id}: title, url and type are required for non-corpus sources")
        for i, side in enumerate(s.evidence):
            problems += _side_problems(f"source {s.id} evidence[{i}]", side, known_sources)
    if radar.baseline_source not in known_sources:
        problems.append(f"baseline_source {radar.baseline_source} is not a registered source")

    cids = [c.id for c in radar.clauses]
    for dup in sorted({i for i in cids if cids.count(i) > 1}):
        problems.append(f"duplicate clause id {dup}")
    for c in radar.clauses:
        for label, side in c.sides():
            problems += _side_problems(f"{c.id} {label}", side, known_sources)
        if not c.baseline and not c.baseline_note:
            problems.append(f"{c.id}: no baseline quote and no baseline_note")
        for cid in c.affects.citation_ids:
            if cid not in KNOWN_IDS:
                problems.append(f"{c.id}: unknown citation id {cid}")
        for cid in c.check.retired_citation_ids:
            if cid in KNOWN_IDS:
                problems.append(f"{c.id}: retired citation id {cid} is still in the registry")
        new_text = " ".join(s.quote for s in c.new)
        for name, value in (("days", c.check.days), ("amount", c.check.amount)):
            if value is not None and not _number_in(new_text, value):
                problems.append(f"{c.id}: check.{name}={value:g} does not appear in a new-side quote")
        if c.check.kind == "hold_age_vs_max" and c.check.days is None:
            problems.append(f"{c.id}: hold_age_vs_max needs check.days")
        if c.check.kind == "below_floor" and c.check.amount is None:
            problems.append(f"{c.id}: below_floor needs check.amount")
        if c.review.status != "pending" and not (c.review.reviewed_by and c.review.reviewed_on):
            problems.append(f"{c.id}: a reviewed row needs reviewed_by and reviewed_on")
    return problems


def _side_problems(where: str, side: Side, known_sources: set[str]) -> list[str]:
    out = []
    if side.source_id not in known_sources:
        out.append(f"{where}: unknown source {side.source_id}")
    words = quote_word_count(side.quote)
    if not side.quote.strip():
        out.append(f"{where}: empty quote")
    elif words > MAX_QUOTE_WORDS:
        out.append(f"{where}: quote has {words} words (> {MAX_QUOTE_WORDS})")
    if side.citation_id and side.citation_id not in KNOWN_IDS:
        out.append(f"{where}: unknown citation id {side.citation_id}")
    return out


# --------------------------------------------------------------------------- what changes for this case


class Finding(_Model):
    severity: Severity
    text: str
    account_id: str | None = None
    amount: float | None = None
    dates: dict[str, datetime] = Field(default_factory=dict)


class ClauseImpact(_Model):
    clause_id: str
    title: str
    applies: bool  # the clause touches something in this case
    severity: Severity
    summary: str
    findings: list[Finding] = Field(default_factory=list)


def _holds(bundle: CaseBundle) -> list[HoldDecision]:
    if bundle.plan is None:
        return []
    return sorted(
        (d for d in bundle.plan.decisions if d.disposition in HOLDING_DISPOSITIONS and d.hold_amount > 0),
        key=lambda d: d.account_id,
    )


def _calendar_due(start: datetime, days: int) -> datetime:
    return end_of_day_ist(ensure_utc(start) + timedelta(days=days))


def _lea_backed(bundle: CaseBundle) -> bool:
    """True when the case's holds rest on NCRP complaints (a CFCFRMS notice from an LEA), i.e. inside the
    draft's carve-out for holds placed or continued on a specific LEA instruction."""
    return bool(bundle.case.ncrp_ids)


def _n(count: int, noun: str, plural: str | None = None) -> str:
    """'1 hold', '2 holds'."""
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def _impact(clause: Clause, summary: str, findings: list[Finding], applies: bool | None = None) -> ClauseImpact:
    sev = max((f.severity for f in findings), key=SEVERITY_RANK.__getitem__, default="info")
    return ClauseImpact(
        clause_id=clause.id,
        title=clause.title,
        applies=bool(findings) if applies is None else applies,
        severity=sev,
        summary=summary,
        findings=findings,
    )


def _no_plan(clause: Clause) -> ClauseImpact:
    return _impact(clause, "No hold plan yet, so nothing in this case to compare.", [], applies=False)


def _check_lea_backed(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    holds = _holds(bundle)
    if not holds:
        return _no_plan(clause) if bundle.plan is None else _impact(clause, "This case has no holds.", [])
    total = sum(d.hold_amount for d in holds)
    ids = bundle.case.ncrp_ids
    ncrp = ", ".join(ids) or "none recorded"
    findings = [
        Finding(severity="info", account_id=d.account_id, amount=d.hold_amount,
                text=f"{d.account_id}: {fmt_inr(d.hold_amount)} held on the CFCFRMS notice for NCRP {ncrp}.")
        for d in holds
    ]
    one = len(holds) == 1
    summary = (f"{_n(len(holds), 'hold')} totalling {fmt_inr(total)} {'comes' if one else 'come'} from "
               f"{'NCRP complaint' if len(ids) == 1 else 'NCRP complaints'} {ncrp}; {'it stays' if one else 'they stay'}"
               " on the SOP track while the LEA notice is on file.")
    return _impact(clause, summary, findings)


def _check_hold_age(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    holds = _holds(bundle)
    if bundle.plan is None:
        return _no_plan(clause)
    max_days = int(clause.check.days or 0)
    now = ensure_utc(now)
    lea = _lea_backed(bundle)
    findings: list[Finding] = []
    for d in holds:
        start, basis = hold_start(bundle, d.account_id)
        cap = _calendar_due(start, max_days)
        lapse = _calendar_due(start, HOLD_LAPSE_DAYS)
        age = max(0, (now - ensure_utc(start)).days)
        note = " (hold not executed yet; counted from the trace)" if basis == "computed" else ""
        if now > cap:
            # Inside the LEA carve-out the draft maximum does not bite: confirm the notice is on file (review).
            # Without an LEA basis the hold would breach the draft maximum if adopted (action).
            sev: Severity = "review" if lea else "action"
            basis_text = (f"It rests on the CFCFRMS notice, so {DRAFT}'s LEA carve-out applies; confirm the notice "
                          f"is on file, since without it the hold would breach {DRAFT} if adopted."
                          if lea else
                          f"With no LEA or competent-authority instruction on file, it would breach {DRAFT} if "
                          "adopted.")
            text = (f"{d.account_id}: held {age} days{note}, past {DRAFT}'s {max_days}-day maximum for "
                    f"bank-initiated holds ({fmt_ist(cap, with_time=False)}). {basis_text} "
                    f"SOP lapse {fmt_ist(lapse, with_time=False)}.")
        elif cap - now <= timedelta(days=APPROACHING_DAYS):
            sev = "review"
            text = (f"{d.account_id}: day {age} of {max_days}{note}; {DRAFT}'s maximum falls on "
                    f"{fmt_ist(cap, with_time=False)}, the SOP lapse on {fmt_ist(lapse, with_time=False)}.")
        else:
            sev = "info"
            text = (f"{d.account_id}: day {age} of {max_days}{note}; {DRAFT} maximum "
                    f"{fmt_ist(cap, with_time=False)}, SOP lapse {fmt_ist(lapse, with_time=False)}.")
        findings.append(Finding(severity=sev, account_id=d.account_id, amount=d.hold_amount, text=text,
                                dates={"start": ensure_utc(start), "draft_max": cap, "sop_lapse": lapse}))
    past = sum(1 for f in findings if now > f.dates["draft_max"])
    if not findings:
        summary = "This case has no holds."
    elif past:
        summary = (f"{past} of {_n(len(findings), 'hold')} {'is' if past == 1 else 'are'} older than {DRAFT}'s "
                   f"{max_days}-day maximum for bank-initiated holds"
                   + ("; the CFCFRMS notice keeps them on the SOP track." if lea else "."))
    elif len(findings) == 1:
        summary = f"The hold is within {DRAFT}'s {max_days}-day maximum today."
    else:
        summary = f"All {len(findings)} holds are within {DRAFT}'s {max_days}-day maximum today."
    return _impact(clause, summary, findings)


def _is_conduit(d: HoldDecision) -> bool:
    return d.role == Role.UNWITTING_CONDUIT or Role.UNWITTING_CONDUIT.value in d.reason_codes


def _check_innocent(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    if bundle.plan is None:
        return _no_plan(clause)
    rows = [d for d in _holds(bundle)
            if d.disposition == Disposition.HOLD_AMOUNT_NO_ESCALATION
            or d.role in (Role.BONA_FIDE_RECIPIENT, Role.UNWITTING_CONDUIT)]
    findings = []
    for d in rows:
        if _is_conduit(d):
            text = (f"{d.account_id}: {fmt_inr(d.hold_amount)} held as an unwitting conduit; Wapsi recommends an STR "
                    f"for the officer's decision. Under {DRAFT} an account used unknowingly to transfer fraud "
                    "proceeds on behalf of another person is a Money Mule Account: record the STR decision.")
        else:
            text = (f"{d.account_id}: {fmt_inr(d.hold_amount)} held as a bona fide recipient (no STR by default). "
                    f"{DRAFT[0].upper()}{DRAFT[1:]} counts accounts used, knowingly or unknowingly, on behalf of "
                    "another person as Money Mule Accounts: record in the EDD memo why this account is not one.")
        findings.append(Finding(severity="review", account_id=d.account_id, amount=d.hold_amount, text=text))
    summary = (f"{_n(len(rows), 'innocent-holder hold')} {'needs' if len(rows) == 1 else 'need'} a recorded STR "
               "decision." if rows else "No innocent-holder holds in this case.")
    return _impact(clause, summary, findings)


def _check_intermediary(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    if bundle.plan is None:
        return _no_plan(clause)
    rows = [d for d in _holds(bundle) if d.disposition in INTERMEDIARY_DISPOSITIONS]
    findings = [
        Finding(severity="info", account_id=d.account_id, amount=d.hold_amount,
                text=(f"{d.account_id}: {fmt_inr(d.hold_amount)} held in a nodal / pool account on the CFCFRMS "
                      f"notice; {DRAFT}'s bank-initiated hold would not apply to it."))
        for d in rows
    ]
    summary = (f"{_n(len(rows), 'nodal / pool hold')} {'stays' if len(rows) == 1 else 'stay'} on the CFCFRMS "
               "notice." if rows else "No nodal, pool or escrow holds in this case.")
    return _impact(clause, summary, findings)


def _check_amount_level(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    plan = bundle.plan
    if plan is None:
        return _no_plan(clause)
    holds = _holds(bundle)
    if not eq(plan.over_hold_amount, 0.0):
        f = Finding(severity="action", amount=plan.over_hold_amount,
                    text=f"The plan holds {fmt_inr(plan.over_hold_amount)} beyond the tainted money; fix before approval.")
        return _impact(clause, "Over-hold detected.", [f])
    findings: list[Finding] = []
    exceptions = [d for d in holds if d.disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD]
    reasons = {r.value for r in AccountLevelReason}
    for d in exceptions:
        recorded = ", ".join(c.replace("_", " ").lower() for c in d.reason_codes if c in reasons) or "none recorded"
        if SUPERVISOR_APPROVED in d.reason_codes:
            findings.append(Finding(
                severity="info", account_id=d.account_id, amount=d.hold_amount,
                text=(f"{d.account_id}: account-level hold applied as a recorded, supervisor-approved exception "
                      f"(reasons: {recorded}); {DRAFT} also allows it only as a last resort. The amount-specific "
                      f"hold stays {fmt_inr(d.hold_amount)}.")))
        else:
            findings.append(Finding(
                severity="action", account_id=d.account_id, amount=d.hold_amount,
                text=(f"{d.account_id}: account-level hold without a recorded supervisor approval; neither Wapsi's "
                      f"rules nor {DRAFT}'s last-resort test allow it. Revert to the amount-specific hold.")))
    findings.append(Finding(
        severity="info", amount=plan.total_hold,
        text=(f"Wapsi holds {fmt_inr(plan.total_hold)} across {_n(len(holds), 'account')}; the whole-account freeze "
              f"baseline (for comparison only) would immobilise {fmt_inr(plan.baseline_freeze_amount)}.")))
    k = len(exceptions)
    summary = ("Amount-specific holds only; no account-level hold." if not k else
               f"{_n(k, 'account-level hold')} as a recorded exception; every other hold is amount-specific.")
    return _impact(clause, summary, findings)


def _check_below_floor(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    if bundle.plan is None:
        return _no_plan(clause)
    floor = float(clause.check.amount or 0.0)
    rows = [d for d in _holds(bundle) if d.hold_amount < floor]
    findings = [
        Finding(severity="info", account_id=d.account_id, amount=d.hold_amount,
                text=(f"{d.account_id}: {fmt_inr(d.hold_amount)} is below {fmt_inr(floor, paise=False)}; it could "
                      f"not be a bank-initiated hold under {DRAFT}, only a CFCFRMS hold."))
        for d in rows
    ]
    summary = (f"{_n(len(rows), 'hold')} below {fmt_inr(floor, paise=False)}."
               if rows else f"No hold in this case is below {fmt_inr(floor, paise=False)}.")
    return _impact(clause, summary, findings)


def _check_customer_notice(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    if bundle.plan is None:
        return _no_plan(clause)
    holds = _holds(bundle)
    noticed = {d.account_id for d in bundle.drafts if d.kind == DraftKind.CUSTOMER_NOTICE}
    missing = [d for d in holds if d.account_id not in noticed]
    # The draft's notice duty binds bank-initiated holds; for CFCFRMS holds the SOP only permits telling the holder.
    sev: Severity = "info" if _lea_backed(bundle) else "review"
    findings = [
        Finding(severity=sev, account_id=d.account_id, amount=d.hold_amount,
                text=f"{d.account_id}: no customer notice drafted yet for the {fmt_inr(d.hold_amount)} hold.")
        for d in missing
    ]
    if not holds:
        summary = "This case has no holds."
    elif missing:
        summary = f"No customer notice drafted yet for {len(missing)} of {_n(len(holds), 'held account')}."
    else:
        summary = f"A customer notice is drafted for every held account ({len(holds)})."
    return _impact(clause, summary, findings, applies=bool(holds))


def _check_retired(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    retired = set(clause.check.retired_citation_ids)
    hits: list[Finding] = []
    for d in bundle.drafts:
        for cid in sorted(retired & set(d.citation_ids)):
            hits.append(Finding(severity="action", text=f"Draft {d.draft_id} cites {cid}; replace it."))
    for dec in bundle.plan.decisions if bundle.plan else []:
        for cid in sorted(retired & set(dec.citation_ids)):
            hits.append(Finding(severity="action", account_id=dec.account_id,
                                text=f"Decision for {dec.account_id} cites {cid}; replace it."))
    names = ", ".join(sorted(retired)) or "the repealed instrument"
    summary = (f"{_n(len(hits), 'reference')} to {names}." if hits
               else f"No draft or decision in this case cites {names}.")
    return _impact(clause, summary, hits)


def _check_none(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    return _impact(clause, "No case-specific effect; see the suggested action.", [], applies=False)


_CHECKS = {
    "none": _check_none,
    "lea_backed": _check_lea_backed,
    "hold_age_vs_max": _check_hold_age,
    "innocent_holder_str": _check_innocent,
    "intermediary_holds": _check_intermediary,
    "amount_level_only": _check_amount_level,
    "below_floor": _check_below_floor,
    "customer_notice": _check_customer_notice,
    "retired_citations": _check_retired,
}


def clause_impact(clause: Clause, bundle: CaseBundle, now: datetime) -> ClauseImpact:
    return _CHECKS[clause.check.kind](clause, bundle, now)


def case_impact(
    radar: Radar, bundle: CaseBundle, now: datetime, *, include_unverified: bool = False
) -> list[ClauseImpact]:
    """What each (verified) radar row would mean for this case, most severe first, then registry order."""
    order = {c.id: i for i, c in enumerate(radar.clauses)}
    out = [clause_impact(c, bundle, now) for c in radar.visible(include_unverified)]
    return sorted(out, key=lambda x: (-SEVERITY_RANK[x.severity], not x.applies, order[x.clause_id]))
