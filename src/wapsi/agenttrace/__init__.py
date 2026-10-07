"""Agent trace: every tool call an agent makes, in order, with redacted arguments, a result summary, latency,
status and the safety verdicts around it (guard screening, maker-checker policy, verifier). Edition-neutral.

- ``wapsi.agenttrace.model``     ``AgentTrace`` / ``TraceStep`` / ``GuardVerdict`` / ``VerifierVerdict``, audit events
- ``wapsi.agenttrace.recorder``  ``ToolTraceRecorder`` (pure Python; the ADK edition drives it from callbacks)
- ``wapsi.agenttrace.redact``    account / phone / e-mail / name / secret redaction
- ``wapsi.agenttrace.summarize`` result summaries and verdicts taken from tool results, reply grounding
- ``wapsi.agenttrace.demo``      the recorded offline run of the showcase case (no API key needed)
"""

from wapsi.agenttrace.model import (
    SOURCE_LABELS,
    TRACE_ACTION,
    AgentTrace,
    GuardVerdict,
    TraceStep,
    TraceSummary,
    VerifierVerdict,
    bounded,
    dump_traces,
    load_traces,
    trace_event,
    trace_from_event,
    traces_from_audit,
)
from wapsi.agenttrace.recorder import TickClock, ToolTraceRecorder
from wapsi.agenttrace.redact import redact_text, redact_value

__all__ = [
    "SOURCE_LABELS",
    "TRACE_ACTION",
    "AgentTrace",
    "GuardVerdict",
    "TickClock",
    "ToolTraceRecorder",
    "TraceStep",
    "TraceSummary",
    "VerifierVerdict",
    "bounded",
    "dump_traces",
    "load_traces",
    "redact_text",
    "redact_value",
    "trace_event",
    "trace_from_event",
    "traces_from_audit",
]
