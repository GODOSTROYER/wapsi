"""Small FastAPI for the AMD edition (automation, benchmarks, health).

    uvicorn editions.amd.app.api:app --host 0.0.0.0 --port 8080

Endpoints
    GET  /healthz                     liveness + non-secret configuration (no network calls)
    GET  /cases                       processed cases in the store (newest first)
    GET  /cases/{case_id}             one case summary (``?full=1`` returns the whole CaseBundle)
    POST /cases/{ids}/process         run the pipeline: ``ids`` = NCRP id(s), comma-separated, or ``CASE-<ncrp>``;
                                      ``?draft=0`` stops after the hold plan
    GET  /metrics                     Prometheus text format (counters, latency sums, GNN / judge stats)
    GET  /agent                       the officer agent: backend, model, serving platform, tool mode (no secrets)
    POST /agent/run                   one officer message through the ADK agent (Gemma 4 on AMD; the offline
                                      fake endpoint in demo mode): reply, tool sequence, verdicts, trace
    GET  /agent/traces                agent runs: ``?case_id=`` reads the case's hash-chained audit log (with the
                                      chain check); without it, the runs this process recorded

Port 8080 is bound to 127.0.0.1 by ``docker-compose.yml``. If ``WAPSI_API_TOKEN`` is set, POST requires
``Authorization: Bearer <token>``. ``POST /agent/run`` takes the caller's role from the request only when the
caller is authenticated by that token (or in demo mode); otherwise the run is an analyst's, so an unauthenticated
caller can never make the agent record a supervisor approval.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .services import Services, get_services, process, resolve_ncrp_ids
from .summary import case_summary

log = logging.getLogger("wapsi.amd.api")


class Metrics:
    """Process-local counters rendered as Prometheus text (no client library needed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {
            "wapsi_cases_processed_total": 0,
            "wapsi_case_failures_total": 0,
            "wapsi_drafts_total": 0,
            "wapsi_drafts_verified_total": 0,
            "wapsi_llm_drafts_total": 0,
            "wapsi_prose_fallbacks_total": 0,
            "wapsi_injection_flags_total": 0,
            "wapsi_case_seconds_sum": 0.0,
            "wapsi_time_to_hold_plan_seconds_sum": 0.0,
            "wapsi_agent_runs_total": 0,
            "wapsi_agent_failures_total": 0,
            "wapsi_agent_tool_calls_total": 0,
            "wapsi_agent_blocked_calls_total": 0,
            "wapsi_agent_ungrounded_replies_total": 0,
            "wapsi_agent_seconds_sum": 0.0,
        }

    def record(self, summary: dict[str, Any], wall_s: float) -> None:
        with self._lock:
            c = self.counters
            c["wapsi_cases_processed_total"] += 1
            c["wapsi_drafts_total"] += summary["drafts"]
            c["wapsi_drafts_verified_total"] += summary["drafts_passed"]
            c["wapsi_llm_drafts_total"] += summary["llm_drafts"]
            c["wapsi_prose_fallbacks_total"] += summary["prose_fallbacks"]
            c["wapsi_injection_flags_total"] += summary["injection_flags"]
            c["wapsi_case_seconds_sum"] += wall_s
            c["wapsi_time_to_hold_plan_seconds_sum"] += summary["timings_s"].get("time_to_hold_plan_s", 0.0)

    def failure(self) -> None:
        with self._lock:
            self.counters["wapsi_case_failures_total"] += 1

    def agent_run(self, result: Any) -> None:
        with self._lock:
            c = self.counters
            c["wapsi_agent_runs_total"] += 1
            c["wapsi_agent_failures_total"] += 0 if result.ok else 1
            c["wapsi_agent_tool_calls_total"] += len(result.tools)
            c["wapsi_agent_blocked_calls_total"] += result.blocked
            c["wapsi_agent_ungrounded_replies_total"] += 1 if result.grounded is False else 0
            c["wapsi_agent_seconds_sum"] += result.wall_ms / 1000.0

    def render(self, services: Services | None) -> str:
        with self._lock:
            c = dict(self.counters)
        lines = []
        for name, value in c.items():
            kind = "counter" if name.endswith("_total") or name.endswith("_sum") else "gauge"
            lines += [f"# TYPE {name} {kind}", f"{name} {value:g}"]
        lines += ["# TYPE wapsi_case_seconds_count counter",
                  f"wapsi_case_seconds_count {c['wapsi_cases_processed_total']:g}"]
        if services is not None:
            desc = services.describe()
            llm = str(desc.get("llm_name", "")).replace('"', "'")
            lines += ["# TYPE wapsi_info gauge",
                      f'wapsi_info{{llm="{llm}",demo_mode="{int(bool(desc.get("demo_mode")))}"}} 1']
            stats = getattr(services.scorer, "stats", None)
            if stats is not None and hasattr(stats, "as_dict"):
                s = stats.as_dict()
                for k in ("calls", "gnn_ok", "failures", "fallback_rows"):
                    lines += [f"# TYPE wapsi_gnn_{k}_total counter", f"wapsi_gnn_{k}_total {s[k]:g}"]
            jstats = getattr(services.guard, "stats", None)
            if jstats is not None and hasattr(jstats, "as_dict"):
                s = jstats.as_dict()
                for k in ("judge_calls", "judge_errors", "judge_flagged", "rules_flagged"):
                    lines += [f"# TYPE wapsi_{k}_total counter", f"wapsi_{k}_total {s[k]:g}"]
        return "\n".join(lines) + "\n"


METRICS = Metrics()

app = FastAPI(title="Wapsi AMD edition", version="0.1.0",
              description="Money-trail copilot: pipeline API on vLLM-ROCm (synthetic data only).")


def services_dep() -> Services:
    return get_services()


Svc = Annotated[Services, Depends(services_dep)]


def require_token(svc: Svc, authorization: Annotated[str | None, Header()] = None) -> None:
    token = svc.settings.api_token
    if not token:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


@app.get("/healthz")
def healthz(svc: Svc) -> dict[str, Any]:
    from .agent import agent_status

    status = agent_status(svc)  # no network call; no secrets
    agent = {k: status[k] for k in ("available", "backend", "model", "provider", "reason") if k in status}
    return {"status": "ok", **svc.describe(), "agent": agent}


@app.get("/cases")
def list_cases(svc: Svc) -> dict[str, Any]:
    cases = svc.store.list_cases()
    return {"count": len(cases), "cases": [c.model_dump(mode="json") for c in cases]}


@app.get("/cases/{case_id}")
def get_case(case_id: str, svc: Svc, full: Annotated[bool, Query()] = False) -> dict[str, Any]:
    try:
        bundle = svc.store.get_bundle(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    return bundle.model_dump(mode="json") if full else case_summary(bundle)


@app.post("/cases/{ids}/process", dependencies=[Depends(require_token)])
def process_ids(ids: str, svc: Svc, draft: Annotated[bool, Query()] = True) -> dict[str, Any]:
    try:
        ncrp_ids = resolve_ncrp_ids(svc, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    t = time.perf_counter()
    try:
        bundle = process(svc, ncrp_ids, draft=draft)
    except Exception as exc:  # noqa: BLE001 - report, count, keep serving
        METRICS.failure()
        log.exception("process_case failed for %s", ncrp_ids)
        raise HTTPException(status_code=502, detail=f"pipeline failed: {type(exc).__name__}: {exc}"[:500]) from exc
    wall = time.perf_counter() - t
    summary = case_summary(bundle)
    METRICS.record(summary, wall)
    return {**summary, "wall_s": round(wall, 4)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(svc: Svc) -> str:
    return METRICS.render(svc)


# --------------------------------------------------------------------------- the officer agent (ADK on AMD)


class AgentAsk(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="The officer's message.")
    user: str = Field(default="api", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._@\-]*$")
    role: str = Field(default="analyst", pattern="^(analyst|supervisor|auditor)$")
    case_id: str | None = Field(default=None, max_length=128, description="The case the message is about.")
    session: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9._\-]*$")
    include_trace: bool = True


def _agent_role(svc: Services, asked: str) -> tuple[str, str | None]:
    """(role the run gets, note). A role other than analyst is honoured only for a caller the API token
    authenticated, or in demo mode (synthetic showcase data on a laptop)."""
    if asked == "analyst" or svc.settings.api_token or svc.settings.demo_mode:
        return asked, None
    return "analyst", (f"role {asked!r} needs an authenticated caller (set WAPSI_API_TOKEN); the run was made as "
                       "an analyst")


@app.get("/agent")
def agent_info(svc: Svc) -> dict[str, Any]:
    from .agent import agent_status

    return agent_status(svc)


@app.post("/agent/run", dependencies=[Depends(require_token)])
def agent_run(body: AgentAsk, svc: Svc) -> dict[str, Any]:
    from . import agent as agent_mod

    if body.case_id is not None:
        try:
            known = svc.store.get_bundle(body.case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if known is None:
            raise HTTPException(status_code=404, detail=f"case {body.case_id} not found")
    role, note = _agent_role(svc, body.role)
    try:
        result = agent_mod.ask(body.message, services=svc, case_id=body.case_id, user_id=body.user, role=role,
                               session=body.session)
    except agent_mod.AgentUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    METRICS.agent_run(result)
    out = result.as_dict(include_trace=body.include_trace)
    out["agent"] = {k: v for k, v in agent_mod.agent_status(svc).items()
                    if k in ("backend", "model", "provider", "tool_mode", "tool_result_role")}
    if note:
        out["role_note"] = note
    return out


@app.get("/agent/traces")
def agent_traces(svc: Svc, case_id: Annotated[str | None, Query(max_length=128)] = None,
                 limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, Any]:
    """Redacted ``AgentTrace``s, oldest first. With ``case_id``: the runs sealed into that case's audit log (any
    process: API, UI, CLI) and the result of re-verifying its hash chain. Without: this process's recent runs."""
    from wapsi.agenttrace import traces_from_audit

    chain = None
    if case_id is not None:
        try:
            bundle = svc.store.get_bundle(case_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"case {case_id} not found")
        items = traces_from_audit(bundle.audit, case_id=case_id)
        report = svc.store.verify_audit(case_id) if hasattr(svc.store, "verify_audit") else None
        if report is not None:
            chain = {"ok": report.ok, "events": len(bundle.audit), "reason": report.reason,
                     "sealed_trace_events": sum(1 for e in bundle.audit if e.action == "agent_trace" and e.hash)}
    else:
        from . import agent as agent_mod

        rt = agent_mod._runtime
        items = rt.traces() if rt is not None and rt.services is svc else []
    items = items[-limit:]
    return {"count": len(items), "source": "audit_log" if case_id else "process", "audit_chain": chain,
            "traces": [t.model_dump(mode="json") for t in items],
            "summaries": [{"trace_id": t.trace_id, "cases": t.case_ids, "source": t.source, "model": t.model,
                           "provider": t.provider, **t.summary().model_dump(mode="json")} for t in items]}
