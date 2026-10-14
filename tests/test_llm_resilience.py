"""ResilientLLM: prompt-hash cache, RPM/RPD limiter, 429/503 retries, usage and cost accounting (offline)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from wapsi.adapters.llm_openai_compat import LLMError, TokenUsage
from wapsi.adapters.llm_resilience import (
    QuotaExhausted,
    RateLimiter,
    ResilientLLM,
    is_daily_limit,
    is_retryable,
    paid_price,
    request_key,
    server_retry_delay,
    usage_from,
)
from wapsi.ports import LLM

MSG = [{"role": "system", "content": "Write prose."}, {"role": "user", "content": "facts"}]


class Inner:
    """Scripted inner LLM: each outcome is a reply string or an exception to raise."""

    name = "inner:test-flash"
    model = "test-flash"
    backend = "aistudio"

    def __init__(self, *outcomes, usage=None):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_usage = None
        self.usage = usage or {"prompt_token_count": 100, "candidates_token_count": 20, "thoughts_token_count": 5,
                               "total_token_count": 125}

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None):
        self.calls += 1
        out = self.outcomes.pop(0) if self.outcomes else '{"text":"ok"}'
        if isinstance(out, BaseException):
            raise out
        self.last_usage = self.usage
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make(inner, tmp_path=None, **kw):
    clock = Clock()
    kw.setdefault("rpm", 0)
    kw.setdefault("rpd", 0)
    llm = ResilientLLM(inner, cache_dir=tmp_path, sleep=clock.sleep, clock=clock, **kw)
    return llm, clock


def test_is_an_llm_and_passes_through():
    llm, _ = make(Inner('{"text":"hello"}'))
    assert isinstance(llm, LLM) and llm.name == "inner:test-flash+resilient"
    assert llm.complete(MSG, json_schema={"type": "object"}) == '{"text":"hello"}'
    r = llm.report()
    assert r["requests"] == r["api_calls"] == r["ok"] == 1 and r["cache_hits"] == 0
    assert r["tokens"] == {"prompt_tokens": 100, "output_tokens": 20, "thinking_tokens": 5, "total_tokens": 125}


def test_cache_hit_on_rerun_is_free_and_deterministic(tmp_path):
    first, _ = make(Inner('{"text":"one"}'), tmp_path)
    assert first.complete(MSG, temperature=0.0, max_tokens=700) == '{"text":"one"}'
    inner2 = Inner('{"text":"DIFFERENT"}')
    second, _ = make(inner2, tmp_path)  # a new process: same cache dir
    assert second.complete(MSG, temperature=0.0, max_tokens=700) == '{"text":"one"}'
    assert inner2.calls == 0
    r = second.report()
    assert r["cache_hits"] == 1 and r["api_calls"] == 0 and r["cache_hit_rate"] == 1.0
    assert r["tokens"]["total_tokens"] == 0 and r["tokens_served_from_cache"]["total_tokens"] == 125
    # a different request is a miss
    assert second.complete(MSG, temperature=0.2, max_tokens=700) == '{"text":"DIFFERENT"}'
    entry = next(p for p in tmp_path.rglob("*.json") if not p.name.startswith("_"))
    data = json.loads(entry.read_text(encoding="utf-8"))
    assert data["purpose"].endswith("never training data") and data["model"] == "test-flash"


def test_request_key_is_canonical():
    a = request_key("m", MSG, temperature=0.0, max_tokens=700, json_schema={"b": 1, "a": 2})
    b = request_key("m", [dict(m) for m in MSG], temperature=0, max_tokens=700, json_schema={"a": 2, "b": 1})
    assert a == b and len(a) == 64
    assert a != request_key("m2", MSG, temperature=0.0, max_tokens=700, json_schema={"a": 2, "b": 1})
    assert a != request_key("m", MSG, temperature=0.0, max_tokens=701, json_schema={"a": 2, "b": 1})


def test_429_then_success_backs_off_with_the_server_delay():
    err = LLMError("Gemini call failed: 429 RESOURCE_EXHAUSTED {'retryDelay': '7s'}", status_code=429)
    inner = Inner(err, '{"text":"after"}')
    llm, clock = make(inner)
    assert llm.complete(MSG) == '{"text":"after"}'
    assert inner.calls == 2 and clock.slept == [7.0]
    r = llm.report()
    assert r["retries"] == 1 and r["rate_limited_429"] == 1 and r["errors"] == 0 and r["backoff_wait_s"] == 7.0


def test_503_and_timeouts_are_retried_with_exponential_backoff():
    inner = Inner(LLMError("unavailable", status_code=503), TimeoutError("slow"), '{"text":"ok"}')
    llm, clock = make(inner, backoff_s=2.0)
    assert llm.complete(MSG) == '{"text":"ok"}'
    assert clock.slept == [2.0, 4.0] and llm.report()["unavailable_5xx"] == 2


def test_gives_up_after_max_retries_and_does_not_retry_client_errors():
    llm, clock = make(Inner(*[LLMError("busy", status_code=503)] * 3), max_retries=2)
    with pytest.raises(LLMError):
        llm.complete(MSG)
    assert len(clock.slept) == 2 and llm.report()["errors"] == 1
    bad = Inner(LLMError("bad request", status_code=400))
    llm, clock = make(bad)
    with pytest.raises(LLMError):
        llm.complete(MSG)
    assert bad.calls == 1 and clock.slept == []


def test_daily_quota_is_not_retried_and_later_calls_fail_fast():
    daily = LLMError("The Gemini API free-tier daily quota for m is used up; it resets at midnight Pacific time.",
                     status_code=429)
    inner = Inner(daily)
    llm, clock = make(inner)
    with pytest.raises(QuotaExhausted):
        llm.complete(MSG)
    with pytest.raises(QuotaExhausted):
        llm.complete([{"role": "user", "content": "another"}])
    assert inner.calls == 1 and clock.slept == []
    assert llm.report()["quota_exhausted"] is True


def test_rate_limiter_rpm_window_and_rpd_budget(tmp_path):
    clock = Clock()
    lim = RateLimiter(2, 3, state_path=tmp_path / "_quota.json", clock=clock, sleep=clock.sleep,
                      today=lambda: date(2026, 10, 7))
    assert lim.acquire() == 0 and lim.acquire() == 0
    waited = lim.acquire()  # third in the same minute waits for the window
    assert 59.0 < waited <= 60.1 and lim.used_today == 3 and lim.remaining_today() == 0
    with pytest.raises(QuotaExhausted):
        lim.acquire()
    again = RateLimiter(2, 3, state_path=tmp_path / "_quota.json", clock=clock, sleep=clock.sleep,
                        today=lambda: date(2026, 10, 7))
    assert again.used_today == 3  # persisted: a rerun the same Pacific day knows the budget is spent
    tomorrow = RateLimiter(2, 3, state_path=tmp_path / "_quota.json", clock=clock, sleep=clock.sleep,
                           today=lambda: date(2026, 10, 8))
    assert tomorrow.used_today == 0 and tomorrow.acquire() == 0


def test_rpd_budget_through_the_wrapper_counts_only_api_calls(tmp_path):
    inner = Inner('{"text":"a"}', '{"text":"b"}')
    llm, _ = make(inner, tmp_path, rpd=1)
    llm.complete(MSG)
    assert llm.complete(MSG) == '{"text":"a"}'  # cache hit: no budget used
    with pytest.raises(QuotaExhausted):
        llm.complete([{"role": "user", "content": "new"}])
    assert inner.calls == 1


def test_error_classification_helpers():
    genai_errors = pytest.importorskip("google.genai.errors", reason="gcp extra not installed")

    body = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"}]}}
    exc = genai_errors.ClientError(429, body)
    assert is_daily_limit(exc) and server_retry_delay(exc) == 31.0 and is_retryable(exc)
    wrapped = LLMError("giving up")
    wrapped.__cause__ = exc
    assert is_daily_limit(wrapped)
    assert not is_retryable(LLMError("bad", status_code=400)) and is_retryable(ConnectionError())


def test_usage_normalisation_and_cost_report(monkeypatch):
    monkeypatch.delenv("WAPSI_LLM_PRICE_IN", raising=False)
    monkeypatch.delenv("WAPSI_LLM_PRICE_OUT", raising=False)
    u = usage_from(TokenUsage(10, 5, 15, 1))
    assert (u.prompt_tokens, u.output_tokens, u.total_tokens) == (10, 5, 15)
    assert usage_from(None) is None and usage_from({}) is None
    assert paid_price("gemini-3.8-flash-lite") == (0.10, 0.40) and paid_price("gemini-3.8-flash") == (0.30, 2.50)
    inner = Inner('{"text":"x"}', usage={"prompt_token_count": 1_000_000, "candidates_token_count": 100_000,
                                         "total_token_count": 1_100_000})
    inner.model = "gemini-3.8-flash"
    llm, _ = make(inner)
    llm.complete(MSG)
    cost = llm.report()["cost_estimate_usd"]
    assert cost["this_run"] == 0.0  # Gemini API free tier
    assert cost["paid_tier_equivalent"] == pytest.approx(0.30 + 0.25)
    assert "free tier" in cost["basis"].lower() and "assumed" in cost["basis"]
    monkeypatch.setenv("WAPSI_LLM_PRICE_IN", "1")
    monkeypatch.setenv("WAPSI_LLM_PRICE_OUT", "2")
    assert paid_price("anything") == (1.0, 2.0)
    vertex = Inner('{"text":"x"}', usage={"prompt_token_count": 1_000_000, "total_token_count": 1_000_000})
    vertex.backend = "vertex"
    llm, _ = make(vertex)
    llm.complete(MSG)
    assert llm.report()["cost_estimate_usd"]["this_run"] == pytest.approx(1.0)


def test_env_limits(monkeypatch):
    monkeypatch.setenv("WAPSI_LLM_RPM", "7")
    monkeypatch.setenv("WAPSI_LLM_RPD", "nonsense")
    llm = ResilientLLM(Inner())
    assert llm.limiter.rpm == 7 and llm.limiter.rpd == 250


def _real_shape_429(daily: bool = False):
    """A Gemini API free-tier 429 in the shape the service returns it: a long message, then QuotaFailure, Help and
    RetryInfo last, so a message truncated to a few hundred characters no longer carries the delay."""
    from google.genai import errors as genai_errors

    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else \
        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": quota, "quotaDimensions": {"location": "global", "model": "test-flash"}, "quotaValue": "10"}]},
        {"@type": "type.googleapis.com/google.rpc.Help", "links": [
            {"description": "Learn more about Gemini API quotas", "url": "https://ai.google.dev/gemini-api/docs/rate-limits"}]},
    ]
    if not daily:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"})
    return genai_errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "details": details,
        "message": "You exceeded your current quota, please check your plan and billing details. For more information "
                   "on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current "
                   "usage, head to: https://ai.dev/usage?tab=rate-limit. \n* Quota exceeded for metric: "
                   "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 10, model: "
                   "test-flash\nPlease retry in 37.170264s."}})


def test_real_gemini_adapter_429_keeps_the_server_retry_delay():
    """Through ``GeminiLLM(max_retries=0)`` (how the eval runs it): the wrapped error must still carry RetryInfo,
    so the wait is the server's 37 s and not a blind 2 s / 4 s backoff that burns requests into the same window."""
    from wapsi.adapters.gemini_llm import GeminiLLM

    class Models:
        calls = 0

        def generate_content(self, **kw):
            Models.calls += 1
            if Models.calls == 1:
                raise _real_shape_429()
            return {"candidates": [{"content": {"parts": [{"text": '{"text":"ok"}'}]}}],
                    "usage_metadata": {"prompt_token_count": 10, "candidates_token_count": 2, "total_token_count": 12}}

    client = type("C", (), {"models": Models()})()
    llm, clock = make(GeminiLLM("test-flash", client=client, max_retries=0))
    assert llm.complete(MSG, json_schema={"type": "object"}) == '{"text":"ok"}'
    assert clock.slept == [37.0] and Models.calls == 2
    r = llm.report()
    assert r["rate_limited_429"] == 1 and r["retries"] == 1 and r["errors"] == 0 and not r["quota_exhausted"]


