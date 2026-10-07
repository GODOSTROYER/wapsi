"""Edition-neutral data model of an agent run's trace: what the agent did, in order, and what checked it.

One ``AgentTrace`` per agent invocation (one officer message and everything the agent did to answer it). Its
``steps`` are, in order:

- ``input``  the officer's message and the input guard's verdict (Model Armor or RulesGuard: passed / rewritten);
- ``model``  one model turn (Gemini, Gemma or the offline scripted model): latency, tokens when the model reports
  them, and the tool calls it asked for;
- ``tool``   one tool call: redacted arguments, a one-line result summary built from the tool's own result
  (numbers come from data), latency, status, the guard verdicts around it (argument screening, maker-checker
  policy, untrusted text found in its result) and the verifier verdict it carries (plan policy check, draft
  verification, re-verification before an approval);
- ``output`` the agent's reply, the output guard's verdict (Model Armor response screening when configured) and a
  grounding check: every rupee amount and account number in the reply must appear in a tool result;
- ``note``   only in a size-bounded audit record (``bounded``): how many steps were left out.

A trace is stored as one ``agent_trace`` audit event per case it touched (``trace_event``), so the case's hash
chain covers it; ``traces_from_audit`` reads them back.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wapsi.models import AuditEvent
from wapsi.timeutil import ensure_utc

TRACE_ACTION = "agent_trace"
TRACE_OBJECT = "agent_run"
SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 512 * 1024  # one agent_trace audit event (Firestore caps a document at 1 MiB)

StepKind = Literal["input", "model", "tool", "output", "note"]
OK_STATUSES = frozenset({"ok", "approved", "passed"})
STOPPED_STATUSES = frozenset({"blocked", "refused"})
FAILED_STATUSES = frozenset({"error", "exception"})
STOP_ACTIONS = frozenset({"blocked", "refused"})

SOURCE_LABELS = {
    "adk": "Live ADK run",
    "adk-offline-demo": "Offline ADK run (scripted model, no API key)",
    "recording": "Recorded offline ADK run (scripted model, no API key)",
}


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GuardVerdict(_M):
    """One safety screen around a step."""

    stage: str  # input | model_request | tool_args | tool_policy | tool_result | output
    source: str = ""  # model_armor | fallback (RulesGuard after a Model Armor failure) | RulesGuard | maker_checker
    flagged: bool = False
    action: str = "passed"  # passed | rewritten | blocked | refused | quoted | logged | not_screened
    categories: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def stopped(self) -> bool:
        return self.action in STOP_ACTIONS


class VerifierVerdict(_M):
    """A deterministic check the step's result carries (never the model's own opinion)."""

    name: str  # policy_check | verifier | drafts_verified | re_verification | reply_grounding
    passed: bool
    checked: int = 0
    failed: int = 0
    detail: str = ""


class TraceStep(_M):
    seq: int
    kind: StepKind
    name: str
    agent: str = ""
    status: str = "ok"
    started_at: datetime
    latency_ms: float | None = None
    call_id: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    guards: list[GuardVerdict] = Field(default_factory=list)
    verifier: VerifierVerdict | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    calls: list[str] = Field(default_factory=list)
    case_id: str | None = None

    @property
    def stopped(self) -> bool:
        """Blocked by a guard or refused by policy (the tool did not do what was asked)."""
        return self.status in STOPPED_STATUSES or any(g.stopped for g in self.guards)

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES


class TraceSummary(_M):
    tool_calls: int = 0
    model_turns: int = 0
    tool_latency_ms: float = 0.0
    model_latency_ms: float = 0.0
    wall_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    blocked: int = 0
    errors: int = 0
    guard_checks: int = 0
    guard_flags: int = 0
    verifier_passed: int = 0
    verifier_failed: int = 0
    tools: list[str] = Field(default_factory=list)


class AgentTrace(_M):
    schema_version: int = SCHEMA_VERSION
    trace_id: str
    app: str = ""
    session_id: str = ""
    user: str = ""  # display name (the audit actor)
    user_id: str = ""  # the authenticated user id (IAP e-mail on Cloud Run): scopes who may read the trace
    role: str = ""
    source: str = "adk"
    model: str = ""
    prompt: str = ""  # the officer's message, redacted and shortened
    started_at: datetime
    finished_at: datetime | None = None
    case_ids: list[str] = Field(default_factory=list)
    steps: list[TraceStep] = Field(default_factory=list)

    # ------------------------------------------------------------------ views
    def tool_steps(self) -> list[TraceStep]:
        return [s for s in self.steps if s.kind == "tool"]

    def summary(self) -> TraceSummary:
        tools = self.tool_steps()
        models = [s for s in self.steps if s.kind == "model"]
        tin = [s.tokens_in for s in models if s.tokens_in is not None]
        tout = [s.tokens_out for s in models if s.tokens_out is not None]
        guards = [g for s in self.steps for g in s.guards]
        verdicts = [s.verifier for s in self.steps if s.verifier is not None]
        wall = None
        if self.finished_at is not None:
            wall = max(0.0, (ensure_utc(self.finished_at) - ensure_utc(self.started_at)).total_seconds() * 1000)
        return TraceSummary(
            tool_calls=len(tools),
            model_turns=len(models),
            tool_latency_ms=round(sum(s.latency_ms or 0.0 for s in tools), 3),
            model_latency_ms=round(sum(s.latency_ms or 0.0 for s in models), 3),
            wall_ms=round(wall, 3) if wall is not None else None,
            tokens_in=sum(tin) if tin else None,
            tokens_out=sum(tout) if tout else None,
            blocked=sum(1 for s in tools if s.stopped),
            errors=sum(1 for s in self.steps if s.failed),
            guard_checks=sum(1 for g in guards if g.action != "not_screened"),
            guard_flags=sum(1 for g in guards if g.flagged),
            verifier_passed=sum(1 for v in verdicts if v.passed),
            verifier_failed=sum(1 for v in verdicts if not v.passed),
            tools=[s.name for s in tools],
        )

    def skeleton(self) -> list[tuple[Any, ...]]:
        """What happened, without timings, ids or wording: (kind, name, status, guard actions, verifier passed).
        Two runs of the same script on the same data have the same skeleton."""
        return [(s.kind, s.name, s.status, tuple(f"{g.stage}:{g.action}" for g in s.guards),
                 None if s.verifier is None else s.verifier.passed) for s in self.steps]

    def fingerprint(self, *, timings: bool = True) -> str:
        """SHA-256 of the trace's canonical JSON; ``timings=False`` drops clock-dependent fields and run ids."""
        data = self.model_dump(mode="json")
        if not timings:
            for k in ("trace_id", "session_id", "started_at", "finished_at"):
                data.pop(k, None)
            for s in data["steps"]:
                for k in ("started_at", "latency_ms", "call_id"):
                    s.pop(k, None)
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- audit events


