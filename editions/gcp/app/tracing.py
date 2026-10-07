"""``ToolTracePlugin``: records every ADK run of ``wapsi_officer`` as an ``AgentTrace`` (``wapsi.agenttrace``).

It is the **first** plugin of the app, so it sees every callback before any other plugin can answer it: the
officer's raw message (before the guard may rewrite it), and every tool call (before the guard may block it, so a
blocked call still gets a step, its latency and the guard's verdict). ADK runs plugin callbacks in order and stops
at the first one that returns a value; this plugin always returns ``None``, so it never changes a run.

Recorded per run (one ADK invocation): the input step, every model turn (agent, model, latency, tokens when the
model reports usage, the tool calls it asked for), every tool call (redacted args, result summary from the tool's
own result, latency, status, guard and verifier verdicts) and the reply (output screening, reply grounding).
``WapsiGuardPlugin`` reports its verdicts into the same recorder.

When a run finishes, its trace is appended to the audit log of every case it touched (``agent_trace`` events, so
the case's hash chain covers it; Firestore keeps them on the case too) and logged as one structured JSON line
(Cloud Logging). A run that touched no case is attributed to the case the session is working on, if any.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from wapsi.agenttrace import AgentTrace, ToolTraceRecorder, trace_event

from .identity import display_name, verified_role
from .services import get_services

log = logging.getLogger("wapsi.gcp.trace")
trace_log = logging.getLogger("wapsi.trace")

LAST_CASE_KEY = "wapsi:last_case_id"  # tools.LAST_CASE_KEY (not imported: keeps this module free of tool imports)

_recorder: ToolTraceRecorder | None = None
_lock = threading.Lock()


def get_recorder() -> ToolTraceRecorder:
    """The process-wide recorder shared by the trace and guard plugins."""
    global _recorder
    if _recorder is None:
        with _lock:
            if _recorder is None:
                _recorder = ToolTraceRecorder()
    return _recorder


def set_recorder(recorder: ToolTraceRecorder | None) -> None:
    global _recorder
    with _lock:
        _recorder = recorder


def _texts(content: Any) -> str:
    return " ".join(p.text for p in (getattr(content, "parts", None) or []) if getattr(p, "text", None)
                    and not getattr(p, "thought", False))


def _usage(llm_response: Any) -> tuple[int | None, int | None]:
    u = getattr(llm_response, "usage_metadata", None)
    if u is None:
        return None, None
    tin = getattr(u, "prompt_token_count", None)
    tout = getattr(u, "candidates_token_count", None)
    return (int(tin) if tin is not None else None, int(tout) if tout is not None else None)


def persist_trace(trace: AgentTrace, store: Any | None = None) -> list[str]:
    """Append ``trace`` to the audit log of each case it touched; returns the cases it was recorded on."""
    store = store if store is not None else get_services().store
    done = []
    for case_id in trace.case_ids:
        try:
            store.append_audit(case_id, trace_event(trace, case_id=case_id))
            done.append(case_id)
        except (KeyError, ValueError):
            continue  # not a stored case
        except Exception:  # noqa: BLE001 - auditing must not break the run
            log.exception("could not record the agent trace on %s", case_id)
    return done


class ToolTracePlugin(BasePlugin):
    def __init__(self, recorder: ToolTraceRecorder | None = None, *, persist: bool = True,
                 store: Any | None = None, name: str = "wapsi_trace") -> None:
        super().__init__(name=name)
        self._recorder = recorder
        self.persist = persist
        self._store = store

    @property
    def recorder(self) -> ToolTraceRecorder:
        return self._recorder or get_recorder()

    # ------------------------------------------------------------------ run boundaries
    async def on_user_message_callback(self, *, invocation_context: Any, user_message: Any) -> None:
        try:
            session = getattr(invocation_context, "session", None)
            state = getattr(session, "state", None) or {}
            user = str(getattr(invocation_context, "user_id", "") or getattr(session, "user_id", "") or "")
            self.recorder.start_run(
                str(invocation_context.invocation_id), app=str(getattr(invocation_context, "app_name", "") or ""),
                session_id=str(getattr(session, "id", "") or ""), user=display_name(state, user) if user else "",
                role=verified_role(state, user) if user else "", prompt=_texts(user_message))
        except Exception:  # noqa: BLE001 - tracing never breaks a run
            log.exception("trace: could not start the run")
        return None

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        run_id = str(getattr(invocation_context, "invocation_id", "") or "")
        trace = self.recorder.finish_run(run_id)
        if trace is None:
            return None
        try:
            if not trace.case_ids:
                state = getattr(getattr(invocation_context, "session", None), "state", None) or {}
                last = state.get(LAST_CASE_KEY)
                if isinstance(last, str) and last:
                    trace.case_ids.append(last)
            s = trace.summary()
            trace_log.info(json.dumps({
                "severity": "WARNING" if s.blocked or s.errors or s.verifier_failed else "INFO",
                "message": f"wapsi agent run {trace.trace_id}: {s.tool_calls} tool calls, {s.blocked} blocked",
                "logging.googleapis.com/labels": {"app": "wapsi", "kind": "agent_trace"},
                "wapsi_trace": {"trace_id": trace.trace_id, "user": trace.user, "role": trace.role,
                                "cases": trace.case_ids, **s.model_dump(mode="json")},
            }, ensure_ascii=False, default=str))
            if self.persist:
                persist_trace(trace, self._store)
        except Exception:  # noqa: BLE001
            log.exception("trace: could not record run %s", run_id)
        return None

    # ------------------------------------------------------------------ model turns
    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> None:
        try:
            seen = [json.dumps(p.function_response.response, ensure_ascii=False, default=str)
                    for c in (getattr(llm_request, "contents", None) or []) for p in (c.parts or [])
                    if getattr(p, "function_response", None) is not None and p.function_response.response]
            self.recorder.model_start(str(callback_context.invocation_id), agent=str(callback_context.agent_name),
                                      model=str(getattr(llm_request, "model", "") or ""), context_texts=seen)
        except Exception:  # noqa: BLE001
            log.exception("trace: model start")
        return None

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> None:
        try:
            if getattr(llm_response, "partial", False):
                return None
            run_id = str(callback_context.invocation_id)
            content = getattr(llm_response, "content", None)
            parts = getattr(content, "parts", None) or []
            calls = [p.function_call.name for p in parts if getattr(p, "function_call", None) is not None]
            tin, tout = _usage(llm_response)
            err = getattr(llm_response, "error_code", None)
            self.recorder.model_end(run_id, calls=calls, tokens_in=tin, tokens_out=tout,
                                    status="error" if err else "ok", note=str(err or ""))
            text = _texts(content)
            if text.strip() and not calls:
                self.recorder.output(run_id, text, agent=str(callback_context.agent_name))
        except Exception:  # noqa: BLE001
            log.exception("trace: model end")
        return None

    async def on_model_error_callback(self, *, callback_context: Any, llm_request: Any, error: Exception) -> None:
        try:
            self.recorder.model_end(str(callback_context.invocation_id), status="error",
                                    note=f"{type(error).__name__}")
        except Exception:  # noqa: BLE001
            log.exception("trace: model error")
        return None

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _ids(tool_context: Any) -> tuple[str, str, str]:
        return (str(getattr(tool_context, "invocation_id", "") or ""),
                str(getattr(tool_context, "function_call_id", "") or ""),
                str(getattr(tool_context, "agent_name", "") or ""))

    async def before_tool_callback(self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any) -> None:
        run_id, call_id, agent = self._ids(tool_context)
        self.recorder.tool_start(run_id, getattr(tool, "name", "?"), args=tool_args, call_id=call_id, agent=agent)
        return None

    async def after_tool_callback(self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any,
                                  result: Any) -> None:
        run_id, call_id, agent = self._ids(tool_context)
        self.recorder.tool_end(run_id, getattr(tool, "name", "?"), result=result, call_id=call_id, args=tool_args,
                               agent=agent)
        return None

    async def on_tool_error_callback(self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any,
                                     error: Exception) -> None:
        run_id, call_id, agent = self._ids(tool_context)
        self.recorder.tool_end(run_id, getattr(tool, "name", "?"), call_id=call_id, args=tool_args, agent=agent,
                               error=f"{type(error).__name__}: {error}")
        return None