def test_real_gemini_adapter_daily_429_fails_fast():
    from wapsi.adapters.gemini_llm import GeminiLLM

    class Models:
        calls = 0

        def generate_content(self, **kw):
            Models.calls += 1
            raise _real_shape_429(daily=True)

    llm, clock = make(GeminiLLM("test-flash", client=type("C", (), {"models": Models()})(), max_retries=0))
    for _ in range(3):
        with pytest.raises(QuotaExhausted):
            llm.complete(MSG)
    assert Models.calls == 1 and clock.slept == [] and llm.report()["quota_exhausted"] is True


def test_retry_delay_from_the_plain_message():
    assert server_retry_delay(LLMError("429 ... Please retry in 12.5s.", status_code=429)) == 12.5


# --------------------------------------------------------------------------- any OpenAI-compatible endpoint


def test_min_interval_spaces_requests_on_a_shared_key():
    clock = Clock()
    lim = RateLimiter(0, 0, clock=clock, sleep=clock.sleep, min_interval_s=4.0)
    assert lim.acquire() == 0.0  # the first request goes at once
    clock.t += 1.5
    assert lim.acquire() == pytest.approx(2.5) and clock.slept == [pytest.approx(2.5)]
    clock.t += 10
    assert lim.acquire() == 0.0  # already far enough apart


