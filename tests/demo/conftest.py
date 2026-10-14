from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # evals, ui, scripts and deploy are imported from the repository root

# never a live call from a test: a developer's shell may hold the real endpoint variables
LLM_ENVS = ("WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_LLM_MODEL", "WAPSI_LLM_JSON_MODE",
            "WAPSI_LLM_EXTRA_BODY", "WAPSI_LLM_PLATFORM", "WAPSI_LLM_RPM", "WAPSI_LLM_RPD",
            "WAPSI_LLM_MIN_INTERVAL_S", "WAPSI_LLM_PRICE_IN", "WAPSI_LLM_PRICE_OUT", "WAPSI_LLM_CACHE_DIR",
            "WAPSI_UI_LLM", "WAPSI_PROSE_RECORDING", "RADEON_API_KEY")


@pytest.fixture(autouse=True)
def _no_live_endpoint(monkeypatch):
    for name in LLM_ENVS:
        monkeypatch.delenv(name, raising=False)
