"""Stored-procedure handlers (registered by ``editions/snowflake/sql/30_procs.sql``).

Each handler takes the procedure's Snowpark session plus its ``P_`` arguments and returns a JSON-able dict
(``RETURNS VARIANT``). They are thin wrappers: Snowflake I/O here, all fraud logic in the portable core —

    TRACE_CASE(P_NCRP_ID)         wapsi.pipeline.run.group_complaints + wapsi.graph.trace.trace_case
    BUILD_HOLD_PLAN(P_CASE_ID)    wapsi.classify.classify_trace + wapsi.graph.holdplan.build_hold_plan
                                  + wapsi.verify.verify_plan
    DRAFT(P_CASE_ID, P_KIND)      wapsi.draft.drafter.draft_case (Cortex AI_COMPLETE writes prose only; every
                                  number is a slot) + wapsi.verify.verify_draft, up to MAX_REDRAFTS retries
    VERIFY(P_DRAFT_ID)            wapsi.verify.verify_draft on the stored draft
    CASE_SUMMARY(P_CASE_ID)       compact status (<= 2 KB) for the agent, incl. the audit-chain check
    APPROVE(...)                  wapsi.pipeline.run.approve (maker-checker; caller's rights)
    REQUEST_ACCOUNT_LEVEL_HOLD    wapsi.pipeline.run.request_account_level_hold (officer = maker; >= 1 recorded
                                  reason; nothing changes in the plan until a different supervisor approves)
    APPROVE_ACCOUNT_LEVEL(...)    the ledger step of APPROVE for an ACCOUNT_LEVEL_HOLD_REQUEST: wapsi.pipeline.run
                                  .approve with the ledger (applies the exception, redrafts the hold instruction
                                  and CFCFRMS update for a fresh approval); called by APPROVE, which establishes
                                  the approver with the caller's own roles
    CHECK_LEA(P_CASE_ID, P_REQUEST)   wapsi.pipeline.run.check_lea: police / LEA hold request vs the 8-field
                                  checklist; hold limited to the identifiable amount; never changes the plan
    APPEND_AUDIT(...)             one sealed audit event (the actions runner's only audit write path)

Every audit event is sealed into the case's hash chain (``wapsi.audit``) and stored INSERT-only by
``SnowflakeCaseStore``. User errors (unknown id, missing prerequisite) come back as ``{"ok": false, "error": ...}``
so the Cortex Agent can explain them; unexpected failures raise and show up in the query history.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from typing import Any

from wapsi.adapters.snowflake_ledger import SnowflakeLedger, row_dict, snapshot_ledger
from wapsi.audit import append_event
from wapsi.config import WapsiConfig
from wapsi.models import (
    AccountLevelReason,
    ActionKind,
    ActionStatus,
    ApprovalDecision,
    AuditEvent,
    Case,
    CaseBundle,
    CaseStatus,
    Disposition,
    DraftKind,
)
from wapsi.timeutil import fmt_ist, utcnow
from wapsi.verify.report import code_of, label_of, text_of

from .casestore import SnowflakeCaseStore, draft_makers

SHOWCASE_SCHEMA = "RAW_SHOWCASE"
DEFAULT_SCHEMA = "RAW"
SCHEMAS = (SHOWCASE_SCHEMA, DEFAULT_SCHEMA)  # world resolution order for an NCRP id
MAX_REDRAFTS = 2
SUMMARY_MAX_CHARS = 2000
DEFAULT_SETTINGS = {
    "llm_model": "claude-sonnet-5",
    "llm_mode": "ai_complete",
    "guardrails": "false",
    "ledger_mode": "sql",
    "default_officer": "nodal.officer@hb01",
    "search_service": "WAPSI.DOCS.SOP_SEARCH",
}
_NCRP = re.compile(r"^\d{6,20}$")
_CASE = re.compile(r"^CASE-\d{6,20}$")
_DRAFT = re.compile(r"^[A-Za-z0-9_.:\-]{3,200}$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,60}$")
_EVENT = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
ACTOR = "wapsi"
LIEN_AMOUNT = "AMOUNT"
LIEN_EXCEPTION = "ACCOUNT_LEVEL_EXCEPTION"  # the approved exception's restriction (amount 0: the lien is the money)


# --------------------------------------------------------------------------- helpers


def error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def settings(session: Any) -> dict[str, str]:
    out = dict(DEFAULT_SETTINGS)
    try:
        for r in map(row_dict, session.sql("SELECT name, value FROM WAPSI.APP.SETTINGS").collect()):
            if r.get("name") and r.get("value") is not None:
                out[str(r["name"])] = str(r["value"])
    except Exception:  # noqa: BLE001 - settings are optional
        pass
    return out


def current_user(session: Any) -> str:
    try:
        rows = session.sql("SELECT CURRENT_USER() AS U").collect()
        return str(list(row_dict(rows[0]).values())[0])
    except Exception:  # noqa: BLE001
        return "unknown"


def resolve_schema(session: Any, ncrp_id: str) -> str | None:
    """RAW_SHOWCASE first (the hand-checked world), then RAW; None if no schema has the complaint."""
    for schema in SCHEMAS:
        try:
            rows = session.sql(f"SELECT COUNT(*) AS N FROM WAPSI.{schema}.COMPLAINTS WHERE ncrp_id = ?",
                               params=[ncrp_id]).collect()
        except Exception:  # noqa: BLE001 - schema not loaded
            continue
        if rows and int(list(row_dict(rows[0]).values())[0] or 0) > 0:
            return schema
    return None


def make_ledger(session: Any, schema: str, cfg: dict[str, str]) -> Any:
    if cfg.get("ledger_mode") == "snapshot":
        return snapshot_ledger(session, tempfile.mkdtemp(prefix="wapsi_world_"), schema)
    return SnowflakeLedger(session, schema)


def world_of(schema: str, ledger: Any) -> str:
    if schema == SHOWCASE_SCHEMA:
        return "showcase"
    try:
        return ledger.meta.get("world", "demo")
    except Exception:  # noqa: BLE001
        return "demo"


def branch_cities(ledger: Any, account_ids: Any) -> dict[str, str]:
    """account_id -> holder city (the 'branch' proxy used by the semantic view); {} if unavailable."""
    try:
        accts = ledger.accounts(list(account_ids))
        cust = ledger.customers({a.customer_id for a in accts.values() if a.customer_id})
    except Exception:  # noqa: BLE001
        return {}
    return {aid: cust[a.customer_id].city for aid, a in accts.items()
            if a.customer_id in cust and cust[a.customer_id].city}


def audit(bundle: CaseBundle, action: str, object_type: str, object_id: str, *, actor: str = ACTOR,
          role: str = "system", **details: Any) -> None:
    """Seal an event onto the case's hash-chained audit log (``wapsi.audit.append_event``)."""
    event = AuditEvent(at=utcnow(), actor=actor, role=role, action=action, object_type=object_type,
                       object_id=object_id, details=json.loads(json.dumps(details, default=str)))
    append_event(bundle.audit, event, case_id=bundle.case.case_id)