def test_max_wait_fails_over_instead_of_blocking_a_page():
    from wapsi.adapters.llm_resilience import RateLimited

    clock = Clock()
    lim = RateLimiter(2, 0, clock=clock, sleep=clock.sleep, max_wait_s=8.0)
    lim.acquire()
    lim.acquire()
    with pytest.raises(RateLimited) as e:
        lim.acquire()  # the window is full for another minute: an interactive caller is told, not held
    assert e.value.status_code == 429 and e.value.retry_after_s == pytest.approx(60.05) and clock.slept == []
    clock.t += 55
    assert lim.acquire() == pytest.approx(5.05)  # a short wait is still taken

    llm = ResilientLLM(Inner(), rpm=1, rpd=0, clock=clock, sleep=clock.sleep, max_wait_s=1.0)
    llm.complete(MSG)
    with pytest.raises(RateLimited):
        llm.complete([*MSG, {"role": "user", "content": "again"}])
    assert llm.report()["errors"] == 1 and llm.report()["api_calls"] == 1  # nothing was sent


def test_retry_after_from_an_openai_compatible_error_is_honoured():
    limited = LLMError("giving up after 1 attempt(s): HTTP 429", status_code=429, retry_after_s=41.0)
    assert server_retry_delay(limited) == 41.0 and is_retryable(limited) and not is_daily_limit(limited)
    daily = LLMError("HTTP 429: daily cost limit of $1 reached", status_code=429, retry_after_s=3600.0)
    assert is_daily_limit(daily)  # not retried: it only resets the next day
    llm, clock = make(Inner(limited, '{"text":"ok"}'))
    assert llm.complete(MSG) == '{"text":"ok"}' and clock.slept == [41.0]


