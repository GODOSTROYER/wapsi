"""``ToolTraceRecorder``: records agent runs as ``AgentTrace`` objects. Pure Python, no agent-framework imports.

The ADK edition drives it from plugin callbacks (``editions/gcp/app/tracing.py``); any other agent loop can call
the same methods. Every method is safe to call out of order (a tool end without a start, a verdict for an unknown
run): the recorder never raises into the agent run it observes.

    rec = ToolTraceRecorder()
    rec.start_run("inv-1", user="asha", role="analyst", prompt="Open case 31009260000101")
    rec.guard("inv-1", GuardVerdict(stage="input", source="RulesGuard"))
    rec.model_start("inv-1", agent="wapsi_officer", model="gemini-3.8-flash")
    rec.model_end("inv-1", calls=["open_case"], tokens_in=812, tokens_out=24)
    rec.tool_start("inv-1", "open_case", args={"ncrp_ids": ["31009260000101"]}, call_id="c1")
    rec.tool_end("inv-1", "open_case", result={...}, call_id="c1")
    rec.output("inv-1", "Opened CASE-31009260000101 ...")
    trace = rec.finish_run("inv-1")
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from wapsi.agenttrace.model import AgentTrace, GuardVerdict, TraceStep, VerifierVerdict
from wapsi.agenttrace.redact import redact_text, redact_value
from wapsi.agenttrace.summarize import (
    as_text,
    case_of,
    grounding_facts,
    reply_grounding,
    result_guards,
    status_of,
    summarize_result,
    verifier_of,
)
from wapsi.timeutil import utcnow

log = logging.getLogger("wapsi.agenttrace")

TOOL_STAGES = frozenset({"tool_args", "tool_policy", "tool_result"})
PROMPT_CHARS = 280


class TickClock:
    """Deterministic monotonic clock for tests and reproducible recordings: every read advances ``step_ms``."""

    def __init__(self, step_ms: float = 1.0, start: float = 0.0) -> None:
        self.t = start
        self.step = step_ms / 1000.0

    def __call__(self) -> float:
        self.t += self.step
        return self.t


@dataclass
class _Run:
    trace: AgentTrace
    t0: float
    open_tools: dict[str, tuple[TraceStep, float]] = field(default_factory=dict)
    open_model: tuple[TraceStep, float] | None = None
    pending_tool_guards: list[tuple[str, str, GuardVerdict]] = field(default_factory=list)  # (call_id, tool, v)
    pending_output_guards: list[GuardVerdict] = field(default_factory=list)
    amounts: set[float] = field(default_factory=set)
    accounts: set[str] = field(default_factory=set)


class ToolTraceRecorder:
    """Collects one ``AgentTrace`` per run id (ADK: the invocation id).

    ``clock`` is a monotonic seconds clock (latencies); ``now`` gives wall-clock timestamps. Both are injectable
    so a recording can be made reproducible (``TickClock`` and a fixed start time). ``on_finish`` is called with
    every finished trace (persistence); its errors are logged, never raised.
    """

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter, now: Callable[[], datetime] = utcnow,
                 on_finish: Callable[[AgentTrace], None] | None = None, max_finished: int = 200,
                 source: str = "adk") -> None:
        self.clock = clock
        self.now = now
        self.on_finish = on_finish
        self.source = source
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self.finished: deque[AgentTrace] = deque(maxlen=max_finished)
        self._lock = threading.RLock()
        self._wall0: tuple[datetime, float] | None = None

    # ------------------------------------------------------------------ time
    def _stamp(self, t: float) -> datetime:
        """Wall-clock time of monotonic reading ``t`` (one anchor per recorder: steps are ordered consistently)."""
        if self._wall0 is None:
            self._wall0 = (self.now(), t)
        base, t0 = self._wall0
        return base + timedelta(seconds=t - t0)

    @staticmethod
    def _ms(t0: float, t1: float) -> float:
        return round(max(0.0, (t1 - t0) * 1000.0), 3)

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: str, *, app: str = "", session_id: str = "", user: str = "", role: str = "",
                  prompt: str = "", source: str | None = None, model: str = "") -> AgentTrace:
        with self._lock:
            t = self.clock()
            trace = AgentTrace(trace_id=run_id, app=app, session_id=session_id, user=user, role=role,
                               source=source or self.source, model=model,
                               prompt=redact_text(prompt or "", max_len=PROMPT_CHARS), started_at=self._stamp(t))
            if prompt:
                trace.steps.append(TraceStep(seq=1, kind="input", name="officer message", status="ok",
                                             started_at=trace.started_at, summary=trace.prompt))
            self._runs[run_id] = _Run(trace=trace, t0=t)
            while len(self._runs) > 64:  # abandoned runs (crashed invocations) must not pile up
                self._runs.popitem(last=False)
            return trace

    def _run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            self.start_run(run_id)
            run = self._runs[run_id]
        return run

    def has_run(self, run_id: str) -> bool:
        return run_id in self._runs

    def _add(self, run: _Run, **kw: Any) -> TraceStep:
        step = TraceStep(seq=len(run.trace.steps) + 1, **kw)
        run.trace.steps.append(step)
        return step

    # ------------------------------------------------------------------ guards
    def guard(self, run_id: str, verdict: GuardVerdict, *, call_id: str = "", tool: str = "") -> None:
        """Attach a guard verdict: input -> the input step; output -> the reply; tool stages -> the tool call
        (the open call with ``call_id``, else the next call of ``tool``)."""
        try:
            with self._lock:
                run = self._run(run_id)
                if verdict.stage == "input":
                    step = next((s for s in run.trace.steps if s.kind == "input"), None)
                    if step is None:
                        step = self._add(run, kind="input", name="officer message",
                                         started_at=self._stamp(self.clock()))
                    step.guards.append(verdict)
                    if verdict.action == "rewritten":
                        step.status = "flagged"
                elif verdict.stage == "output":
                    run.pending_output_guards.append(verdict)
                elif verdict.stage in TOOL_STAGES:
                    open_ = run.open_tools.get(call_id) if call_id else None
                    if open_ is None and tool:
                        open_ = next((v for v in run.open_tools.values() if v[0].name == tool), None)
                    if open_ is not None:
                        open_[0].guards.append(verdict)
                    else:
                        run.pending_tool_guards.append((call_id, tool, verdict))
                else:  # model_request and anything new: on the open model turn, else the latest step
                    target = run.open_model[0] if run.open_model else (run.trace.steps[-1] if run.trace.steps
                                                                      else None)
                    if target is None:
                        target = self._add(run, kind="input", name="officer message",
                                           started_at=self._stamp(self.clock()))
                    target.guards.append(verdict)
        except Exception:  # noqa: BLE001 - tracing never breaks a run
            log.exception("trace: guard verdict not recorded")

    # ------------------------------------------------------------------ model turns
    def model_start(self, run_id: str, *, agent: str = "", model: str = "",
                    context_texts: Iterable[str] = ()) -> None:
        """A model call begins. ``context_texts``: tool results the model can see (for reply grounding)."""
        try:
            with self._lock:
                run = self._run(run_id)
                amounts, accounts = grounding_facts(context_texts)
                run.amounts |= amounts
                run.accounts |= accounts
                t = self.clock()
                step = self._add(run, kind="model", name=model or "model", agent=agent, started_at=self._stamp(t))
                run.open_model = (step, t)
                if model and not run.trace.model:
                    run.trace.model = model
        except Exception:  # noqa: BLE001
            log.exception("trace: model start not recorded")

    def model_end(self, run_id: str, *, calls: Iterable[str] = (), tokens_in: int | None = None,
                  tokens_out: int | None = None, status: str = "ok", note: str = "") -> None:
        try:
            with self._lock:
                run = self._run(run_id)
                t = self.clock()
                if run.open_model is None:
                    step = self._add(run, kind="model", name=run.trace.model or "model", started_at=self._stamp(t))
                    t0 = t
                else:
                    step, t0 = run.open_model
                    run.open_model = None
                step.latency_ms = self._ms(t0, t)
                step.calls = [str(c) for c in calls]
                step.tokens_in, step.tokens_out = tokens_in, tokens_out
                step.status = status
                if step.calls:
                    step.summary = "asked for " + ", ".join(step.calls)
                elif note:
                    step.summary = redact_text(note, max_len=160)
                else:
                    step.summary = "wrote the reply" if status == "ok" else status
        except Exception:  # noqa: BLE001
            log.exception("trace: model end not recorded")

    # ------------------------------------------------------------------ tools
    def tool_start(self, run_id: str, tool: str, *, args: dict[str, Any] | None = None, call_id: str = "",
                   agent: str = "") -> None:
        try:
            with self._lock:
                run = self._run(run_id)
                t = self.clock()
                key = call_id or f"{tool}#{len(run.trace.steps) + 1}"
                step = self._add(run, kind="tool", name=tool, agent=agent, call_id=call_id,
                                 args=redact_value(dict(args or {}), max_str=200), started_at=self._stamp(t),
                                 status="running")
                keep = []
                for cid, name, v in run.pending_tool_guards:
                    (step.guards.append(v) if (cid and cid == call_id) or (not cid and name == tool)
                     else keep.append((cid, name, v)))
                run.pending_tool_guards = keep
                run.open_tools[key] = (step, t)
        except Exception:  # noqa: BLE001
            log.exception("trace: tool start not recorded")

    def tool_end(self, run_id: str, tool: str, *, result: Any = None, call_id: str = "",
                 args: dict[str, Any] | None = None, agent: str = "", error: str | None = None) -> None:
        """A tool call finished (or was answered by a guard without running: then there was no ``tool_start``)."""
        try:
            with self._lock:
                run = self._run(run_id)
                t = self.clock()
                key = call_id if call_id in run.open_tools else next(
                    (k for k, v in run.open_tools.items() if v[0].name == tool and not call_id), None)
                if key is None:  # answered before it started (a guard short-circuited the call)
                    self.tool_start(run_id, tool, args=args, call_id=call_id, agent=agent)
                    key = call_id or next(k for k, v in run.open_tools.items() if v[0].name == tool)
                    t = self.clock()
                step, t0 = run.open_tools.pop(key)
                step.latency_ms = self._ms(t0, t)
                step.status = status_of(result, error)
                step.summary = summarize_result(tool, result, args or step.args, error=error)
                step.result = (redact_value(result, max_str=160, max_items=6, max_depth=4)
                               if isinstance(result, dict) else {})
                step.case_id = case_of(args, result)
                step.verifier = verifier_of(tool, result)
                step.guards.extend(result_guards(tool, result))
                if step.status in {"blocked", "refused"} and not any(g.stopped for g in step.guards):
                    step.guards.append(GuardVerdict(stage="tool_policy", source="tool", flagged=False,
                                                    action=step.status, detail=step.summary))
                if step.case_id and step.case_id not in run.trace.case_ids and step.status not in {"error"}:
                    run.trace.case_ids.append(step.case_id)
                if isinstance(result, dict) and step.status not in {"error", "blocked", "refused"}:
                    amounts, accounts = grounding_facts([as_text(result)])
                    run.amounts |= amounts
                    run.accounts |= accounts
        except Exception:  # noqa: BLE001
            log.exception("trace: tool end not recorded")

    # ------------------------------------------------------------------ reply
    def output(self, run_id: str, text: str, *, agent: str = "", verifier: VerifierVerdict | None = None) -> None:
        """The agent's reply to the officer; checks that its amounts and accounts appear in tool results."""
        try:
            with self._lock:
                run = self._run(run_id)
                t = self.clock()
                step = self._add(run, kind="output", name="agent reply", agent=agent, started_at=self._stamp(t),
                                 summary=redact_text(text or "", max_len=PROMPT_CHARS))
                step.guards.extend(run.pending_output_guards)
                run.pending_output_guards = []
                step.verifier = verifier or reply_grounding(text or "", run.amounts, run.accounts)
                if not step.verifier.passed:
                    step.status = "flagged"
        except Exception:  # noqa: BLE001
            log.exception("trace: reply not recorded")

    def finish_run(self, run_id: str) -> AgentTrace | None:
        with self._lock:
            run = self._runs.pop(run_id, None)
            if run is None:
                return None
            t = self.clock()
            for step, t0 in run.open_tools.values():  # never finished (the run was cancelled)
                step.latency_ms, step.status = self._ms(t0, t), "unfinished"
            if run.open_model is not None:
                step, t0 = run.open_model
                step.latency_ms, step.status = self._ms(t0, t), "unfinished"
            out_step = next((s for s in reversed(run.trace.steps) if s.kind == "output"), None)
            if run.pending_output_guards:
                if out_step is not None:
                    out_step.guards.extend(run.pending_output_guards)
                run.pending_output_guards = []
            run.trace.finished_at = self._stamp(t)
            trace = run.trace
            self.finished.append(trace)
        if self.on_finish is not None:
            try:
                self.on_finish(trace)
            except Exception:  # noqa: BLE001
                log.exception("trace: on_finish failed for %s", run_id)
        return trace

    # ------------------------------------------------------------------ reads
    def traces(self, *, case_id: str | None = None) -> list[AgentTrace]:
        with self._lock:
            items = list(self.finished)
        return [t for t in items if case_id is None or case_id in t.case_ids]
