"""The live smoke script's health probe is always live: a cached healthy answer never hides a degraded model.

The script itself (``pitch/amd/evidence/token_factory_smoke.py``) is not a test and goes live; here it runs against
an in-process ``httpx.MockTransport`` endpoint, so nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pitch" / "amd" / "evidence" / "token_factory_smoke.py"
BASE = "http://smoke.test/v1"


def _load():
    spec = importlib.util.spec_from_file_location("token_factory_smoke_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def smoke(monkeypatch):
    module = _load()
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)  # the 4-second spacing is for the real endpoint
    monkeypatch.setenv("WAPSI_LLM_BASE_URL", BASE)
    monkeypatch.setenv("WAPSI_LLM_API_KEY", "test-not-a-key")
    monkeypatch.setenv("WAPSI_LLM_MODEL", "Gemma-4-31B-IT")
    return module


def _endpoint(reply: str, finish: str, log: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "Gemma-4-31B-IT"}]})
        return httpx.Response(200, json={
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}})
    return httpx.MockTransport(handler)


HEALTHY = ("Wapsi returns the money.", "stop")
GARBAGE = ("<|tool_call>" + "/" * 60, "length")


def test_probe_is_never_answered_from_the_cache(smoke, tmp_path, capsys):
    cache = tmp_path / "cache"
    healthy_log: list[str] = []
    assert smoke.main(["--probe-only", "--cache", str(cache)], inner=_endpoint(*HEALTHY, healthy_log)) == 0
    assert "health probe: ok" in capsys.readouterr().out
    assert healthy_log == ["GET /v1/models", "POST /v1/chat/completions"]
    assert not list(cache.glob("*.json")), "the model list and the probe must not be cached"

    garbage_log: list[str] = []
    code = smoke.main(["--probe-only", "--cache", str(cache)], inner=_endpoint(*GARBAGE, garbage_log))
    out = capsys.readouterr().out
    assert code == 3
    assert "DEGENERATE REPLY" in out and "finish_reason=length" in out
    assert garbage_log == ["GET /v1/models", "POST /v1/chat/completions"], "both requests reached the endpoint"


def test_a_cache_primed_by_an_old_run_cannot_answer_the_probe(smoke, tmp_path, monkeypatch):
    """Cache files written for the exact probe requests (the old script cached them) are ignored."""
    cache = tmp_path / "cache"
    cache.mkdir()
    log: list[str] = []
    with monkeypatch.context() as old_behaviour:
        old_behaviour.setattr(smoke, "UNCACHED_KINDS", frozenset())
        transport = smoke.RecordingTransport(cache, 0.0, inner=_endpoint(*HEALTHY, log))
        with httpx.Client(transport=transport) as client:
            assert smoke.health_probe(client, BASE, "Gemma-4-31B-IT", transport)["ok"]
    assert len(list(cache.glob("*.json"))) == 2, "the old behaviour cached the model list and the probe"
    bad: list[str] = []
    transport = smoke.RecordingTransport(cache, 0.0, inner=_endpoint(*GARBAGE, bad))
    with httpx.Client(transport=transport) as client:
        probe = smoke.health_probe(client, BASE, "Gemma-4-31B-IT", transport)
    assert not probe["ok"] and not probe["cached"]
    assert len(bad) == 2


def test_other_replies_are_cached_and_a_replay_is_labelled_not_measured(smoke, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    first: list[str] = []
    transport = smoke.RecordingTransport(cache, 0.0, inner=_endpoint(*HEALTHY, first))
    payload = {"model": "Gemma-4-31B-IT", "messages": [{"role": "user", "content": "hello there friend"}]}
    with httpx.Client(transport=transport) as client:
        transport.kind = "tool_call"
        smoke._chat(client, BASE, payload)
    assert len(list(cache.glob("*.json"))) == 1
    stored = json.loads(next(cache.glob("*.json")).read_text(encoding="utf-8"))
    assert stored["record"]["measured_at_utc"][:4].isdigit()

    second: list[str] = []
    transport = smoke.RecordingTransport(cache, 0.0, inner=_endpoint(*GARBAGE, second))
    with httpx.Client(transport=transport) as client:
        transport.kind = "tool_call"
        smoke._chat(client, BASE, payload)
    assert second == [] and transport.calls[-1]["cached"]
    assert smoke.replay_dates(transport.calls) == [stored["record"]["measured_at_utc"][:10]]

    fresh: list[str] = []
    transport = smoke.RecordingTransport(cache, 0.0, inner=_endpoint(*HEALTHY, fresh), read_cache=False)
    with httpx.Client(transport=transport) as client:
        transport.kind = "tool_call"
        smoke._chat(client, BASE, payload)
    assert fresh == ["POST /v1/chat/completions"], "--fresh sends the request live"


def test_the_tool_follow_up_echoes_a_plain_openai_assistant_turn(smoke, tmp_path):
    """A provider's extra response fields are not request fields: the follow-up sends role, content and tool_calls."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "reasoning_content": None, "refusal": None, "annotations": [],
                "tool_calls": [{"id": "call_1", "type": "function", "index": 0,
                                "function": {"name": "hold_plan", "arguments": '{"ncrp_id": "31009260000101"}'}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "Held ₹1,42,848.00; recoverable ₹3,34,848.00."}}]})

    transport = smoke.RecordingTransport(tmp_path, 0.0, inner=httpx.MockTransport(handler), read_cache=False)
    with httpx.Client(transport=transport) as client:
        out = smoke.run_tools(client, BASE, "Gemma-4-31B-IT", transport)
    assistant = seen[1]["messages"][2]
    assert set(assistant) == {"role", "content", "tool_calls"} and assistant["content"] == ""
    assert set(assistant["tool_calls"][0]) == {"id", "type", "function"}
    assert seen[1]["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": seen[1]["messages"][3]["content"]}
    assert out["tool_name"] == "hold_plan" and out["arguments_valid"] and out["reply_grounded"]
