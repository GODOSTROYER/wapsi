"""End-to-end case processing: screen → trace → classify → plan → draft → verify → clocks.

Every edition calls this with its own adapters (BigQuery + Gemini on Google Cloud, Parquet + vLLM on AMD).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from wapsi.audit.chain import append_event, sha256_hex
from wapsi.config import WapsiConfig
from wapsi.models import (
    HOLDING_DISPOSITIONS,
    Action,
    ActionKind,
    ActionStatus,
    Approval,
    ApprovalDecision,
    AuditEvent,
    Case,
    CaseBundle,
    CaseStatus,
    Complaint,
    Disposition,
    Draft,
    DraftKind,
)
from wapsi.ports import LLM, ActionSink, CaseStore, DocSearch, Guard, Ledger
from wapsi.timeutil import utcnow

SYSTEM_ACTOR = "wapsi"
SIMULATED_CFCFRMS = "SIMULATED_CFCFRMS"  # CFCFRMS actions are recorded by a simulated interface, never sent


def _audit(bundle: CaseBundle, action: str, object_type: str, object_id: str, *, actor: str = SYSTEM_ACTOR,
           role: str = "system", at: datetime | None = None, **details) -> None:
    """Append an event to the case's hash-chained, append-only audit log (``wapsi.audit``)."""
    append_event(bundle.audit, AuditEvent(at=at or utcnow(), actor=actor, role=role, action=action,
                                          object_type=object_type, object_id=object_id, details=details),
                 case_id=bundle.case.case_id)


def _draft_created(bundle: CaseBundle, d: Draft, **kw) -> None:
    _audit(bundle, "draft_created", "draft", d.draft_id, kind=d.kind.value, version=d.version,
           account_id=d.account_id, llm_model=d.llm_model, draft_sha256=draft_fingerprint(d), **kw)


def group_complaints(ledger: Ledger, complaints: Iterable[Complaint]) -> list[list[str]]:
    """Group complaints into cases.

    Two complaints belong to the same case when their reported transfers credit the same account (multi-victim
    mule -> Process 2), or when one complaint's victim account received another complaint's money (a victim
    account that forwarded someone else's stolen money must be traced together, or the same rupees are
    claimed twice).
    """
    complaints = list(complaints)
    parent: dict[str, str] = {c.ncrp_id: c.ncrp_id for c in complaints}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_account: dict[str, list[str]] = defaultdict(list)
    for c in complaints:
        by_account[c.victim_account].append(c.ncrp_id)
        for t in ledger.complaint_transactions(c.ncrp_id):
            if t.to_account:
                by_account[t.to_account].append(c.ncrp_id)
            if t.from_account and t.from_account != c.victim_account:
                by_account[t.from_account].append(c.ncrp_id)
    for ids in by_account.values():
        for other in ids[1:]:
            ra, rb = find(ids[0]), find(other)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = defaultdict(list)
    for c in complaints:
        groups[find(c.ncrp_id)].append(c.ncrp_id)
    return [sorted(g) for _, g in sorted(groups.items())]


