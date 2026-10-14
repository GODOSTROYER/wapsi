"""The env-configured OpenAI-compatible endpoint (``wapsi.adapters.llm_env``): configuration, the name drafts
record for it, and the request it sends. A mocked transport stands in for the server; no network, no real key."""

from __future__ import annotations

import json

import httpx
import pytest

from wapsi.adapters.llm_env import (
    API_KEY_ENV,
    BASE_URL_ENV,
    MODEL_ENV,
    EndpointNotConfigured,
    endpoint_config,
    make_endpoint_llm,
    model_family,
    parse_served,
    platform_for,
    resilient_endpoint_llm,
    served_name,
)
from wapsi.adapters.llm_openai_compat import LLMError, TokenUsage, endpoint_host

SENTINEL = "rc-SENTINEL-not-a-real-key-0123456789abcdef"
AMD = {BASE_URL_ENV: "https://developer.amd.com.cn/radeon/api/v1", API_KEY_ENV: SENTINEL, MODEL_ENV: "Gemma-4-31B-IT"}
MSG = [{"role": "system", "content": "Write prose."}, {"role": "user", "content": "facts"}]


def reply(text: str = '{"text": "ok"}', **usage) -> httpx.Response:
    usage = usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.000021}
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text},
                                                  "finish_reason": "stop"}], "usage": usage},
                          headers={"x-ratelimit-remaining-user-rpm": "19"})


def test_endpoint_config_reads_the_three_variables_and_holds_no_secret():
    cfg = endpoint_config(AMD)
    assert cfg.configured and cfg.has_key and cfg.host == "developer.amd.com.cn" and cfg.platform == "AMD"
    assert cfg.json_mode == "json_object" and cfg.missing == ()
    assert cfg.served_name == "Gemma-4-31B-IT on AMD @ developer.amd.com.cn"
    assert SENTINEL not in repr(cfg) and SENTINEL not in json.dumps(cfg.describe())
    assert cfg.describe()["api_key_set"] is True

    none = endpoint_config({})
    assert not none.configured and none.host is None and none.missing == (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV)
    assert endpoint_config({BASE_URL_ENV: "http://127.0.0.1:8000/v1"}).missing == (API_KEY_ENV, MODEL_ENV)
    local = endpoint_config({BASE_URL_ENV: "http://127.0.0.1:8000/v1", MODEL_ENV: "wapsi-llm"})
    assert local.configured and local.missing == () and not local.has_key  # a local vLLM needs no key
    assert local.platform is None and local.served_name == "wapsi-llm @ 127.0.0.1:8000"


def test_platform_is_stated_or_known_never_guessed():
    assert platform_for("developer.amd.com.cn") == "AMD" and platform_for("api.amd.com") == "AMD"
    assert platform_for("notamd.com") is None and platform_for("amd.com.evil.example") is None
    assert platform_for("10.0.0.5:8000") is None and platform_for("10.0.0.5:8000", "AMD") == "AMD"
    declared = endpoint_config({BASE_URL_ENV: "http://203.0.113.9:8000/v1", MODEL_ENV: "google/gemma-4-31b-it",
                                "WAPSI_LLM_PLATFORM": "AMD"})
    assert declared.served_name == "google/gemma-4-31b-it on AMD @ 203.0.113.9:8000"
    assert endpoint_host("https://user:pw@host.example:8443/v1") == "host.example:8443"  # never credentials


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError, match="WAPSI_LLM_JSON_MODE"):
        endpoint_config({**AMD, "WAPSI_LLM_JSON_MODE": "yaml"})
    with pytest.raises(ValueError, match="WAPSI_LLM_TIMEOUT_S"):
        endpoint_config({**AMD, "WAPSI_LLM_TIMEOUT_S": "soon"})
    with pytest.raises(ValueError, match="WAPSI_LLM_EXTRA_BODY"):
        endpoint_config({**AMD, "WAPSI_LLM_EXTRA_BODY": "[1]"})
    with pytest.raises(EndpointNotConfigured, match=BASE_URL_ENV):
        make_endpoint_llm(env={})


def test_served_names_round_trip():
    live = parse_served("Gemma-4-31B-IT on AMD @ developer.amd.com.cn")
    assert (live.kind, live.model, live.platform, live.host) == ("endpoint", "Gemma-4-31B-IT", "AMD",
                                                                 "developer.amd.com.cn")
    assert live.label == "Gemma 4 on AMD via developer.amd.com.cn"
    wrapped = parse_served("Gemma-4-31B-IT on AMD @ developer.amd.com.cn+resilient")
    assert wrapped.kind == "endpoint" and wrapped.host == "developer.amd.com.cn"
    rec = parse_served(served_name("Gemma-4-31B-IT", "developer.amd.com.cn", "AMD", recorded_on="2026-10-14"))
    assert (rec.kind, rec.recorded_on, rec.label) == ("recorded", "2026-10-14",
                                                     "Gemma 4 on AMD via developer.amd.com.cn")
    plain = parse_served("wapsi-llm @ 127.0.0.1:8000")
    assert plain.platform is None and plain.label == "wapsi-llm via 127.0.0.1:8000"
    assert parse_served(None) is None and parse_served("  ") is None
    assert parse_served("fake-llm").kind == "stub" and parse_served("fake-llm (demo mode)").kind == "stub"
    assert parse_served("template-fallback").kind == "template"
    assert parse_served("gemini-3.8-flash").label == "gemini-3.8-flash"
    assert parse_served("vllm-rocm:wapsi-llm").label == "wapsi-llm on AMD (vLLM-ROCm)"
    assert model_family("google/gemma-4-E4B-it") == "Gemma 4" and model_family("Qwen3.8-27B") == "Qwen3.8-27B"
    assert served_name("m", None) == "m"