def test_cache_namespace_keeps_endpoints_apart_and_old_keys_valid():
    args = {"temperature": 0.0, "max_tokens": 700, "json_schema": None}
    plain = request_key("Gemma-4-31B-IT", MSG, **args)
    assert request_key("Gemma-4-31B-IT", MSG, namespace=None, **args) == plain  # existing caches stay valid
    a = request_key("Gemma-4-31B-IT", MSG, namespace="developer.amd.com.cn", **args)
    b = request_key("Gemma-4-31B-IT", MSG, namespace="10.0.0.5:8000", **args)
    assert len({plain, a, b}) == 3


def test_read_cache_off_asks_again_and_still_stores(tmp_path):
    inner = Inner('{"text":"one"}', '{"text":"two"}')
    first, _ = make(inner, tmp_path)
    assert first.complete(MSG) == '{"text":"one"}' and first.last_from_cache is False
    fresh, _ = make(inner, tmp_path, read_cache=False)  # "regenerate": a real call, not the stored answer
    assert fresh.complete(MSG) == '{"text":"two"}' and inner.calls == 2 and fresh.report()["cache_hits"] == 0
    later, _ = make(inner, tmp_path)
    assert later.complete(MSG) == '{"text":"two"}' and later.last_from_cache is True and inner.calls == 2


def test_endpoint_reported_cost_is_reported_not_estimated(tmp_path):
    inner = Inner(usage=TokenUsage(400, 60, 460, 1, 0.00002))
    inner.backend = "openai-compatible"
    inner.model = "Gemma-4-31B-IT"
    llm, _ = make(inner, tmp_path)
    llm.complete(MSG)
    r = llm.report()
    assert r["reported_cost_usd"] == 0.00002 and r["tokens"]["prompt_tokens"] == 400
    assert r["cost_estimate_usd"] == {
        "this_run": 0.00002, "paid_tier_equivalent": None, "saved_by_cache_paid_equivalent": 0.0,
        "price_per_1m_tokens": None, "basis": r["cost_estimate_usd"]["basis"]}
    assert "reported by the endpoint" in r["cost_estimate_usd"]["basis"]
    again, _ = make(inner, tmp_path)
    again.complete(MSG)
    assert again.report()["cost_estimate_usd"]["saved_by_cache_paid_equivalent"] == 0.00002



def test_one_shared_limiter_per_endpoint_host_only_ever_tightens():
    from wapsi.adapters.llm_resilience import reset_shared_limiters, shared_limiter

    reset_shared_limiters()
    try:
        assert shared_limiter("developer.amd.com.cn", rpm=0, min_interval_s=0.0) is None  # nothing to limit
        a = shared_limiter("developer.amd.com.cn", rpm=18, min_interval_s=3.0)
        b = shared_limiter("developer.amd.com.cn", rpm=20, min_interval_s=4.0)  # the agent asks for a longer gap
        assert a is b and (a.rpm, a.min_interval_s, a.rpd) == (18, 4.0, 0)
        assert shared_limiter("other.example.org", rpm=5, min_interval_s=0.0) is not a
    finally:
        reset_shared_limiters()


def test_the_wait_board_says_what_is_being_waited_for():
    from wapsi.adapters.llm_resilience import WaitBoard

    clock = [100.0]
    board = WaitBoard(clock=lambda: clock[0])
    assert board.current() is None
    seen = []
    board.wait(30.0, "the endpoint's rate limit", sleep=lambda s: seen.append(board.current()))
    assert seen == [(30.0, "the endpoint's rate limit")] and board.current() is None
    token = board.begin(60.0, "the endpoint to recover")
    clock[0] += 15.0
    assert board.current() == (45.0, "the endpoint to recover")
    board.end(token)
    assert board.current() is None