def process_case(
    ledger: Ledger,
    ncrp_ids: list[str],
    *,
    llm: LLM,
    cfg: WapsiConfig | None = None,
    search: DocSearch | None = None,
    guard: Guard | None = None,
    scorer=None,
    store: CaseStore | None = None,
    case_id: str | None = None,
    assigned_to: str | None = None,
    draft: bool = True,
    lea_request=None,
) -> CaseBundle:
    """``lea_request`` (optional ``wapsi.verify.lea_request.LeaHoldRequest``): a police / LEA hold request for this
    case, checked against the eight-field checklist once the plan exists; the result (missing fields, the hold
    limited to the identifiable amount and the clarification letter) is recorded as a ``lea_request_checked`` audit
    event. It never changes the plan."""
    from wapsi.classify import classify_trace
    from wapsi.domain.clocks import clocks_for_bundle
    from wapsi.draft.drafter import draft_case
    from wapsi.graph.holdplan import build_hold_plan
    from wapsi.graph.trace import trace_case
    from wapsi.verify import verify_draft, verify_plan
    from wapsi.verify.injection import RulesGuard

    cfg = cfg or WapsiConfig()
    guard = guard or RulesGuard()
    ncrp_ids = sorted(ncrp_ids)
    case_id = case_id or f"CASE-{ncrp_ids[0]}"
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    complaints = [ledger.complaint(n) for n in ncrp_ids]
    case = Case(case_id=case_id, ncrp_ids=ncrp_ids, opened_at=utcnow(), assigned_to=assigned_to)
    bundle = CaseBundle(case=case, complaints=complaints)
    _audit(bundle, "case_opened", "case", case_id, ncrp_ids=ncrp_ids)

    # untrusted input screening
    for c in complaints:
        g = guard.screen(c.narrative_text)
        if g.flagged:
            _audit(bundle, "untrusted_instruction_detected", "complaint", c.ncrp_id,
                   categories=g.categories, quoted_spans=g.quoted_spans)
    timings["screen_s"] = time.perf_counter() - t0

    t = time.perf_counter()
    bundle.trace = trace_case(ledger, ncrp_ids, cfg.trace, case_id=case_id)
    bundle.case.status = CaseStatus.TRACED
    timings["trace_s"] = time.perf_counter() - t
    _audit(bundle, "trace_built", "trace", case_id, accounts=len(bundle.trace.accounts),
           edges=len(bundle.trace.edges), trace_sha256=sha256_hex(bundle.trace.model_dump(mode="json")))

    t = time.perf_counter()
    bundle.classifications = classify_trace(ledger, bundle.trace, cfg.classify, model=scorer)
    timings["classify_s"] = time.perf_counter() - t

    t = time.perf_counter()
    bundle.plan = build_hold_plan(bundle.trace, bundle.classifications, ledger, cfg)
    bundle.case.status = CaseStatus.PLANNED
    bundle.case.priority_amount = bundle.plan.total_recoverable
    plan_report = verify_plan(bundle, ledger, cfg=cfg)
    bundle.verifications[plan_report.draft_id] = plan_report
    timings["plan_s"] = time.perf_counter() - t
    timings["time_to_hold_plan_s"] = time.perf_counter() - t0
    _audit(bundle, "hold_plan_built", "plan", case_id, total_hold=bundle.plan.total_hold,
           total_recoverable=bundle.plan.total_recoverable, passed=plan_report.passed,
           plan_sha256=sha256_hex(bundle.plan.model_dump(mode="json")))

    if lea_request is not None:
        check_lea(bundle, lea_request, ledger, guard=guard)

    if draft:
        t = time.perf_counter()
        bundle.drafts = draft_case(bundle, ledger, llm, search)
        bundle.case.status = CaseStatus.DRAFTED
        timings["draft_s"] = time.perf_counter() - t
        for d in bundle.drafts:
            _draft_created(bundle, d)

        t = time.perf_counter()
        for d in bundle.drafts:
            report = verify_draft(d, bundle, ledger, search=search, guard=guard, cfg=cfg)
            bundle.verifications[d.draft_id] = report
            _audit(bundle, "draft_verified", "draft", d.draft_id, passed=report.passed,
                   draft_sha256=draft_fingerprint(d))
        if all(r.passed for r in bundle.verifications.values()):
            bundle.case.status = CaseStatus.VERIFIED
        timings["verify_s"] = time.perf_counter() - t

    bundle.clocks = clocks_for_bundle(bundle, utcnow())
    timings["total_s"] = time.perf_counter() - t0
    _audit(bundle, "case_processed", "case", case_id, timings={k: round(v, 4) for k, v in timings.items()},
           llm=getattr(llm, "name", "unknown"))
    if store is not None:
        store.put_bundle(bundle)
    return bundle


_ACTION_FOR_DRAFT = {
    DraftKind.HOLD_INSTRUCTION: ActionKind.HOLD_INSTRUCTION,
    DraftKind.CFCFRMS_UPDATE: ActionKind.CFCFRMS_UPDATE,
    DraftKind.OUTBOUND_REQUEST: ActionKind.CFCFRMS_UPDATE,
    DraftKind.STR: ActionKind.STR_EXPORT,
}