def test_the_adapter_sends_the_configured_request_and_never_shows_the_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply()

    llm = make_endpoint_llm(env=AMD, transport=httpx.MockTransport(handler))
    assert llm.name == "Gemma-4-31B-IT on AMD @ developer.amd.com.cn" and llm.host == "developer.amd.com.cn"
    assert SENTINEL not in repr(llm)
    text, usage = llm.complete_with_usage(MSG, json_schema={"type": "object"})
    assert text == '{"text": "ok"}' and usage == TokenUsage(100, 20, 120, 1, 0.000021)
    assert llm.last_rate_limit == {"x-ratelimit-remaining-user-rpm": "19"}
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://developer.amd.com.cn/radeon/api/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {SENTINEL}"
    assert body["model"] == "Gemma-4-31B-IT" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"  # the Token Factory wants the system message first


def test_json_mode_none_and_extra_body():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return reply()

    env = {**AMD, "WAPSI_LLM_JSON_MODE": "none",
           "WAPSI_LLM_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}'}
    llm = make_endpoint_llm(env=env, transport=httpx.MockTransport(handler))
    llm.complete(MSG, json_schema={"type": "object"})
    assert "response_format" not in bodies[0]  # the prompt asks for JSON; the server's JSON modes are left alone
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert endpoint_config(env).describe()["extra_body_fields"] == ["chat_template_kwargs"]


def test_a_local_endpoint_without_a_key_sends_no_authorization(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, SENTINEL)  # a key in the process must not leak to another endpoint's config
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply()

    env = {BASE_URL_ENV: "http://127.0.0.1:8000/v1", MODEL_ENV: "wapsi-llm"}
    make_endpoint_llm(env=env, transport=httpx.MockTransport(handler)).complete(MSG)
    assert "authorization" not in seen[0].headers


def test_gateway_error_shapes_and_retry_after_reach_the_caller():
    answers = [
        httpx.Response(429, json={"detail": {"error": {"message": "rate limit exceeded: 20 rpm"}}},
                       headers={"Retry-After": "60"}),
        httpx.Response(400, json={"error": {"message": "does not support JSON schema output mode"}}),
    ]
    llm = make_endpoint_llm(env=AMD, transport=httpx.MockTransport(lambda request: answers.pop(0)))
    with pytest.raises(LLMError) as limited:
        llm.complete(MSG)
    assert limited.value.status_code == 429 and limited.value.retry_after_s == 60.0
    assert "rate limit exceeded" in str(limited.value) and SENTINEL not in str(limited.value)
    with pytest.raises(LLMError, match="JSON schema output mode") as bad:
        llm.complete(MSG)
    assert bad.value.status_code == 400 and bad.value.retry_after_s is None


def test_resilient_endpoint_llm_retries_429_and_serves_the_rerun_from_its_cache(tmp_path):
    """The resilience path for any OpenAI-compatible endpoint: HTTP 429 with Retry-After, then success; a rerun
    is answered from the prompt-hash cache without a request."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "too many requests"}},
                                  headers={"Retry-After": "7"})
        return reply('{"text": "hello"}')

    slept: list[float] = []

    def build():
        inner = make_endpoint_llm(env=AMD, transport=httpx.MockTransport(handler))
        return resilient_endpoint_llm(endpoint_config(AMD), inner=inner, cache_dir=tmp_path, rpm=0,
                                      sleep=slept.append)

    llm = build()
    assert llm.name == "Gemma-4-31B-IT on AMD @ developer.amd.com.cn" and llm.backend == "openai-compatible"
    assert llm.complete(MSG, json_schema={"type": "object"}) == '{"text": "hello"}'
    r = llm.report()
    assert slept == [7.0] and len(calls) == 2  # waited what the server asked for, then one retry
    assert (r["rate_limited_429"], r["retries"], r["api_calls"], r["ok"], r["errors"]) == (1, 1, 2, 1, 0)
    assert r["endpoint_host"] == "developer.amd.com.cn" and r["model_id"] == "Gemma-4-31B-IT"
    assert r["reported_cost_usd"] == 0.000021
    assert r["cost_estimate_usd"]["this_run"] == 0.000021 and "reported by the endpoint" in r["cost_estimate_usd"]["basis"]
    assert r["limits"]["rpd"] == 0  # the endpoint enforces its own daily cap
    assert SENTINEL not in json.dumps(r) and SENTINEL not in repr(llm)

    again = build()
    assert again.complete(MSG, json_schema={"type": "object"}) == '{"text": "hello"}'
    r2 = again.report()
    assert len(calls) == 2 and r2["api_calls"] == 0 and r2["cache_hits"] == 1 and r2["cache_hit_rate"] == 1.0
    assert r2["cost_estimate_usd"]["this_run"] == 0.0
    assert r2["cost_estimate_usd"]["saved_by_cache_paid_equivalent"] == 0.000021

    # the same model name on another host does not share those answers
    other = {**AMD, BASE_URL_ENV: "http://203.0.113.9:8000/v1"}
    elsewhere = resilient_endpoint_llm(endpoint_config(other), cache_dir=tmp_path, rpm=0, sleep=slept.append,
                                       inner=make_endpoint_llm(env=other, transport=httpx.MockTransport(handler)))
    elsewhere.complete(MSG, json_schema={"type": "object"})
    assert len(calls) == 3 and elsewhere.report()["cache_hits"] == 0
