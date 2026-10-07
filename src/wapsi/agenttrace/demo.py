"""The recorded offline agent run of the showcase case, so the Agent trace panel works with no API key.

``showcase_recording.json`` is written by ``uv run python -m editions.gcp.app.trace_demo --write``: the real ADK
app (``wapsi_officer`` with its guard, audit and trace plugins and the real tools) driven by the offline scripted
model (``DemoScriptedLlm``) through a fixed officer script on the showcase ledger. Its latencies were measured on
a laptop CPU during that recording; nothing in it comes from Gemini. With a Gemini key, live runs replace it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from wapsi.agenttrace.model import AgentTrace, load_traces

RECORDING = Path(__file__).with_name("showcase_recording.json")


@lru_cache(maxsize=1)
def _load() -> tuple[AgentTrace, ...]:
    try:
        return tuple(load_traces(RECORDING.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ()


def recorded_traces(case_id: str | None = None) -> list[AgentTrace]:
    """Recorded showcase traces (copies), oldest first; ``case_id`` keeps the runs that touched that case."""
    out = [t.model_copy(deep=True) for t in _load() if case_id is None or case_id in t.case_ids]
    for t in out:
        t.source = "recording"
    return out
