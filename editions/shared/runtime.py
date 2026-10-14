"""``AgentRuntime``: runs the ADK officer agent from synchronous code (FastAPI handlers, Streamlit, a CLI).

One runtime owns one ADK ``App`` (built on first use), one in-memory session store and **one long-lived event
loop on a daemon thread**. Every run is a coroutine submitted to that loop, so the model client's connections
stay on the loop they were created on (starting a new loop per request, as ``asyncio.run`` does, breaks LiteLLM's
cached clients) and callers never need a running loop of their own. Runs are serialized: one officer message at a
time per runtime, which is also what a shared model endpoint's rate limit wants.

Each ``run`` returns an ``AgentRunResult``:

- ``reply``: what to show the officer. The agent never computes money: every rupee amount and account number in
  the model's reply must appear in a tool result (the trace's reply-grounding check). When it does not, the
  model's reply is **not shown**; ``reply`` is then a summary built only from the tool results
  (``scripted.summarize_results``) and ``reply_source`` says ``tool_results``;
- ``trace``: the run's ``AgentTrace`` (every model turn and tool call with guard and verifier verdicts), which the
  trace plugin has already sealed into the audit chain of each case the run touched;
- ``tool_results``: the tools' own results, for callers that render numbers (they come from the pipeline).

Before every run the runtime binds its services into ``context`` (see there).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wapsi.agenttrace import AgentTrace, ToolTraceRecorder

from . import context
from .identity import ROLES, granted_state

log = logging.getLogger("wapsi.agent.runtime")

REPLY_MODEL, REPLY_TOOLS, REPLY_NOTICE = "model", "tool_results", "notice"
NOTICE_PREFIX = "[Wapsi]"


@dataclass
class AgentRunResult:
    ok: bool
    reply: str
    reply_source: str = REPLY_MODEL  # model | tool_results | notice
    model_reply: str = ""  # what the model wrote (differs from ``reply`` when it was replaced)
    grounded: bool | None = None  # reply-grounding verdict on the model's reply (None: no reply to check)
    grounding_detail: str = ""
    trace: AgentTrace | None = None
    trace_id: str = ""
    session_id: str = ""
    user_id: str = ""
    role: str = ""
    case_ids: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)  # tool calls, in order
    tool_results: list[tuple[str, dict[str, Any]]] = field(default_factory=list, repr=False)
    blocked: int = 0
    error: str | None = None
    wall_ms: float = 0.0

    def as_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        """JSON-safe view (API responses). Tool results are left out: the redacted trace carries their
        summaries, and the case API serves the numbers."""
        summary = self.trace.summary().model_dump(mode="json") if self.trace else None
        out: dict[str, Any] = {
            "ok": self.ok, "reply": self.reply, "reply_source": self.reply_source, "grounded": self.grounded,
            "grounding_detail": self.grounding_detail, "trace_id": self.trace_id, "session_id": self.session_id,
            "user_id": self.user_id, "role": self.role, "case_ids": list(self.case_ids), "tools": list(self.tools),
            "blocked": self.blocked, "error": self.error, "wall_ms": round(self.wall_ms, 1), "summary": summary,
        }
        if self.reply_source != REPLY_MODEL and self.model_reply:
            out["model_reply_withheld"] = True
        if include_trace:
            out["trace"] = self.trace.model_dump(mode="json") if self.trace else None
        return out


def _texts(content: Any) -> str:
    return "\n".join(p.text for p in (getattr(content, "parts", None) or [])
                     if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()


class AgentRuntime:
    """See the module docstring.

    ``build_app(recorder)`` returns the ADK ``App`` (called once, on the runtime's loop thread, on first use);
    ``describe`` returns a non-secret description of the model behind it (health endpoints, UI); ``scrub`` masks
    secrets in error text before it is shown (``AgentEndpoint.scrub``); ``prepare`` runs (in a worker thread)
    before each run, so the model knows its tool mode and which model answers before the first turn is traced.
    """

    def __init__(self, build_app: Callable[[ToolTraceRecorder], Any], *, services: Any,
                 recorder: ToolTraceRecorder | None = None, max_llm_calls: int = 24, run_timeout_s: float = 600.0,
                 describe: Callable[[], dict[str, Any]] | None = None,
                 scrub: Callable[[str], str] | None = None, prepare: Callable[[], Any] | None = None) -> None:
        self._build_app = build_app
        self._prepare = prepare  # blocking work before each run (the model's capability probe / fallback choice)
        self._scrub = scrub or (lambda s: s)  # masks secrets in error text shown to people
        self.services = services
        self.recorder = recorder or ToolTraceRecorder()
        self.max_llm_calls = max_llm_calls
        self.run_timeout_s = run_timeout_s
        self._describe = describe
        self._app: Any = None
        self._runner: Any = None
        self._sessions: dict[tuple[str, str, str], str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._atexit = False
        self.runs = 0

    # ------------------------------------------------------------------ lifecycle
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, name="wapsi-agent-loop", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
                if not self._atexit:
                    self._atexit = True
                    atexit.register(self.close)  # leave no half-finished client task behind at interpreter exit
            return self._loop

    @staticmethod
    async def _drain() -> None:
        """Cancel what is still scheduled on the loop (the model client's background tasks) and let it finish."""
        loop = asyncio.get_running_loop()
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await loop.shutdown_asyncgens()

    def close(self) -> None:
        """Stop the loop thread. A later ``run`` starts a new loop and rebuilds the app (and its sessions)."""
        with self._start_lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
            self._runner = self._app = None
            self._sessions = {}
        if loop is None or loop.is_closed():
            return
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._drain(), loop).result(timeout=5)
            except Exception:  # noqa: BLE001 - shutting down regardless
                log.debug("agent loop did not drain cleanly", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        if not loop.is_running():
            loop.close()

    @property
    def app(self) -> Any:
        """The ADK app, if a run has built it."""
        return self._app

    def describe(self) -> dict[str, Any]:
        base = self._describe() if self._describe else {}
        return {**base, "runs": self.runs, "trace_source": self.recorder.source}

    def traces(self, *, case_id: str | None = None) -> list[AgentTrace]:
        return self.recorder.traces(case_id=case_id)

    # ------------------------------------------------------------------ running
    def run(self, text: str, *, user_id: str = "officer", role: str = "analyst", name: str | None = None,
            session: str = "") -> AgentRunResult:
        """Send one officer message and wait for the agent to finish. ``session`` separates conversations of the
        same user (e.g. one per case room); the role is bound to the session by a signed grant."""
        text = (text or "").strip()
        role = (role or "analyst").strip().lower()
        user_id = (user_id or "officer").strip()[:120] or "officer"
        if not text:
            return AgentRunResult(ok=False, reply=f"{NOTICE_PREFIX} The message is empty.", reply_source=REPLY_NOTICE,
                                  error="empty message", user_id=user_id, role=role)
        if role not in ROLES:
            return AgentRunResult(ok=False, reply=f"{NOTICE_PREFIX} Unknown role {role!r}.",
                                  reply_source=REPLY_NOTICE, error="unknown role", user_id=user_id, role=role)
        loop = self._ensure_loop()
        t0 = time.perf_counter()
        with self._run_lock:
            future = asyncio.run_coroutine_threadsafe(
                self._run(text, user_id=user_id, role=role, name=name, session=session), loop)
            try:
                result = future.result(timeout=self.run_timeout_s)
            except TimeoutError:
                future.cancel()
                result = AgentRunResult(ok=False, reply_source=REPLY_NOTICE, error="timeout", user_id=user_id,
                                        role=role, reply=f"{NOTICE_PREFIX} The agent did not finish within "
                                                         f"{self.run_timeout_s:.0f} s and was stopped.")
            self.runs += 1
        result.wall_ms = (time.perf_counter() - t0) * 1000.0
        return result

    async def _ensure_app(self) -> None:
        if self._runner is None:
            from google.adk.runners import InMemoryRunner

            self._app = self._build_app(self.recorder)
            self._runner = InMemoryRunner(app=self._app)

    async def _session(self, user_id: str, role: str, name: str | None, session: str) -> str:
        key = (user_id, role, session)
        if key not in self._sessions:
            s = await self._runner.session_service.create_session(
                app_name=self._app.name, user_id=user_id, state=granted_state(user_id, role, name))
            self._sessions[key] = s.id
        return self._sessions[key]

    async def _run(self, text: str, *, user_id: str, role: str, name: str | None, session: str) -> AgentRunResult:
        from google.adk.agents.run_config import RunConfig
        from google.genai import types

        context.set_services(self.services)  # the tools and plugins of this run use this runtime's services
        if self._prepare is not None:
            try:
                await asyncio.to_thread(self._prepare)
            except Exception:  # noqa: BLE001 - the run itself reports what is wrong with the endpoint
                log.warning("agent preparation failed", exc_info=True)
        await self._ensure_app()
        session_id = await self._session(user_id, role, name, session)
        before = {t.trace_id for t in self.recorder.finished}
        events: list[Any] = []
        error: str | None = None
        try:
            async for ev in self._runner.run_async(
                    user_id=user_id, session_id=session_id,
                    new_message=types.Content(role="user", parts=[types.Part(text=text)]),
                    run_config=RunConfig(max_llm_calls=self.max_llm_calls)):
                events.append(ev)
        except Exception as exc:  # noqa: BLE001 - reported to the caller as a notice
            error = self._error_text(exc)
            log.warning("agent run failed: %s", error)

        trace = self._finished_trace(before, events)
        calls: list[str] = []
        results: list[tuple[str, dict[str, Any]]] = []
        model_reply = ""
        for ev in events:
            parts = getattr(getattr(ev, "content", None), "parts", None) or []
            for p in parts:
                if getattr(p, "function_call", None) is not None:
                    calls.append(str(p.function_call.name))
                elif getattr(p, "function_response", None) is not None:
                    r = p.function_response.response
                    results.append((str(p.function_response.name), dict(r) if isinstance(r, dict) else {}))
            if not any(getattr(p, "function_call", None) or getattr(p, "function_response", None) for p in parts):
                text_out = _texts(getattr(ev, "content", None))
                if text_out and getattr(ev, "author", "") != "user":
                    model_reply = text_out
        return self._result(trace=trace, calls=calls, results=results, model_reply=model_reply, error=error,
                            user_id=user_id, role=role, session_id=session_id)

    def _error_text(self, exc: BaseException) -> str:
        name = type(exc).__name__
        if name == "LlmCallsLimitExceededError":
            return f"step limit: the agent used all {self.max_llm_calls} model turns allowed for one request"
        return self._scrub(f"{name}: {exc}")[:300]

    def _finished_trace(self, before: set[str], events: list[Any]) -> AgentTrace | None:
        """The trace of the run that just ended. A run that raised never reaches the trace plugin's end
        callback: finish it here and seal it like any other."""
        from .tracing import persist_trace

        ids = [str(getattr(ev, "invocation_id", "") or "") for ev in events]
        new = [t for t in self.recorder.finished if t.trace_id not in before]
        trace = next((t for t in reversed(new) if t.trace_id in ids), new[-1] if new else None)
        if trace is None:
            for run_id in self.recorder.active_run_ids():
                trace = self.recorder.finish_run(run_id)
                if trace is not None:
                    try:
                        persist_trace(trace, getattr(self.services, "store", None))
                    except Exception:  # noqa: BLE001 - auditing must not hide the run's own error
                        log.exception("could not record the trace of a failed run")
        return trace

    def _result(self, *, trace: AgentTrace | None, calls: list[str], results: list[tuple[str, dict[str, Any]]],
                model_reply: str, error: str | None, user_id: str, role: str, session_id: str) -> AgentRunResult:
        from .scripted import summarize_results

        out = AgentRunResult(ok=error is None, reply=model_reply, model_reply=model_reply, trace=trace,
                             trace_id=trace.trace_id if trace else "", session_id=session_id, user_id=user_id,
                             role=role, case_ids=list(trace.case_ids) if trace else [], tools=calls,
                             tool_results=results, blocked=trace.summary().blocked if trace else 0, error=error)
        verdict = None
        if trace is not None:
            last = next((s for s in reversed(trace.steps) if s.kind == "output"), None)
            verdict = last.verifier if last is not None else None
            if any(s.kind == "model" and s.failed for s in trace.steps) and model_reply.startswith(NOTICE_PREFIX):
                out.ok, out.reply_source = False, REPLY_NOTICE  # the endpoint failed; the reply is our notice
                out.error = out.error or "the model endpoint failed"
                return out
        if error is not None:
            summary = summarize_results(results)
            out.reply_source = REPLY_NOTICE
            out.reply = (f"{NOTICE_PREFIX} The agent stopped before it finished ({error})."
                         + (f" What the tools returned so far:\n\n{summary}" if summary else ""))
            return out
        if not model_reply:
            summary = summarize_results(results)
            out.reply_source = REPLY_TOOLS if summary else REPLY_NOTICE
            out.reply = summary or f"{NOTICE_PREFIX} The model returned no reply. Please ask again."
            out.ok = bool(summary)
            return out
        if verdict is not None:
            out.grounded, out.grounding_detail = verdict.passed, verdict.detail
            if not verdict.passed:
                # numbers come from data: a reply with a figure no tool returned is not shown
                summary = summarize_results(results)
                out.reply_source = REPLY_TOOLS if summary else REPLY_NOTICE
                # the notice gives the count only: an ungrounded figure is never repeated as if it were a fact
                n = verdict.failed or 1
                head = (f"{NOTICE_PREFIX} The model's reply quoted {n} value{'s' if n != 1 else ''} that no tool "
                        "returned, so it is not shown.")
                out.reply = (f"{head} This summary is built only from the tool results:\n\n{summary}" if summary
                             else f"{head} Ask again, or open the case first so the tools can supply the figures.")
        return out
