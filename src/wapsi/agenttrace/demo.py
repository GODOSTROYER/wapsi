"""Recorded agent runs of the showcase case, so the Agent trace panel works with no API key.

Two recordings, both replayed and labelled as recordings:

- ``showcase_gemma_recording.json`` (preferred): a **live** run of the real ADK app (``wapsi_officer`` with its
  guard, audit and trace plugins and the real tools) on Gemma 4 served on AMD GPUs by the AMD Token Factory, made
  with ``python -m editions.amd.app.agent_demo --case 31009260000101 --live --prose endpoint --demo --out ...`` on
  2026-10-14 (results file ``editions/amd/bench/results/agent-tokenfactory-20261014/``). Model latencies are the
  HTTP requests to that endpoint from a laptop; the pause for the endpoint's rate limit is recorded apart.
- ``showcase_recording.json``: the same app driven by the offline scripted model (``DemoScriptedLlm``), written by
  ``uv run python -m editions.gcp.app.trace_demo --write``; its latencies were measured on a laptop CPU.

Live runs in a deployment with a model configured replace both.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from wapsi.agenttrace.model import AgentTrace, load_traces

RECORDING = Path(__file__).with_name("showcase_recording.json")
LIVE_RECORDING = Path(__file__).with_name("showcase_gemma_recording.json")
LIVE_RECORDING_SOURCE = "recording-endpoint"


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


@lru_cache(maxsize=1)
def _load_live() -> tuple[AgentTrace, ...]:
    try:
        return tuple(load_traces(LIVE_RECORDING.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ()


def live_recorded_traces(case_id: str | None = None) -> list[AgentTrace]:
    """The recorded live run on Gemma 4 served on AMD (copies), oldest first; ``case_id`` keeps the runs that
    touched that case. Each is marked ``source = "recording-endpoint"``: replayed, never presented as live."""
    out = [t.model_copy(deep=True) for t in _load_live() if case_id is None or case_id in t.case_ids]
    for t in out:
        t.source = LIVE_RECORDING_SOURCE
    return out