def persona(session: Any, p_actor: str | None) -> tuple[str, str]:
    """(actor, role) of the person acting: the app persona ``P_ACTOR`` when it is mapped in APP.USER_ROLES (the
    Streamlit app runs with owner's rights and passes the viewer's persona), else CURRENT_USER() as an analyst.
    Raises LookupError for an unmapped persona."""
    actor = str(p_actor or "").strip()
    if not actor:
        return current_user(session), "analyst"
    rows = session.sql("SELECT app_role FROM WAPSI.APP.USER_ROLES WHERE LOWER(user_name) = LOWER(?)",
                       params=[actor]).collect()
    roles = {str(row_dict(r)["app_role"]).upper() for r in rows}
    for role in ("SUPERVISOR", "ANALYST", "AUDITOR"):
        if role in roles:
            return actor, role.lower()
    raise LookupError(f"{actor} is not mapped in WAPSI.APP.USER_ROLES")


def call_proc(session: Any, proc: str, *args: Any) -> dict[str, Any]:
    """CALL WAPSI.CASEMGMT.<proc>(?, ...) with binds; its VARIANT result as a dict."""
    marks = ", ".join("?" for _ in args)
    rows = session.sql(f"CALL WAPSI.CASEMGMT.{proc}({marks})", params=list(args)).collect()
    raw = next(iter(row_dict(rows[0]).values())) if rows else None
    data = json.loads(raw) if isinstance(raw, str) else raw
    return data if isinstance(data, dict) else error(f"{proc} returned no result")