def bounded(trace: AgentTrace, max_bytes: int = MAX_EVENT_BYTES) -> AgentTrace:
    """``trace`` itself when its JSON fits ``max_bytes``; otherwise a copy that fits: first without the redacted
    raw results (every step keeps its one-line summary, status, guards and verifier verdict), then keeping only the
    first and last steps around a ``note`` step that says how many were left out. A runaway agent loop must not
    produce an audit event a store cannot write (a Firestore document is capped at 1 MiB)."""

    def size(t: AgentTrace) -> int:
        return len(t.model_dump_json().encode("utf-8"))

    if size(trace) <= max_bytes:
        return trace
    t = trace.model_copy(deep=True)
    for s in t.steps:
        s.result = {}
    if size(t) <= max_bytes:
        return t
    steps, keep = t.steps, len(t.steps)
    while keep > 2:
        keep = max(2, keep // 2)
        head = steps[: (keep + 1) // 2]
        tail = steps[len(steps) - keep // 2:]
        gap = TraceStep(seq=head[-1].seq + 1, kind="note", name="steps left out", status="truncated",
                        started_at=head[-1].started_at,
                        summary=f"{len(steps) - len(head) - len(tail)} of {len(steps)} steps left out of this "
                                "audit record (trace too large); the run's totals above count every step")
        candidate = t.model_copy(update={"steps": [*head, gap, *tail]})
        if keep == 2 or size(candidate) <= max_bytes:
            return candidate
    return t


def trace_event(trace: AgentTrace, *, case_id: str | None = None) -> AuditEvent:
    """The audit event recording ``trace`` on a case's hash-chained log (summary fields first, so the audit page's
    one-line summary reads well; the full trace sits under ``trace``). The summary fields describe the whole run;
    the stored trace is size-bounded (``bounded``)."""
    s = trace.summary()
    details: dict[str, Any] = {
        "tools": " → ".join(s.tools) if s.tools else "no tool calls",
        "tool_calls": s.tool_calls,
        "blocked": s.blocked,
        "verifier": f"{s.verifier_passed} passed, {s.verifier_failed} failed",
        "guard_flags": s.guard_flags,
        "source": trace.source,
        "trace_id": trace.trace_id,
        "trace": bounded(trace).model_dump(mode="json"),
    }
    if case_id:
        details["case_id"] = case_id
    return AuditEvent(at=trace.finished_at or trace.started_at, actor=trace.user or "agent",
                      role=trace.role or "analyst", action=TRACE_ACTION, object_type=TRACE_OBJECT,
                      object_id=trace.trace_id, details=details)


def trace_from_event(event: AuditEvent) -> AgentTrace | None:
    if event.action != TRACE_ACTION or not isinstance(event.details.get("trace"), dict):
        return None
    try:
        return AgentTrace.model_validate(event.details["trace"])
    except ValidationError:
        return None


def traces_from_audit(events: Iterable[AuditEvent], *, case_id: str | None = None) -> list[AgentTrace]:
    """Agent traces recorded in an audit log, oldest first (one per run, even if recorded twice)."""
    out: dict[str, AgentTrace] = {}
    for e in events:
        t = trace_from_event(e)
        if t is None or (case_id and t.case_ids and case_id not in t.case_ids):
            continue
        out.setdefault(t.trace_id, t)
    return sorted(out.values(), key=lambda t: ensure_utc(t.started_at))


def dump_traces(traces: Sequence[AgentTrace]) -> str:
    return json.dumps([t.model_dump(mode="json") for t in traces], ensure_ascii=False, indent=1) + "\n"


def load_traces(text: str) -> list[AgentTrace]:
    data = json.loads(text)
    items = data.get("traces", []) if isinstance(data, dict) else data
    return [AgentTrace.model_validate(x) for x in items]
