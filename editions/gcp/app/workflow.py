"""``case_pipeline``: a deterministic ADK 2.x ``Workflow`` for event-driven work (Pub/Sub push -> ADK trigger).

Pub/Sub push subscriptions deliver to ``POST /apps/case_pipeline/trigger/pubsub``. Three message kinds:

- CFCFRMS notice: ``{"ncrp_ids": [...]}`` (topic ``wapsi-notices``);
- Cloud Storage upload (bucket notification, ``OBJECT_FINALIZE``): a complaint PDF/screenshot to extract with
  Gemini multimodal (topic ``wapsi-uploads``);
- clock sweep: ``{"task": "clocks"}`` published by Cloud Scheduler (topic ``wapsi-clocks``).

Graph::

    START -> parse_notice --notice--> process_notice -> route_case --ready-----> queue_for_supervisor
                          --upload--> extract_upload --+               --attention-> flag_for_officer
                          --clocks--> sweep_clocks     |               --rejected--> reject_notice
                          --rejected-> reject_notice   +--> process_notice

No LLM decides anything here: tracing, planning, drafting and verification run in the Wapsi pipeline, and every
route is a pure function of the data. Drafts still wait for a supervisor's approval.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.workflow import Workflow
from google.genai import types

from wapsi.domain.clocks import clocks_for_bundle, evaluate_clocks
from wapsi.models import Action, ActionKind, AuditEvent, CaseStatus, ClockStatus
from wapsi.pipeline.run import process_case
from wapsi.timeutil import fmt_ist, utcnow

from .services import get_services

APP_NAME = "case_pipeline"
log = logging.getLogger("wapsi.gcp.workflow")
NCRP_RE = re.compile(r"^\d{14}$")
ACTOR = "case-pipeline"


def _payload(node_input: Any) -> Any:
    if isinstance(node_input, types.Content):
        text = " ".join(p.text for p in (node_input.parts or []) if p.text)
        try:
            node_input = json.loads(text)
        except ValueError:
            return {"data": text}
    return node_input


def parse_notice(node_input: Any, ctx: Context) -> dict[str, Any]:
    """Pub/Sub trigger text ``{"data": ..., "attributes": ...}`` -> a routed work item."""
    msg = _payload(node_input)
    data = msg.get("data", msg) if isinstance(msg, dict) else msg
    attrs = (msg.get("attributes") or {}) if isinstance(msg, dict) else {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {"ncrp_ids": re.findall(r"(?<![\w-])\d{14}(?![\w-])", data)}
    data = data if isinstance(data, dict) else {}
    source = str(attrs.get("source") or attrs.get("eventType") or "pubsub")[:40]

    if data.get("task") == "clocks":
        ctx.route = "clocks"
        return {"task": "clocks", "source": source}
    if data.get("kind") == "storage#object" or attrs.get("eventType") == "OBJECT_FINALIZE":
        bucket = str(data.get("bucket") or attrs.get("bucketId") or "")
        name = str(data.get("name") or attrs.get("objectId") or "")
        ctx.route = "upload" if bucket and name else "rejected"
        return {"file_uri": f"gs://{bucket}/{name}", "source": source, "reason": "" if bucket and name
                else "storage notification without bucket/object"}

    raw = data.get("ncrp_ids") or ([data["ncrp_id"]] if data.get("ncrp_id") else [])
    ids = [str(x).strip() for x in raw if isinstance(x, (str, int))]
    valid = sorted({i for i in ids if NCRP_RE.match(i)})
    invalid = sorted(set(ids) - set(valid))
    ctx.route = "notice" if valid and not invalid else "rejected"
    return {"ncrp_ids": valid, "invalid": invalid, "source": source,
            "reason": "" if ctx.route == "notice" else "no valid NCRP ids in the notice"}


def extract_upload(node_input: dict[str, Any]) -> dict[str, Any]:
    """Gemini multimodal extraction of an uploaded complaint; only ledger-confirmed complaints go on."""
    from .intake import extract_complaint

    out = extract_complaint(node_input["file_uri"])
    fields = out.get("fields") or {}
    check = out.get("ledger_check") or {}
    ncrp = str(fields.get("ncrp_id") or "")
    if out.get("status") != "ok" or not (check.get("complaint_found") and check.get("amount_matches")):
        return {"ncrp_ids": [], "invalid": [ncrp] if ncrp else [], "source": "upload",
                "file_uri": node_input["file_uri"],
                "reason": out.get("error") or "extracted complaint does not match the ledger"}
    return {"ncrp_ids": [ncrp], "invalid": [], "source": "upload", "file_uri": node_input["file_uri"],
            "narrative_flagged": bool((out.get("narrative") or {}).get("flagged"))}


def process_notice(node_input: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Run the Wapsi pipeline for the notice and store the case bundle."""
    ids = node_input.get("ncrp_ids") or []
    if not ids or node_input.get("invalid"):
        return {"ok": False, "reason": node_input.get("reason") or "no valid NCRP ids",
                "invalid": node_input.get("invalid", [])}
    svc = get_services()
    try:
        for i in ids:
            svc.ledger.complaint(i)
    except KeyError as exc:
        return {"ok": False, "reason": f"unknown complaint {exc}"}
    with svc.lock:
        bundle = process_case(svc.ledger, ids, llm=svc.llm, cfg=svc.cfg, search=svc.search, guard=svc.guard,
                              store=svc.store, assigned_to="intake-queue")
    flagged = sorted({e.object_id for e in bundle.audit if e.action == "untrusted_instruction_detected"})
    failed = sorted(k for k, r in bundle.verifications.items() if not r.passed)
    ctx.state["wapsi:last_case_id"] = bundle.case.case_id
    return {
        "ok": True,
        "case_id": bundle.case.case_id,
        "status": bundle.case.status.value,
        "drafts": len(bundle.drafts),
        "failed_checks": failed,
        "untrusted_text_in": flagged,
        "recoverable": bundle.plan.total_recoverable if bundle.plan else 0.0,
        "source": node_input.get("source", "pubsub"),
    }