def parse_list(value: Any) -> list[str]:
    """'A, B' / 'A B' / '["A", "B"]' / ["A", "B"] -> ['A', 'B'] (upper-case, order kept, no duplicates)."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = text.strip("[]")
        if isinstance(value, str):
            value = re.split(r"[\s,;]+", value)
    items = [str(v).strip().upper() for v in (value or []) if str(v).strip()]
    return list(dict.fromkeys(items))


def _jsonable(model: Any) -> Any:
    return json.loads(model.model_dump_json())


def _load(session: Any, case_id: str) -> tuple[SnowflakeCaseStore, CaseBundle | None, dict[str, Any] | None]:
    store = SnowflakeCaseStore(session)
    return store, store.get_bundle(case_id), store.case_info(case_id)


def _ledger_for_case(session: Any, info: dict[str, Any] | None, cfg: dict[str, str]) -> Any:
    return make_ledger(session, (info or {}).get("raw_schema") or DEFAULT_SCHEMA, cfg)


# --------------------------------------------------------------------------- TRACE_CASE


def trace_case_proc(session: Any, p_ncrp_id: str) -> dict[str, Any]:
    from wapsi.domain.clocks import clocks_for_bundle
    from wapsi.graph.trace import trace_case
    from wapsi.pipeline.run import group_complaints
    from wapsi.verify.injection import RulesGuard

    ncrp_id = str(p_ncrp_id or "").strip()
    if not _NCRP.match(ncrp_id):
        return error(f"not an NCRP acknowledgement number: {ncrp_id!r}")
    t0 = time.perf_counter()
    cfg = settings(session)
    schema = resolve_schema(session, ncrp_id)
    if schema is None:
        return error(f"NCRP {ncrp_id} is not in WAPSI.RAW_SHOWCASE or WAPSI.RAW")
    ledger = make_ledger(session, schema, cfg)
    if isinstance(ledger, SnowflakeLedger):
        ledger.prefetch_complaints()
    group = next(g for g in group_complaints(ledger, ledger.complaints()) if ncrp_id in g)
    case_id = f"CASE-{group[0]}"
    wcfg = WapsiConfig()
    store = SnowflakeCaseStore(session)
    prev = store.get_bundle(case_id)
    now = utcnow()
    bundle = CaseBundle(
        case=Case(case_id=case_id, ncrp_ids=group, opened_at=prev.case.opened_at if prev else now,
                  status=CaseStatus.TRACED,
                  assigned_to=(prev.case.assigned_to if prev else None) or cfg["default_officer"]),
        complaints=[ledger.complaint(n) for n in group],
        approvals=list(prev.approvals) if prev else [],
        actions=list(prev.actions) if prev else [],
        audit=list(prev.audit) if prev else [],
    )
    if prev is None:
        audit(bundle, "case_opened", "case", case_id, ncrp_ids=group, raw_schema=schema)
    guard = RulesGuard()
    for c in bundle.complaints:  # untrusted input: record, quote, never act on it
        g = guard.screen(c.narrative_text)
        if g.flagged:
            audit(bundle, "untrusted_instruction_detected", "complaint", c.ncrp_id,
                  categories=g.categories, quoted_spans=g.quoted_spans)
    bundle.trace = trace_case(ledger, group, wcfg.trace, case_id=case_id)
    bundle.clocks = clocks_for_bundle(bundle, now)
    branch = branch_cities(ledger, bundle.trace.accounts)
    audit(bundle, "case_traced", "case", case_id, totals=_jsonable(bundle.trace.totals),
          n_accounts=len(bundle.trace.accounts), n_edges=len(bundle.trace.edges),
          ledger_queries=len(getattr(ledger, "queries", [])), seconds=round(time.perf_counter() - t0, 3))
    world = world_of(schema, ledger)
    store.save(bundle, world=world, raw_schema=schema, branch=branch)
    return {
        "ok": True, "case_id": case_id, "ncrp_ids": group, "world": world, "raw_schema": schema,
        "rule": bundle.trace.rule.value, "apportionment": "pro-rata, SOP Para 5(xiii)"
        if bundle.trace.rule.value == "PRO_RATA" else bundle.trace.rule.value,
        "totals": _jsonable(bundle.trace.totals), "warnings": bundle.trace.warnings,
        "n_accounts": len(bundle.trace.accounts), "n_edges": len(bundle.trace.edges),
        "next": f"CALL WAPSI.CASEMGMT.BUILD_HOLD_PLAN('{case_id}')",
    }


# --------------------------------------------------------------------------- BUILD_HOLD_PLAN


def build_hold_plan_proc(session: Any, p_case_id: str) -> dict[str, Any]:
    from wapsi.classify import classify_trace
    from wapsi.domain.clocks import clocks_for_bundle
    from wapsi.graph.holdplan import build_hold_plan
    from wapsi.verify import PLAN_DRAFT_ID, verify_plan

    case_id = str(p_case_id or "").strip()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r} (expected CASE-<ncrp id>)")
    cfg = settings(session)
    store, bundle, info = _load(session, case_id)
    if bundle is None or bundle.trace is None:
        return error(f"{case_id} has not been traced; call TRACE_CASE with one of its NCRP ids first")
    ledger = _ledger_for_case(session, info, cfg)
    wcfg = WapsiConfig()
    bundle.classifications = classify_trace(ledger, bundle.trace, wcfg.classify)
    bundle.plan = build_hold_plan(bundle.trace, bundle.classifications, ledger, wcfg)
    bundle.drafts = []  # a new plan invalidates earlier drafts; approvals and audit stay
    bundle.verifications = {}
    report = verify_plan(bundle, ledger, cfg=wcfg)
    bundle.verifications[PLAN_DRAFT_ID] = report
    bundle.case.status = CaseStatus.PLANNED
    bundle.case.priority_amount = bundle.plan.total_recoverable
    bundle.clocks = clocks_for_bundle(bundle, utcnow())
    p = bundle.plan
    audit(bundle, "plan_built", "case", case_id, total_hold=p.total_hold, total_recoverable=p.total_recoverable,
          over_hold_amount=p.over_hold_amount, plan_check_passed=report.passed)
    branch = branch_cities(ledger, bundle.trace.accounts)
    store.save(bundle, branch=branch)
    return {
        "ok": True, "case_id": case_id, "status": bundle.case.status.value,
        "total_hold": p.total_hold, "total_forward_hold": p.total_forward_hold,
        "total_recoverable": p.total_recoverable, "over_hold_amount": p.over_hold_amount,
        "baseline_freeze_amount": p.baseline_freeze_amount, "baseline_freeze_accounts": p.baseline_freeze_accounts,
        "restoration_process": p.restoration_process.value, "str_ground": p.str_ground.value if p.str_ground else None,
        "per_victim_recoverable": p.per_victim_recoverable,
        "plan_check": {"passed": report.passed,
                       "failed": [f"{c.name}: {label_of(c)}" for c in report.checks if not c.passed]},
        "decisions": [
            {"account_id": d.account_id, "layer": d.layer, "role": d.role.value, "disposition": d.disposition.value,
             "hold_amount": d.hold_amount, "forward_hold_amount": d.forward_hold_amount,
             "tainted_amount": d.tainted_amount, "requires_human": d.requires_human,
             "reason_codes": d.reason_codes, "citation_ids": d.citation_ids}
            for d in p.decisions
        ],
        "next": f"CALL WAPSI.CASEMGMT.DRAFT('{case_id}', 'ALL')",
    }


# --------------------------------------------------------------------------- DRAFT / VERIFY


def _llm(session: Any, cfg: dict[str, str]) -> Any:
    from wapsi.adapters.cortex_llm import CortexLLM

    return CortexLLM(session, cfg["llm_model"], mode=cfg.get("llm_mode", "ai_complete"),
                     guardrails=cfg.get("guardrails", "false").lower() == "true")


def _search(session: Any, cfg: dict[str, str]) -> Any:
    from .docsearch import CortexDocSearch

    return CortexDocSearch(session, service=cfg.get("search_service", DEFAULT_SETTINGS["search_service"]))


def _report_summary(report: Any) -> dict[str, Any]:
    return {"passed": report.passed,
            "checks": [{"name": c.name, "passed": c.passed, "severity": c.severity, "code": code_of(c),
                        "message": text_of(c)[:300], "spans": c.spans[:3]} for c in report.checks]}


def draft_proc(session: Any, p_case_id: str, p_kind: str = "ALL", *, llm: Any = None, search: Any = None,
               actor: str | None = None) -> dict[str, Any]:
    from wapsi.domain.clocks import clocks_for_bundle
    from wapsi.draft.drafter import draft_case
    from wapsi.pipeline.run import draft_fingerprint
    from wapsi.verify import verify_draft
    from wapsi.verify.injection import RulesGuard

    case_id = str(p_case_id or "").strip()
    kind_arg = str(p_kind or "ALL").strip().upper()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r}")
    if kind_arg == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST.value:
        return error("an account-level hold is only an officer's recorded exception: use "
                     "REQUEST_ACCOUNT_LEVEL_HOLD (a different supervisor approves it)")
    if kind_arg != "ALL" and kind_arg not in DraftKind.__members__:
        return error(f"unknown draft kind {kind_arg!r}; use ALL or one of {sorted(DraftKind.__members__)}")
    cfg = settings(session)
    store, bundle, info = _load(session, case_id)
    if bundle is None or bundle.plan is None:
        return error(f"{case_id} has no hold plan; call BUILD_HOLD_PLAN first")
    ledger = _ledger_for_case(session, info, cfg)
    llm = llm or _llm(session, cfg)
    search = search or _search(session, cfg)
    guard = RulesGuard()
    maker = actor or current_user(session)
    wanted = (lambda d: True) if kind_arg == "ALL" else (lambda d: d.kind.value == kind_arg)  # noqa: E731
    previous = {d.draft_id: d for d in bundle.drafts}

    accepted: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    attempts = 0
    pending: set[str] | None = None
    while attempts <= MAX_REDRAFTS:
        attempts += 1
        fresh = [d for d in draft_case(bundle, ledger, llm, search) if wanted(d)]
        for d in fresh:
            if pending is not None and d.draft_id not in pending:
                continue
            if d.draft_id in previous:
                d.version = previous[d.draft_id].version + 1
            rep = verify_draft(d, bundle, ledger, search=search, guard=guard)
            if rep.passed or d.draft_id not in accepted:
                accepted[d.draft_id], reports[d.draft_id] = d, rep
        pending = {i for i, r in reports.items() if not r.passed}
        if not pending:
            break

    # an officer's account-level request is not a generated draft: it is kept whatever is redrafted
    kept = [d for d in bundle.drafts if d.draft_id not in accepted
            and (d.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST or not (kind_arg == "ALL" or wanted(d)))]
    bundle.drafts = kept + list(accepted.values())
    bundle.verifications = {k: v for k, v in bundle.verifications.items()
                            if k not in accepted and k in {d.draft_id for d in kept} | {"PLAN"}}
    for d in accepted.values():
        rep = reports[d.draft_id]
        bundle.verifications[d.draft_id] = rep
        audit(bundle, "draft_created", "draft", d.draft_id, actor=maker, role="analyst", kind=d.kind.value,
              version=d.version, account_id=d.account_id, llm_model=d.llm_model, draft_sha256=draft_fingerprint(d))
        audit(bundle, "draft_verified", "draft", d.draft_id, passed=rep.passed, draft_sha256=draft_fingerprint(d))
        if not rep.passed:
            audit(bundle, "draft_needs_human", "draft", d.draft_id,
                  failed=[c.name for c in rep.checks if not c.passed], attempts=attempts)
    usage = llm.usage_summary() if hasattr(llm, "usage_summary") else {"model": getattr(llm, "name", "llm")}
    audit(bundle, "llm_usage", "case", case_id, **usage)
    all_ok = all(r.passed for r in bundle.verifications.values())
    bundle.case.status = CaseStatus.VERIFIED if all_ok else CaseStatus.DRAFTED
    bundle.clocks = clocks_for_bundle(bundle, utcnow())
    store.save(bundle, parts=("drafts",))
    return {
        "ok": True, "case_id": case_id, "status": bundle.case.status.value, "llm": getattr(llm, "name", "llm"),
        "attempts": attempts, "usage": usage, "draft_ids": list(accepted),
        "drafts": [{"draft_id": d.draft_id, "kind": d.kind.value, "account_id": d.account_id,
                    "passed": reports[d.draft_id].passed,
                    "failed_checks": [c.name for c in reports[d.draft_id].checks if not c.passed],
                    "llm_fallback": bool(d.structured.get("llm_fallback", False))}
                   for d in accepted.values()],
        "next": "Show each draft's verification report before its text; never show a draft that failed.",
    }


def verify_proc(session: Any, p_draft_id: str, *, search: Any = None) -> dict[str, Any]:
    from wapsi.pipeline.run import draft_fingerprint
    from wapsi.verify import verify_draft
    from wapsi.verify.injection import RulesGuard

    draft_id = str(p_draft_id or "").strip()
    if not _DRAFT.match(draft_id):
        return error(f"not a draft id: {draft_id!r}")
    cfg = settings(session)
    store = SnowflakeCaseStore(session)
    case_id = store.case_for_draft(draft_id)
    if case_id is None:
        return error(f"draft {draft_id} not found; call DRAFT first")
    bundle, info = store.get_bundle(case_id), store.case_info(case_id)
    draft = next((d for d in (bundle.drafts if bundle else []) if d.draft_id == draft_id), None)
    if bundle is None or draft is None:
        return error(f"draft {draft_id} not found in case {case_id}")
    ledger = _ledger_for_case(session, info, cfg)
    search = search or _search(session, cfg)
    report = verify_draft(draft, bundle, ledger, search=search, guard=RulesGuard())
    bundle.verifications[draft_id] = report
    audit(bundle, "draft_verified", "draft", draft_id, passed=report.passed, draft_sha256=draft_fingerprint(draft))
    store.save(bundle, parts=("drafts",))
    return {"ok": True, "draft_id": draft_id, "case_id": case_id, "kind": draft.kind.value,
            **_report_summary(report)}


# --------------------------------------------------------------------------- CASE_SUMMARY


def case_summary_proc(session: Any, p_case_id: str) -> dict[str, Any]:
    case_id = str(p_case_id or "").strip()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r}")
    store = SnowflakeCaseStore(session)
    b = store.get_bundle(case_id)
    if b is None:
        return error(f"{case_id} not found; call TRACE_CASE with one of its NCRP ids")
    out: dict[str, Any] = {"ok": True, "case_id": case_id, "status": b.case.status.value, "ncrp_ids": b.case.ncrp_ids,
                           "assigned_to": b.case.assigned_to}
    chain = store.verify_audit(case_id)  # AUDIT_LOG read back, checked against the latest AUDIT_ANCHOR
    out["audit_chain"] = {"status": chain.status, "events": chain.length, "anchored": chain.anchored}
    if not chain.ok:
        out["audit_chain"].update(code=chain.code, first_bad_seq=chain.first_bad_seq)
    if b.trace:
        out["trace_totals"] = _jsonable(b.trace.totals)
    if b.plan:
        p = b.plan
        out["plan"] = {"total_hold": p.total_hold, "total_forward_hold": p.total_forward_hold,
                       "total_recoverable": p.total_recoverable, "over_hold_amount": p.over_hold_amount,
                       "baseline_freeze_amount": p.baseline_freeze_amount,
                       "restoration_process": p.restoration_process.value}
        out["holds"] = [[d.account_id, d.disposition.value, d.hold_amount] for d in p.decisions if d.hold_amount > 0]
        conduits = [d.account_id for d in p.decisions if "STR_RECOMMENDED_OFFICER_DECISION" in d.reason_codes]
        if conduits:  # unwitting conduits: STR recommended, the officer decides (never automatic)
            out["str_recommended_officer_decision"] = conduits
    requests = [d for d in b.drafts if d.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST]
    exceptions = [d.account_id for d in (b.plan.decisions if b.plan else [])
                  if d.disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD]
    if requests or exceptions:
        decided = {a.draft_id for a in b.approvals}
        out["account_level"] = {"applied": exceptions,
                                "pending_requests": sum(1 for d in requests if d.draft_id not in decided)}
    failed = [d.draft_id for d in b.drafts
              if not (b.verifications.get(d.draft_id) and b.verifications[d.draft_id].passed)]
    out["drafts"] = {"total": len(b.drafts), "passed": len(b.drafts) - len(failed), "failed_ids": failed[:5],
                     "kinds": sorted({d.kind.value for d in b.drafts})}
    open_clocks = sorted((c for c in b.clocks if c.status.value != "DONE"), key=lambda c: c.due)
    out["open_clocks"] = [[c.kind.value, c.subject or "", fmt_ist(c.due, with_time=False), c.status.value]
                          for c in open_clocks[:6]]
    out["approvals"] = len(b.approvals)
    out["untrusted_instruction_flags"] = sum(1 for e in b.audit if e.action == "untrusted_instruction_detected")
    for key in ("open_clocks", "holds"):  # keep the agent payload small
        while len(json.dumps(out, default=str)) > SUMMARY_MAX_CHARS and out.get(key):
            out[key] = out[key][:-1]
            out[f"{key}_truncated"] = True
    return out


# --------------------------------------------------------------------------- APPROVE (caller's rights)


def approve_proc(session: Any, p_draft_id: str, p_decision: str, p_reason_code: str, p_comment: str = "",
                 p_actor: str | None = None) -> dict[str, Any]:
    """Maker-checker. Allowed when the caller has WAPSI_SUPERVISOR (approver = CURRENT_USER()), or when the
    Streamlit owner role calls with P_ACTOR mapped to SUPERVISOR in APP.USER_ROLES. Checker != maker. Approving an
    ACCOUNT_LEVEL_HOLD_REQUEST is handed to APPROVE_ACCOUNT_LEVEL (the ledger step) with the approver established
    here; an approved hold instruction places the simulated lien once per account."""
    from wapsi.domain.clocks import clocks_for_bundle
    from wapsi.pipeline.run import approve

    draft_id = str(p_draft_id or "").strip()
    try:
        decision = ApprovalDecision(str(p_decision or "").strip().upper())
    except ValueError:
        return error(f"decision must be one of {[d.value for d in ApprovalDecision]}")
    rows = session.sql("SELECT IS_ROLE_IN_SESSION('WAPSI_SUPERVISOR') AS SUP, "
                       "IS_ROLE_IN_SESSION('WAPSI_APP_OWNER') AS APP, CURRENT_USER() AS U").collect()
    who = row_dict(rows[0])
    actor = (p_actor or "").strip()
    if actor and who.get("app"):
        role_rows = session.sql("SELECT app_role FROM WAPSI.APP.USER_ROLES WHERE LOWER(user_name) = LOWER(?)",
                                params=[actor]).collect()
        roles = {str(row_dict(r)["app_role"]).upper() for r in role_rows}
        if "SUPERVISOR" not in roles:
            return error(f"{actor} is not mapped to SUPERVISOR in WAPSI.APP.USER_ROLES")
        checker = actor
    elif who.get("sup"):
        checker = str(who.get("u"))
    else:
        return error("only a supervisor can approve drafts (maker-checker); the agent cannot approve")
    store = SnowflakeCaseStore(session)
    case_id = store.case_for_draft(draft_id)
    bundle = store.get_bundle(case_id) if case_id else None
    draft = next((d for d in (bundle.drafts if bundle else []) if d.draft_id == draft_id), None)
    if bundle is None or draft is None:
        return error(f"draft {draft_id} not found")
    maker = draft_makers(bundle).get(draft_id)
    if maker and maker.lower() == checker.lower():
        return error(f"maker-checker: {checker} drafted {draft_id} and cannot also approve it")
    if draft.kind == DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST and decision == ApprovalDecision.APPROVED:
        # applying the exception redrafts and re-verifies the account's drafts against the ledger (RAW), which a
        # caller's-rights procedure cannot read: the owner's-rights step applies it for the approver established
        # here from the caller's own roles
        return call_proc(session, "APPROVE_ACCOUNT_LEVEL", draft_id, str(p_reason_code or ""),
                         str(p_comment or ""), checker)
    before_actions = {a.action_id for a in bundle.actions}
    try:
        approve(bundle, draft_id, approver=checker, role="supervisor", decision=decision,
                reason_code=str(p_reason_code or ""), comment=str(p_comment or ""))
    except (PermissionError, ValueError, KeyError) as exc:
        return error(f"not approved: {exc}")
    now = utcnow()
    liens: list[dict[str, Any]] = []
    placed = active_liens(store, bundle.case.case_id)
    for a in bundle.actions:
        if a.action_id in before_actions:
            continue
        if a.kind == ActionKind.HOLD_INSTRUCTION and bundle.plan is not None:
            dec = next((d for d in bundle.plan.decisions if d.account_id == draft.account_id), None)
            amount = dec.hold_amount if dec else 0.0
            if amount <= 0:
                continue
            # simulated core banking: the amount lien is placed once per account (a redrafted, re-approved hold
            # instruction never doubles it); an approved exception adds its account-level restriction row
            have = placed.setdefault(draft.account_id, set())
            rows = []
            if LIEN_AMOUNT not in have:
                rows.append((LIEN_AMOUNT, f"LIEN-{a.action_id}", amount))
            if dec.disposition == Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD and LIEN_EXCEPTION not in have:
                rows.append((LIEN_EXCEPTION, f"LIEN-{a.action_id}-X", 0.0))
            for scope, lien_id, lien_amount in rows:
                have.add(scope)
                liens.append({"lien_id": lien_id, "case_id": bundle.case.case_id, "account_id": draft.account_id,
                              "amount": lien_amount, "placed_at": now, "released_at": None,
                              "source_action_id": a.action_id, "scope": scope})
                audit(bundle, "lien_placed", "action", a.action_id, actor=checker, role="supervisor",
                      account_id=draft.account_id, amount=lien_amount, scope=scope, simulated=True)
            a.status, a.executed_at = ActionStatus.DONE, now
            a.result = ("lien placed (simulated core banking)" if rows
                        else "lien already in place (simulated core banking)")
    bundle.clocks = clocks_for_bundle(bundle, now)
    written = store.record_approval(bundle, liens)
    new_actions = [a for a in bundle.actions if a.action_id not in before_actions]
    out = {"ok": True, "draft_id": draft_id, "case_id": bundle.case.case_id, "decision": decision.value,
           "approver": checker, "status": bundle.case.status.value,
           "actions": [[a.kind.value, a.status.value] for a in new_actions],
           "liens_total": round(sum(x["amount"] for x in liens), 2), "written": written}
    if any(a.payload.get("interface") == "SIMULATED_CFCFRMS" for a in new_actions):
        out["note"] = "Simulated CFCFRMS interface: the CFCFRMS update is recorded, not sent to the portal."
    return out


def active_liens(store: SnowflakeCaseStore, case_id: str) -> dict[str, set[str]]:
    """account_id -> scopes of its unreleased liens in the case (rows written before SCOPE existed: AMOUNT)."""
    rows = store._exec(f"SELECT account_id, scope FROM {store.t('LIENS')} WHERE case_id = ? AND released_at IS NULL",
                       [case_id])
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["account_id"], set()).add(r.get("scope") or LIEN_AMOUNT)
    return out


# --------------------------------------------------------------------------- exceptional account-level hold


def request_account_level_hold_proc(session: Any, p_case_id: str, p_account_id: str, p_reasons: Any,
                                    p_note: str = "", p_actor: str | None = None, *,
                                    search: Any = None) -> dict[str, Any]:
    """An officer (maker) requests an exceptional account-level hold. Amount-specific holds stay the default: the
    request is drafted and verified, and nothing changes in the plan until a supervisor other than the requester
    approves it with APPROVE."""
    from wapsi.pipeline.run import request_account_level_hold
    from wapsi.verify.injection import RulesGuard

    case_id = str(p_case_id or "").strip()
    account_id = str(p_account_id or "").strip()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r}")
    if not _ACCOUNT.match(account_id):
        return error(f"not an account number: {account_id!r}")
    reasons = parse_list(p_reasons)
    if not reasons:
        return error("record at least one reason: " + ", ".join(r.value for r in AccountLevelReason))
    try:
        actor, role = persona(session, p_actor)
    except LookupError as exc:
        return error(str(exc))
    cfg = settings(session)
    store, bundle, info = _load(session, case_id)
    if bundle is None or bundle.plan is None:
        return error(f"{case_id} has no hold plan; call BUILD_HOLD_PLAN first")
    ledger = _ledger_for_case(session, info, cfg)
    search = search or _search(session, cfg)
    try:
        draft = request_account_level_hold(bundle, ledger, account_id, reasons, requested_by=actor, role=role,
                                           note=str(p_note or "")[:500], search=search, guard=RulesGuard())
    except PermissionError as exc:
        return error(f"not allowed: {exc}")
    except (KeyError, ValueError) as exc:
        return error(f"not requested: {exc}")
    store.save(bundle, parts=("drafts",))
    report = bundle.verifications[draft.draft_id]
    return {"ok": True, "case_id": case_id, "draft_id": draft.draft_id, "account_id": account_id,
            "reasons": list(draft.structured.get("reasons", [])), "requested_by": actor, "passed": report.passed,
            "failed_checks": [c.name for c in report.checks if not c.passed],
            "next": f"A supervisor other than {actor} decides: CALL WAPSI.CASEMGMT.APPROVE('{draft.draft_id}', "
                    "'APPROVED' | 'REJECTED', '<reason code>', '<comment>')"}


def approve_account_level_proc(session: Any, p_draft_id: str, p_reason_code: str, p_comment: str,
                               p_approver: str, *, search: Any = None) -> dict[str, Any]:
    """The ledger step of APPROVE for an ACCOUNT_LEVEL_HOLD_REQUEST (owner's rights: reads RAW). APPROVE calls it
    after establishing the approver from the caller's own roles; maker-checker (approver != requester), the
    request's fingerprint and the account's eligibility are re-checked by the core ``approve``."""
    from wapsi.pipeline.run import approve
    from wapsi.verify import PLAN_DRAFT_ID
    from wapsi.verify.injection import RulesGuard

    draft_id = str(p_draft_id or "").strip()
    approver = str(p_approver or "").strip()
    if not _DRAFT.match(draft_id):
        return error(f"not a draft id: {draft_id!r}")
    if not approver:
        return error("the approving supervisor is required")
    cfg = settings(session)
    store = SnowflakeCaseStore(session)
    case_id = store.case_for_draft(draft_id)
    bundle, info = (store.get_bundle(case_id), store.case_info(case_id)) if case_id else (None, None)
    draft = next((d for d in (bundle.drafts if bundle else []) if d.draft_id == draft_id), None)
    if bundle is None or draft is None:
        return error(f"draft {draft_id} not found")
    if draft.kind != DraftKind.ACCOUNT_LEVEL_HOLD_REQUEST:
        return error(f"{draft_id} is a {draft.kind.value}, not an account-level hold request")
    for maker in (draft_makers(bundle).get(draft_id), str(draft.structured.get("requested_by") or "")):
        if maker and maker.casefold() == approver.casefold():
            return error(f"maker-checker: {approver} requested {draft_id} and cannot also approve it")
    ledger = _ledger_for_case(session, info, cfg)
    search = search or _search(session, cfg)
    n_events = len(bundle.audit)
    try:
        approve(bundle, draft_id, approver=approver, role="supervisor", decision=ApprovalDecision.APPROVED,
                reason_code=str(p_reason_code or ""), comment=str(p_comment or ""), ledger=ledger, search=search,
                guard=RulesGuard())
    except (PermissionError, ValueError, KeyError) as exc:
        return error(f"not approved: {exc}")
    new_events = bundle.audit[n_events:]
    store.save(bundle, parts=("plan", "drafts"))
    dec = next(d for d in bundle.plan.decisions if d.account_id == draft.account_id)
    plan_report = bundle.verifications.get(PLAN_DRAFT_ID)
    return {"ok": True, "draft_id": draft_id, "case_id": bundle.case.case_id,
            "decision": ApprovalDecision.APPROVED.value, "approver": approver, "status": bundle.case.status.value,
            "account_id": dec.account_id, "disposition": dec.disposition.value, "hold_amount": dec.hold_amount,
            "reason_codes": dec.reason_codes, "plan_check_passed": plan_report.passed if plan_report else None,
            "redrafted": [e.object_id for e in new_events if e.action == "draft_created"],
            "withdrawn": [e.object_id for e in new_events if e.action == "draft_withdrawn"],
            "next": "The redrafted hold instruction and CFCFRMS update are new versions: approve them with APPROVE."}


# --------------------------------------------------------------------------- CHECK_LEA


def check_lea_proc(session: Any, p_case_id: str, p_request: Any, p_actor: str | None = None) -> dict[str, Any]:
    """Check a police / LEA hold request against the eight-field checklist (rules only). The hold is limited to
    the identifiable amount on the money trail; missing fields go into the clarification letter; the request's
    free text is untrusted (quoted back, never followed). Recorded as a sealed audit event; the plan never changes."""
    from pydantic import ValidationError

    from wapsi.pipeline.run import check_lea
    from wapsi.verify.injection import RulesGuard
    from wapsi.verify.lea_request import LeaHoldRequest, missing_labels

    case_id = str(p_case_id or "").strip()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r}")
    raw = p_request
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return error("P_REQUEST must be a JSON object with the request fields")
    if not isinstance(raw, dict):
        return error("P_REQUEST must be a JSON object with the request fields")
    try:
        actor, role = persona(session, p_actor)
    except LookupError as exc:
        return error(str(exc))
    if role == "auditor":
        return error("auditors have read-only access")
    cfg = settings(session)
    store, bundle, info = _load(session, case_id)
    if bundle is None:
        return error(f"{case_id} not found; call TRACE_CASE with one of its NCRP ids")
    raw = {"request_id": f"LEA-{case_id}-{len(bundle.audit) + 1}", **raw}
    try:
        request = LeaHoldRequest.model_validate(raw)
    except ValidationError as exc:
        return error("invalid request: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]))
    ledger = _ledger_for_case(session, info, cfg)
    result = check_lea(bundle, request, ledger, guard=RulesGuard(), actor=actor, role=role)
    store.sync_audit(bundle)
    return {"ok": True, "case_id": case_id, "request_id": request.request_id,
            "missing_labels": missing_labels(result), "check": json.loads(result.model_dump_json()),
            "next": "Hold at most the identifiable amount; send the letter for any missing field. The plan is "
                    "unchanged."}


# --------------------------------------------------------------------------- APPEND_AUDIT


def append_audit_proc(session: Any, p_case_id: str, p_action: str, p_object_type: str, p_object_id: str,
                      p_details: Any = None) -> dict[str, Any]:
    """Seal one event onto a case's audit chain (the actions runner's audit write path; it has no direct INSERT
    on AUDIT_LOG). The actor is CURRENT_USER()."""
    case_id = str(p_case_id or "").strip()
    action = str(p_action or "").strip()
    object_type = str(p_object_type or "").strip()
    object_id = str(p_object_id or "").strip()
    if not _CASE.match(case_id):
        return error(f"not a case id: {case_id!r}")
    if not _EVENT.match(action) or not _EVENT.match(object_type) or not _DRAFT.match(object_id):
        return error("action / object_type must be snake_case words and object_id an id")
    details = p_details
    if isinstance(details, str):
        try:
            details = json.loads(details) if details.strip() else {}
        except json.JSONDecodeError:
            return error("P_DETAILS must be a JSON object")
    if not isinstance(details, dict | None):
        return error("P_DETAILS must be a JSON object")
    store = SnowflakeCaseStore(session)
    if store.case_info(case_id) is None:
        return error(f"{case_id} not found")
    event = AuditEvent(at=utcnow(), actor=current_user(session), role="actions_runner", action=action,
                       object_type=object_type, object_id=object_id, details=details or {})
    store.append_audit(case_id, event)
    return {"ok": True, "case_id": case_id, "seq": event.seq, "hash": event.hash}


# --------------------------------------------------------------------------- WAPSI_VERSION (smoke)


def version_proc(session: Any) -> dict[str, Any]:
    """Imports the core from the zip and traces the showcase fixture (no table access)."""
    import pandas
    import pydantic

    import wapsi
    from wapsi.fixtures.showcase import N1, SHOWCASE_1, showcase_ledger
    from wapsi.graph.trace import trace_case

    t = trace_case(showcase_ledger(), [N1])
    return {"wapsi": wapsi.__version__, "pydantic": pydantic.VERSION, "pandas": pandas.__version__,
            "stolen": t.totals.stolen, "holdable": t.totals.holdable,
            "matches_fixture": t.totals.holdable == SHOWCASE_1["totals"]["holdable"]}
