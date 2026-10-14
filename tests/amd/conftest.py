"""Shared fixtures for the AMD edition tests (offline: no GPU, no network, no Docker)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# A real endpoint key may be exported in the developer's shell: tests never use it and never go live.
for _name in ("WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_LLM_MODEL", "WAPSI_AGENT_BASE_URL",
              "WAPSI_AGENT_API_KEY", "WAPSI_AGENT_MODEL", "WAPSI_AGENT_BACKEND", "WAPSI_AGENT_TOOL_MODE",
              "WAPSI_AGENT_TOOL_RESULT_ROLE", "RADEON_API_KEY"):
    os.environ.pop(_name, None)

AMD = ROOT / "editions" / "amd"
BENCH_FIXTURES = AMD / "bench" / "fixtures"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "rehearsal: runs editions/amd/kickoff/run_all.sh --rehearsal stages on CPU (deselect with -m 'not rehearsal'; "
        "WAPSI_FULL_REHEARSAL=1 runs every stage)",
    )


@pytest.fixture()
def amd_dir() -> Path:
    return AMD


@pytest.fixture()
def bench_fixtures() -> Path:
    return BENCH_FIXTURES


@pytest.fixture()
def bash() -> str:
    """Path to bash, or skip (the shell scripts target Linux; Git Bash runs them on Windows)."""
    exe = shutil.which("bash")
    if sys.platform == "win32" and (exe is None or "system32" in exe.lower()):  # skip WSL's bash.exe
        git_bash = [Path(r"C:\Program Files\Git\bin\bash.exe"), Path(r"C:\Program Files\Git\usr\bin\bash.exe")]
        exe = next((str(p) for p in git_bash if p.exists()), None)
    if exe is None:
        pytest.skip("bash not available")
    return exe


@pytest.fixture()
def demo_services(tmp_path):
    """Demo-mode AMD services: showcase ledger, FileCaseStore in tmp, FakeLLM, rules guard, heuristic scorer."""
    from editions.amd.app.services import build_services, set_services
    from editions.amd.app.settings import AmdSettings

    svc = build_services(AmdSettings(demo_mode=True, state_dir=str(tmp_path / "state"), ui_seed_limit=0),
                         search=False)
    set_services(svc)
    yield svc
    set_services(None)
    _reset_agent()


def _reset_agent() -> None:
    """Close the process-wide agent runtime and unbind the agent's services (other tests bind their own)."""
    from editions.amd.app import agent as agent_mod
    from editions.shared import context

    agent_mod.set_runtime(None)
    context.set_services(None)


@pytest.fixture()
def agent_runtime(demo_services):
    """Factory: an ``AgentRuntime`` on the demo services and a fake endpoint; closed after the test."""
    pytest.importorskip("google.adk", reason="agent extra not installed (uv sync --extra agent)")
    pytest.importorskip("litellm", reason="agent extra not installed (uv sync --extra agent)")
    from editions.amd.app import agent as agent_mod
    from editions.shared.fake_openai import FakeOpenAIBackend

    made = []

    def make(fake=None, **overrides):
        fake = fake or FakeOpenAIBackend(model="Gemma-4-31B-IT")  # the id whose ADK handling the tests exercise
        model = fake.build_model(**overrides)
        rt = agent_mod.build_runtime(demo_services, fake=fake, model=model, endpoint=model.endpoint)
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close()