def draft_fingerprint(draft: Draft) -> str:
    """SHA-256 over everything a supervisor approves: kind, subject, title, body, slots, structured data, citations."""
    payload = {
        "kind": draft.kind.value,
        "account_id": draft.account_id,
        "title": draft.title,
        "body": draft.body,
        "slots": draft.slots,
        "structured": draft.structured,
        "citation_ids": draft.citation_ids,
        "version": draft.version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _latest_approval(bundle: CaseBundle, draft_id: str) -> Approval | None:
    return next((a for a in reversed(bundle.approvals) if a.draft_id == draft_id), None)


def _verified_fingerprint(bundle: CaseBundle, draft_id: str) -> str | None:
    ev = next(
        (e for e in reversed(bundle.audit) if e.action == "draft_verified" and e.object_id == draft_id), None
    )
    return ev.details.get("draft_sha256") if ev and ev.details.get("passed") else None


def _refresh_case_status(bundle: CaseBundle) -> None:
    holds = [d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION]
    latest = {d.draft_id: _latest_approval(bundle, d.draft_id) for d in holds}
    if holds and all(a is not None and a.decision == ApprovalDecision.APPROVED for a in latest.values()):
        bundle.case.status = CaseStatus.APPROVED
    elif bundle.case.status == CaseStatus.APPROVED:
        passed = all(r.passed for r in bundle.verifications.values())
        bundle.case.status = CaseStatus.VERIFIED if passed else CaseStatus.DRAFTED


def approve(
    bundle: CaseBundle,
    draft_id: str,
    *,
    approver: str,
    role: str,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    reason_code: str = "",
    comment: str = "",
    sink: ActionSink | None = None,
    ledger: Ledger | None = None,
    search: DocSearch | None = None,
    guard: Guard | None = None,
) -> CaseBundle:
    """Maker-checker step.

    - Only a supervisor records decisions.
    - An approval applies to the exact draft content (SHA-256 fingerprint). The draft must have passed
      verification in that exact form: re-verified here when a ledger is given, otherwise matched against the
      fingerprint recorded when it was verified.
    - A rejected draft cannot later be approved; regenerate it (a new version) instead.
    - Approving the same content twice is a no-op (no duplicate actions).
    """
    from wapsi.domain.clocks import clocks_for_bundle

    if role.lower() != "supervisor":
        raise PermissionError("only a supervisor can approve drafts")
    draft = next((d for d in bundle.drafts if d.draft_id == draft_id), None)
    if draft is None:
        raise KeyError(draft_id)
    fingerprint = draft_fingerprint(draft)
    now = utcnow()
    previous = _latest_approval(bundle, draft_id)
    account_level = draft.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST
    if account_level and decision == ApprovalDecision.APPROVED:
        requester = str(draft.structured.get("requested_by") or "").strip()
        if not requester or requester.casefold() == approver.strip().casefold():
            raise PermissionError("maker-checker: the officer who requested the exception cannot approve it")

    if decision == ApprovalDecision.APPROVED:
        if (
            previous is not None
            and previous.decision == ApprovalDecision.APPROVED
            and previous.draft_sha256 == fingerprint
        ):
            _audit(bundle, "approval_duplicate_ignored", "draft", draft_id, actor=approver, role=role, at=now)
            return bundle
        # the rejected content stays rejected; a new version of the draft (a redraft after an approved exception
        # keeps the id and raises ``version``, so its fingerprint differs) gets its own decision
        if (
            previous is not None
            and previous.decision == ApprovalDecision.REJECTED
            and (not previous.draft_sha256 or previous.draft_sha256 == fingerprint)
        ):
            raise ValueError(f"draft {draft_id} was rejected; regenerate it before approving")
        if account_level:
            # checked before anything is recorded: applying the exception redrafts and re-verifies the account's
            # drafts (needs the ledger), and it can only be applied on an amount-specific hold (e.g. not a second
            # request for an account whose exception is already applied)
            from wapsi.graph.holdplan import ACCOUNT_LEVEL_BASE

            if ledger is None:
                raise ValueError(f"approving {draft_id} needs the ledger: the account's hold instruction is "
                                 "redrafted and its drafts re-verified against the changed plan")
            dec = next((d for d in bundle.plan.decisions if d.account_id == draft.account_id), None) \
                if bundle.plan is not None else None
            if dec is None or dec.disposition not in ACCOUNT_LEVEL_BASE:
                state = dec.disposition.value if dec is not None else "no plan decision"
                raise ValueError(f"{draft.account_id}: no amount-specific hold to apply the exception to ({state})")
        if ledger is not None:
            from wapsi.verify import verify_draft

            report = verify_draft(draft, bundle, ledger, search=search, guard=guard)
            bundle.verifications[draft_id] = report
            _audit(bundle, "draft_verified", "draft", draft_id, actor=approver, role=role, at=now,
                   passed=report.passed, draft_sha256=fingerprint)
            if not report.passed:
                raise ValueError(f"draft {draft_id} fails verification in its current form")
        else:
            report = bundle.verifications.get(draft_id)
            if report is None or not report.passed:
                raise ValueError(f"draft {draft_id} has not passed verification")
            if _verified_fingerprint(bundle, draft_id) != fingerprint:
                raise ValueError(f"draft {draft_id} changed after it was verified; verify it again")

    approval = Approval(
        approval_id=f"APR-{uuid.uuid4().hex[:10]}", case_id=bundle.case.case_id, draft_id=draft_id,
        decision=decision, approver=approver, role=role, reason_code=reason_code, comment=comment, at=now,
        draft_sha256=fingerprint,
    )
    bundle.approvals.append(approval)
    # the approved content's fingerprint is sealed into the hash chain with the decision
    _audit(bundle, f"draft_{decision.value.lower()}", "draft", draft_id, actor=approver, role=role, at=now,
           reason_code=reason_code, draft_sha256=fingerprint, approval_id=approval.approval_id)

    if decision == ApprovalDecision.APPROVED:
        kind = _ACTION_FOR_DRAFT.get(draft.kind)
        actions = []
        if kind is not None:
            actions.append(Action(
                action_id=f"ACT-{uuid.uuid4().hex[:10]}", case_id=bundle.case.case_id, kind=kind,
                payload={"draft_id": draft_id, "account_id": draft.account_id, "title": draft.title,
                         "draft_sha256": fingerprint,
                         # Wapsi is not connected to the CFCFRMS portal: these actions are recorded, not sent
                         **({"interface": SIMULATED_CFCFRMS} if kind == ActionKind.CFCFRMS_UPDATE else {})},
                created_at=now,
            ))
        if draft.kind == DraftKind.HOLD_INSTRUCTION:
            actions.append(Action(
                action_id=f"ACT-{uuid.uuid4().hex[:10]}", case_id=bundle.case.case_id, kind=ActionKind.JIRA_TICKET,
                payload={"summary": f"Hold placed: {draft.account_id} ({bundle.case.case_id})",
                         "draft_id": draft_id, "account_id": draft.account_id},
                created_at=now,
            ))
        for a in actions:
            done = sink.submit(a) if sink is not None else a
            bundle.actions.append(done)
            _audit(bundle, "action_" + done.status.value.lower(), "action", done.action_id, actor=approver,
                   role=role, at=now, kind=done.kind.value, draft_id=draft_id)
        if account_level:
            _apply_account_level(bundle, draft, approver=approver, role=role, at=now, ledger=ledger, search=search,
                                 guard=guard)
    _refresh_case_status(bundle)
    bundle.clocks = clocks_for_bundle(bundle, now)
    return bundle


def _apply_account_level(bundle: CaseBundle, request: Draft, *, approver: str, role: str, at: datetime,
                         ledger: Ledger | None, search: DocSearch | None, guard: Guard | None) -> None:
    """A supervisor approved an officer's request: apply the exception to the plan and re-check it."""
    from wapsi.graph.holdplan import apply_account_level_hold
    from wapsi.verify import verify_draft, verify_plan

    if bundle.plan is None or request.account_id is None:
        raise ValueError("the case has no hold plan to apply the exception to")
    requester = str(request.structured.get("requested_by") or "")
    reasons = list(request.structured.get("reasons") or [])
    bundle.plan = apply_account_level_hold(bundle.plan, request.account_id, reasons, requested_by=requester,
                                           approved_by=approver)
    plan_report = verify_plan(bundle, ledger)
    bundle.verifications[plan_report.draft_id] = plan_report
    _audit(bundle, "account_level_hold_applied", "account", request.account_id, actor=approver, role=role, at=at,
           draft_id=request.draft_id, reasons=reasons, requested_by=requester, plan_passed=plan_report.passed)
    if ledger is None:
        return
    # the hold instruction written for the amount-specific decision is redrafted (new version, fresh approval), and
    # so is the case-level CFCFRMS update (its rows carry every account's disposition); the account's other drafts
    # are re-checked against the changed plan
    from wapsi.draft.drafter import redraft_cfcfrms_update, redraft_hold_instruction

    redrafted = [redraft_hold_instruction(bundle, ledger, request.account_id, search=search)]
    if any(d.kind == DraftKind.CFCFRMS_UPDATE for d in bundle.drafts):
        redrafted.append(redraft_cfcfrms_update(bundle, ledger, search=search))
    for new in redrafted:
        bundle.drafts = [new if d.draft_id == new.draft_id else d for d in bundle.drafts]
        if all(d.draft_id != new.draft_id for d in bundle.drafts):
            bundle.drafts.append(new)
        _draft_created(bundle, new, at=at, reason="EXCEPTIONAL_ACCOUNT_LEVEL_HOLD")
    # unapproved drafts written for the amount-only decision are withdrawn: the customer notice ("only this amount
    # is on hold"; the officer informs the holder) and an innocent holder's EDD memo (it recommends no account-level
    # action, and the verifier accepts an EDD memo only for HOLD_AMOUNT_NO_ESCALATION)
    stale = [d for d in bundle.drafts if d.kind in (DraftKind.CUSTOMER_NOTICE, DraftKind.EDD_MEMO)
             and d.account_id == request.account_id and _latest_approval(bundle, d.draft_id) is None]
    for d in stale:
        bundle.drafts.remove(d)
        bundle.verifications.pop(d.draft_id, None)
        _audit(bundle, "draft_withdrawn", "draft", d.draft_id, actor=approver, role=role, at=at,
               reason="EXCEPTIONAL_ACCOUNT_LEVEL_HOLD")
    rechecked = {new.draft_id for new in redrafted}
    for d in bundle.drafts:
        if (d.account_id == request.account_id or d.draft_id in rechecked) and d.draft_id != request.draft_id:
            report = verify_draft(d, bundle, ledger, search=search, guard=guard)
            bundle.verifications[d.draft_id] = report
            _audit(bundle, "draft_verified", "draft", d.draft_id, actor=approver, role=role, at=at,
                   passed=report.passed, draft_sha256=draft_fingerprint(d))


def request_account_level_hold(
    bundle: CaseBundle,
    ledger: Ledger,
    account_id: str,
    reasons: Iterable[str],
    *,
    requested_by: str,
    role: str,
    note: str = "",
    search: DocSearch | None = None,
    guard: Guard | None = None,
) -> Draft:
    """An officer (maker) requests an exceptional account-level hold on ``account_id``.

    Amount-specific holds are the default; account-level action is only a recorded exception. The request needs
    >= 1 reason from ``wapsi.models.AccountLevelReason`` and an eligible account; it is drafted, verified and added
    to the bundle, and nothing changes in the plan until a supervisor other than ``requested_by`` approves it with
    ``approve`` (fingerprint-bound). Raises ``PermissionError`` for the auditor role and ``ValueError`` for a missing
    / unknown reason or an ineligible account.
    """
    from wapsi.draft.drafter import draft_account_level_request
    from wapsi.verify import verify_draft

    if role.strip().lower() == "auditor":
        raise PermissionError("auditors are read-only")
    draft = draft_account_level_request(bundle, ledger, account_id, list(reasons), requested_by=requested_by,
                                        requester_role=role, note=note, search=search)
    report = verify_draft(draft, bundle, ledger, search=search, guard=guard)
    bundle.drafts.append(draft)
    bundle.verifications[draft.draft_id] = report
    now = utcnow()
    _audit(bundle, "account_level_hold_requested", "account", account_id, actor=requested_by, role=role, at=now,
           draft_id=draft.draft_id, reasons=list(draft.structured.get("reasons", [])))
    _draft_created(bundle, draft, actor=requested_by, role=role, at=now)
    _audit(bundle, "draft_verified", "draft", draft.draft_id, actor=requested_by, role=role, at=now,
           passed=report.passed, draft_sha256=draft_fingerprint(draft))
    return draft


def check_lea(bundle: CaseBundle, request, ledger: Ledger | None = None, *, guard: Guard | None = None,
              now: datetime | None = None, actor: str = SYSTEM_ACTOR, role: str = "system"):
    """Check a police / LEA hold request against the case (``wapsi.verify.lea_request``) and record the result as
    a ``lea_request_checked`` audit event. Returns the ``LeaRequestCheck``; the plan is never changed."""
    from wapsi.verify.lea_request import check_lea_request, summary

    at = now or utcnow()
    result = check_lea_request(request, bundle, ledger, now=at, guard=guard)
    _audit(bundle, "lea_request_checked", "lea_request", request.request_id, actor=actor, role=role, at=at,
           **summary(result), letter=result.letter)
    return result


def pending_holds(bundle: CaseBundle) -> list[str]:
    """Accounts with a holding disposition whose hold instruction has not been executed yet."""
    if bundle.plan is None:
        return []
    executed = {a.payload.get("account_id") for a in bundle.actions
                if a.kind == ActionKind.HOLD_INSTRUCTION and a.status == ActionStatus.DONE}
    return [d.account_id for d in bundle.plan.decisions
            if (d.disposition in HOLDING_DISPOSITIONS or d.disposition == Disposition.FORWARD_HOLD)
            and d.account_id not in executed]