def route_case(node_input: dict[str, Any], ctx: Context) -> dict[str, Any]:
    if not node_input.get("ok"):
        ctx.route = "rejected"
    elif node_input.get("failed_checks") or node_input.get("untrusted_text_in"):
        ctx.route = "attention"
    else:
        ctx.route = "ready"
    return node_input


def _notify(case_id: str, text: str, channel: str) -> dict[str, Any]:
    svc = get_services()
    action = Action(action_id=f"ACT-{case_id}-{channel}-{utcnow():%Y%m%d%H%M%S%f}", case_id=case_id,
                    kind=ActionKind.SLACK_NOTICE, payload={"channel": channel, "text": text}, created_at=utcnow())
    done = svc.sink.submit(action)
    return {"action_id": done.action_id, "status": done.status.value}


def queue_for_supervisor(node_input: dict[str, Any]) -> dict[str, Any]:
    cid = node_input["case_id"]
    note = _notify(cid, f"{cid}: {node_input['drafts']} verified drafts await supervisor approval.", "supervisors")
    return {**node_input, "outcome": "queued_for_supervisor", "notification": note}


def flag_for_officer(node_input: dict[str, Any]) -> dict[str, Any]:
    cid = node_input["case_id"]
    note = _notify(cid, f"{cid}: needs an officer (failed checks or untrusted instructions).", "officers")
    return {**node_input, "outcome": "needs_officer", "notification": note}


def reject_notice(node_input: dict[str, Any]) -> dict[str, Any]:
    return {**node_input, "outcome": "rejected"}


def sweep_clocks(node_input: dict[str, Any]) -> dict[str, Any]:
    """Cloud Scheduler job: re-evaluate every open case's clocks and notify on WARNING / OVERDUE."""
    svc = get_services()
    now = utcnow()
    swept, alerts, audit_failed = 0, [], []
    for case in svc.store.list_cases():
        if case.status == CaseStatus.CLOSED:
            continue
        bundle = svc.store.get_bundle(case.case_id)
        if bundle is None:
            continue
        swept += 1
        clocks = evaluate_clocks(bundle.clocks, now) if bundle.clocks else clocks_for_bundle(bundle, now)
        due = [c for c in clocks if c.status in (ClockStatus.WARNING, ClockStatus.OVERDUE)]
        for c in due:
            alerts.append({"case_id": case.case_id, "kind": c.kind.value, "status": c.status.value,
                           "due": fmt_ist(c.due), "subject": c.subject, "notes": c.notes})
        if due:
            worst = "OVERDUE" if any(c.status == ClockStatus.OVERDUE for c in due) else "WARNING"
            _notify(case.case_id, f"{case.case_id}: {len(due)} clock(s) {worst}", "clocks")
            try:
                svc.store.append_audit(case.case_id, AuditEvent(
                    at=now, actor=ACTOR, role="system", action="clock_sweep", object_type="case",
                    object_id=case.case_id, details={"alerts": [f"{c.kind.value}:{c.status.value}" for c in due]}))
            except Exception:  # noqa: BLE001 - e.g. AuditChainError on a tampered log: keep sweeping the others
                log.exception("could not record the clock sweep in the audit log of %s", case.case_id)
                audit_failed.append(case.case_id)
    return {"outcome": "clocks_swept", "cases": swept, "alerts": alerts[:50], "alert_count": len(alerts),
            "audit_failed": audit_failed[:50], "as_of": fmt_ist(now)}


def build_workflow() -> Workflow:
    return Workflow(
        name=APP_NAME,
        edges=[
            ("START", parse_notice,
             {"notice": process_notice, "upload": extract_upload, "clocks": sweep_clocks, "rejected": reject_notice}),
            (extract_upload, process_notice),
            (process_notice, route_case,
             {"ready": queue_for_supervisor, "attention": flag_for_officer, "rejected": reject_notice}),
        ],
    )


def build_pipeline_app() -> App:
    return App(name=APP_NAME, root_agent=build_workflow())


app = build_pipeline_app()
root_agent = app.root_agent
