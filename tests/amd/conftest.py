"""Shared fixtures for the AMD edition tests (offline: no GPU, no network, no Docker)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

AMD = ROOT / "editions" / "amd"
BENCH_FIXTURES = AMD / "bench" / "fixtures"


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
