"""Shared test setup: no test reaches a model endpoint because of what the developer's shell holds.

Voice intake picks its models from the environment (``WAPSI_VOICE_BACKEND``, ``WAPSI_LLM_*``, ``WAPSI_ASR_*``, the
Gemini key). A shell that has sourced a real endpoint and key would otherwise switch the Voice Intake page and
the AMD edition to live models in the middle of a test run. Every such variable is removed before each test (a
test that needs one sets it with ``monkeypatch``), and the process-wide model-health and audio-probe caches start
empty.
"""

from __future__ import annotations

import pytest

VOICE_ENV = (
    "WAPSI_VOICE_BACKEND", "WAPSI_VOICE_OFFLINE", "WAPSI_VOICE_LLM_MODEL", "WAPSI_VOICE_LLM_MODELS",
    "WAPSI_VOICE_JSON_MODE", "WAPSI_VOICE_MODEL", "WAPSI_LLM_BASE_URL", "WAPSI_LLM_MODEL", "WAPSI_LLM_MODELS",
    "WAPSI_LLM_API_KEY", "WAPSI_LLM_ON_AMD", "WAPSI_LLM_PROVIDER", "WAPSI_LLM_COOLDOWN_S",
    "WAPSI_LLM_HEALTH_TTL_S", "WAPSI_LLM_HEALTH_PROBE", "WAPSI_ASR_BACKEND", "WAPSI_ASR_BASE_URL",
    "WAPSI_ASR_API_KEY", "WAPSI_ASR_MODEL", "WAPSI_ASR_LANGUAGE", "WAPSI_ASR_PROMPT", "WAPSI_ASR_DEVICE",
    "WAPSI_ASR_TIMEOUT_S", "WAPSI_ASR_CHAT_AUDIO", "WAPSI_ASR_ON_AMD", "RADEON_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)  # fmt: skip


@pytest.fixture(autouse=True)
def offline_voice_env(monkeypatch, tmp_path_factory):
    for name in VOICE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAPSI_ASR_PROBE_CACHE", str(tmp_path_factory.getbasetemp() / "chat-audio-probe.json"))
    try:
        from wapsi.intake import backends, llm_chain
    except ImportError:  # a partial install: nothing to reset
        yield
        return
    llm_chain.HEALTH.reset()
    backends.reset_probe_cache()
    yield
    llm_chain.HEALTH.reset()
    backends.reset_probe_cache()
