from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# The GCP edition must never reach the network in tests: force demo mode before any app module is imported.
os.environ["WAPSI_DEMO_MODE"] = "1"
os.environ.pop("WAPSI_PROJECT", None)
# ... and never an OpenAI-compatible endpoint a developer's shell may have exported (e.g. AMD Token Factory)
for _name in ("WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_AGENT_BASE_URL", "WAPSI_AGENT_API_KEY"):
    os.environ.pop(_name, None)

pytest.importorskip("google.adk", reason="GCP extras not installed (uv sync --all-extras)")


@pytest.fixture()
def demo_services():
    """Fresh demo-mode services (showcase ledger, in-memory store, FakeLLM, RulesGuard) for one test."""
    from editions.gcp.app.config import GcpSettings
    from editions.gcp.app.services import build_services, set_services

    svc = build_services(GcpSettings(demo_mode=True))
    set_services(svc)
    yield svc
    set_services(None)


class Ctx:
    """Minimal stand-in for ADK's ToolContext (``state`` + ``user_id``)."""

    def __init__(self, user_id: str = "asha", role: str | None = "analyst", **state) -> None:
        from editions.gcp.app.identity import grant_role

        self.user_id = user_id
        self.state: dict = dict(state)
        self.agent_name = "wapsi_officer"
        self.invocation_id = "inv-test"
        if role is not None:
            grant_role(self.state, user_id, role)


@pytest.fixture()
def ctx_factory():
    return Ctx
