"""AMD kickoff, the three execution modes (endpoint, notebook, droplet): the metered endpoint client, the model plan,
mode detection, the notebook, and one evidence bundle that feeds ``fill_pitch.py`` whatever the mode.

Offline: the "endpoint" is an in-memory fake shaped like AMD Token Factory behind ``httpx.MockTransport`` (model
cards with prices, ``/usage``, ``usage.cost``, rate-limit headers, HTTP 429). No GPU, no Docker, no network, no real
key. Every "measurement" below times the fake and is asserted only for shape, never reported.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.amd.kickoff import (  # noqa: E402
    collect_evidence,
    endpoint,
    fill_pitch,
    make_notebook,
    model_plan,
    stagelog,
)

KICK = ROOT / "editions" / "amd" / "kickoff"
TF_URL = "https://developer.amd.com.cn/radeon/api/v1"  # the real host name; the transport below never leaves the process
TF_HOST = "developer.amd.com.cn"
KEY = "rc-TESTONLY-never-print-this-key-0123456789"
GEMMA, JUDGE = "Gemma-4-31B-IT", "MiniCPM5-2B"
PRICE_IN, PRICE_OUT = 3.5e-8, 7e-8
STAGES = ["env", "models", "gnn", "lora-data", "lora-train", "merge", "lora-eval", "serve", "eval", "bench", "ab",
          "evidence"]


# --------------------------------------------------------------------------- a fake Token Factory


class FakeTokenFactory:
    """OpenAI-compatible endpoint in memory, shaped like AMD Token Factory.

    Drafting requests are answered by the offline ``FakeLLM`` (prose with placeholders, as JSON); the raw arm of the
    A/B and the harness's LLM-only baseline get a free-form answer with one wrong amount, one invented account and a
    repealed citation, the way an unbound model fails. A strict JSON schema is refused with HTTP 400, as the real
    endpoint does."""

    def __init__(self, models=(GEMMA, JUDGE, "Qwen3.8-27B"), *, key: str = KEY, fail_429: int = 0,
                 retry_after: str = "0") -> None:
        from wapsi.adapters.llm_fake import FakeLLM

        self.models, self.key, self.fail_429, self.retry_after = list(models), key, fail_429, retry_after
        self.llm = FakeLLM(name="fake-token-factory")
        self.chats: list[dict] = []
        self.seen_auth: set[str | None] = set()
        self.lock = threading.Lock()

    def _text(self, body: dict) -> str:
        from editions.amd.kickoff.ab_hallucination import RawStubLLM
        from editions.amd.lora.mock_endpoint import _complete_text

        last = str(body["messages"][-1]["content"])
        if "Case facts (JSON)" in last:  # the A/B's raw arm
            return RawStubLLM(hallucinate=True).complete(body["messages"])
        if "Write a short case summary with the amounts" in str(body["messages"][0]["content"]):  # harness baseline
            return "The victim lost ₹9,99,999.37 in total across the accounts."
        if "Reply with exactly this JSON" in last:  # endpoint.py probe
            return '{"ok": true}'
        return _complete_text(self.llm, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        auth = request.headers.get("authorization")
        with self.lock:
            self.seen_auth.add(auth)
        if auth != f"Bearer {self.key}":
            return httpx.Response(401, json={"detail": {"error": {"message": "Invalid API key", "code": "invalid_key"}}})
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "name": m, "pricing": {"prompt": str(PRICE_IN), "completion": str(PRICE_OUT)},
                 "context_length": 262144, "json_output": True, "structured_outputs": False,
                 "providers": [{"tools": True}], "architecture": {"input_modalities": ["text", "image"]}}
                for m in self.models]})
        if request.method == "GET" and path.endswith("/usage"):
            return httpx.Response(200, json={"organization_id": "org-SECRET-ACCOUNT-ID", "rpm_limit": 20,
                                             "daily_cost_limit_usd": 1, "daily_cost_used_usd": 0.01,
                                             "daily_cost_remaining_usd": 0.99,
                                             "daily_reset_at": "2026-10-14T16:00:00.000Z"})
        if request.method != "POST" or not path.endswith("/chat/completions"):
            return httpx.Response(404, json={"error": {"message": f"no route {path}"}})
        body = json.loads(request.content)
        with self.lock:
            self.chats.append(body)
            limited = self.fail_429 > 0
            self.fail_429 -= int(limited)
        limits = {"x-ratelimit-limit-user-rpm": "20", "x-ratelimit-remaining-user-rpm": "0" if limited else "12",
                  "x-ratelimit-remaining-user-daily-usd": "0.98"}
        if limited:
            return httpx.Response(429, headers={**limits, "retry-after": self.retry_after},
                                  json={"error": {"message": "Rate limit exceeded: maximum 20 requests per minute",
                                                  "code": "rate_limit_exceeded"}})
        if (body.get("response_format") or {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "this model does not support JSON schema output mode"}})
        text = self._text(body)
        n_in = sum(len(str(m["content"]).split()) for m in body["messages"])
        n_out = max(1, len(text.split()))
        usage = {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out,
                 "cost": n_in * PRICE_IN + n_out * PRICE_OUT}
        if body.get("stream"):
            base = {"id": "c1", "object": "chat.completion.chunk", "model": body["model"]}
            events = [{**base, "choices": [{"index": 0, "delta": {"content": w + " "}}]} for w in text.split()]
            events.append({**base, "choices": [], "usage": {k: v for k, v in usage.items() if k != "cost"}})
            events.append({**base, "choices": [{"index": 0, "delta": {}}], "usage": usage})  # the gateway's cost chunk
            sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=sse, headers={**limits, "content-type": "text/event-stream"})
        return httpx.Response(200, headers=limits, json={
            "id": "c1", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture()
def tf(monkeypatch, tmp_path):
    """A fake Token Factory and the environment endpoint mode reads (the cache in tmp, never the repository's)."""
    for k in [k for k in os.environ if k.startswith(("WAPSI_", "VLLM_", "RADEON_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("WAPSI_LLM_BASE_URL", TF_URL)
    monkeypatch.setenv("WAPSI_LLM_API_KEY", KEY)
    monkeypatch.setenv("WAPSI_LLM_MODEL", GEMMA)
    monkeypatch.setenv("WAPSI_LLM_CACHE_DIR", str(tmp_path / "llm-cache"))
    return FakeTokenFactory()


def _files_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- rate limiter


class Clock:
    def __init__(self) -> None:
        self.now, self.slept = 1000.0, []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_rate_limiter_keeps_the_window_the_gap_and_the_penalty():
    c = Clock()
    rl = endpoint.RateLimiter(rpm=3, max_concurrency=2, clock=c.time, sleep=c.sleep)
    for _ in range(3):
        with rl.slot():
            c.now += 1.0  # each request takes a second
    assert c.slept == [] and rl.room_wait(1) == pytest.approx(57.0)  # the 4th start waits for the 1st to age out
    assert rl.room_wait(3) == pytest.approx(59.0)  # a burst of 3 needs all three earlier starts out of the window
    with rl.slot():
        pass
    assert c.slept == [57.0] and rl.stats()["waits"] == 1 and rl.stats()["starts"] == 4
    rl.penalize(30.0)  # an HTTP 429's Retry-After holds every new start
    assert rl.room_wait(1) >= 30.0
    unlimited = endpoint.RateLimiter(rpm=0, clock=c.time, sleep=c.sleep)
    for _ in range(50):
        unlimited.acquire()
        unlimited.release()
    assert unlimited.room_wait(40) == 0.0 and unlimited.stats()["rpm"] is None
    gap = endpoint.RateLimiter(rpm=0, min_interval_s=4.0, clock=c.time, sleep=c.sleep)
    gap.acquire(); gap.release(); gap.acquire(); gap.release()  # noqa: E702
    assert c.slept[-1] == 4.0


def test_rate_limiter_caps_concurrency():
    rl = endpoint.RateLimiter(rpm=0, max_concurrency=2)
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Event()

    def work() -> None:
        with rl.slot():
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            gate.wait(0.05)
            with lock:
                state["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert state["peak"] == 2


# --------------------------------------------------------------------------- the metered client


def test_endpoint_defaults_follow_the_host():
    assert endpoint.host_of(TF_URL) == TF_HOST and endpoint.host_of("http://127.0.0.1:8000/v1") == "127.0.0.1:8000"
    tf_prov = endpoint.provider_of(TF_URL, {})
    assert tf_prov["kind"] == "amd-token-factory" and tf_prov["amd_served"] is True and tf_prov["gpu_side"] == "provider"
    assert endpoint.default_limits(TF_URL, {}) == (16.0, 4, 0.0)  # below the trial's 20 a minute and 8 at once
    assert endpoint.default_limits(TF_URL, {"WAPSI_LLM_RPM": "6", "WAPSI_LLM_MAX_CONCURRENCY": "1"}) == (6.0, 1, 0.0)
    assert endpoint.default_json_mode(TF_URL, {}) == "json_object"
    local = endpoint.provider_of("http://127.0.0.1:8000/v1", {})
    assert local["kind"] == "self-hosted" and local["amd_served"] is None and local["gpu_side"] == "local"
    assert endpoint.default_limits("http://127.0.0.1:8000/v1", {})[0] == 0.0  # your own vLLM is not rate-limited
    assert endpoint.default_json_mode("http://127.0.0.1:8000/v1", {}) == "json_schema"
    other = endpoint.provider_of("https://llm.example.org/v1", {})
    assert other["kind"] == "other" and other["amd_served"] is False
    declared = endpoint.provider_of("https://llm.example.org/v1", {"WAPSI_ENDPOINT_HARDWARE": "AMD Instinct MI300X, vLLM-ROCm"})
    assert declared["amd_served"] is True and "declared by the operator" in declared["basis"]
    assert endpoint.provider_of("https://notamd.com.evil.org/v1", {})["kind"] == "other"  # a look-alike host
    label = endpoint.measured_label(TF_URL, GEMMA, "2026-10-14", {})
    assert label.startswith("AMD Token Factory, Gemma-4-31B-IT, measured 2026-10-14")
    assert "provider-side AMD GPUs" in label and "not an MI300X droplet benchmark" in label
    assert endpoint.api_key({"RADEON_API_KEY": "r", "VLLM_API_KEY": "v"}) == "r" and endpoint.api_key({}) is None


def test_metered_llm_shapes_requests_retries_429_meters_cost_and_caches(tf, tmp_path):
    tf.fail_429, tf.retry_after = 1, "7"
    c = Clock()
    limiter = endpoint.RateLimiter(rpm=0, clock=c.time, sleep=c.sleep)
    llm = endpoint.MeteredLLM.from_env(transport=tf.transport, limiter=limiter, sleep=c.sleep, jitter=0.0)
    assert llm.json_mode == "json_object" and llm.name == f"{GEMMA}@{TF_HOST}"
    msgs = [{"role": "user", "content": "Summarise the case."}, {"role": "system", "content": "A."},
            {"role": "system", "content": "B."}]
    text = llm.complete(msgs, max_tokens=64, json_schema={"type": "object", "additionalProperties": False})
    assert json.loads(text)["text"]
    assert len(tf.chats) == 2  # the 429, then the accepted retry
    sent = tf.chats[-1]
    assert sent["response_format"] == {"type": "json_object"}  # never a strict schema on this endpoint
    assert sent["messages"][0] == {"role": "system", "content": "A.\n\nB."}  # one system message, first
    assert [m["role"] for m in sent["messages"]] == ["system", "user"] and sent["model"] == GEMMA
    assert c.slept == [7.0]  # Retry-After honoured ...
    assert limiter._not_before == pytest.approx(1007.0)  # ... and every other thread held for the same 7 s
    rep = llm.report()
    assert (rep["calls"], rep["cache_hits"], rep["rate_limited_429"], rep["retries"], rep["errors"]) == (1, 0, 1, 1, 0)
    expected = sent_cost = rep["prompt_tokens"] * PRICE_IN + rep["completion_tokens"] * PRICE_OUT
    assert rep["cost_usd"] == pytest.approx(expected, abs=1e-8) and sent_cost > 0 and rep["cost_reported_calls"] == 1
    assert rep["endpoint_host"] == TF_HOST and rep["provider"]["kind"] == "amd-token-factory"
    assert rep["ratelimit_last_seen"]["remaining_rpm"] == 12.0 and rep["latency_s"]["n"] == 1

    again = llm.complete(msgs, max_tokens=64, json_schema={"type": "object", "additionalProperties": False})
    assert again == text and len(tf.chats) == 2  # the same request: answered from the disk cache, no call
    assert llm.report()["cache_hits"] == 1 and llm.report(since=llm.mark())["calls"] == 0
    fresh = endpoint.MeteredLLM.from_env(transport=tf.transport)  # a new process finds the same cache
    assert fresh.complete(msgs, max_tokens=64, json_schema={"type": "object", "additionalProperties": False}) == text
    assert len(tf.chats) == 2
    llm.complete(msgs, temperature=0.7, max_tokens=64)  # sampled requests are never cached
    llm.complete(msgs, temperature=0.7, max_tokens=64)
    assert len(tf.chats) == 4
    no_cache = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False)  # benchmarks time every call
    no_cache.complete(msgs, max_tokens=64, json_schema={"type": "object", "additionalProperties": False})
    assert len(tf.chats) == 5

    # request fields a served model needs come from the environment, never over the fields the client controls
    extra = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False, env={
        **os.environ, "WAPSI_LLM_EXTRA_BODY": '{"reasoning_effort": "low", "model": "other", "stream": true}'})
    extra.complete(msgs, max_tokens=8)
    assert tf.chats[-1]["reasoning_effort"] == "low" and tf.chats[-1]["model"] == GEMMA and "stream" not in tf.chats[-1]
    with pytest.raises(SystemExit):
        endpoint.MeteredLLM.from_env(transport=tf.transport, env={**os.environ, "WAPSI_LLM_EXTRA_BODY": "[1]"})

    assert tf.seen_auth == {f"Bearer {KEY}"}
    out = tmp_path / "meter.json"
    llm.write_report(out)
    for blob in (repr(llm), json.dumps(rep), out.read_text(encoding="utf-8"), _files_text(tmp_path / "llm-cache")):
        assert KEY not in blob  # the key is a header, nothing else


def test_metered_llm_raises_at_once_on_client_errors_and_reads_both_error_shapes(tf, monkeypatch):
    slept: list[float] = []
    llm = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False, sleep=slept.append, jitter=0.0)
    monkeypatch.setenv("WAPSI_LLM_JSON_MODE", "json_schema")
    strict = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False, sleep=slept.append)
    with pytest.raises(endpoint.EndpointError) as bad:  # {"error": {...}}: a 400 is never retried
        strict.complete([{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert bad.value.status_code == 400 and "does not support JSON schema" in str(bad.value) and slept == []
    monkeypatch.setenv("WAPSI_LLM_API_KEY", "rc-wrong")
    wrong = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False, sleep=slept.append)
    with pytest.raises(endpoint.EndpointError) as denied:  # {"detail": {"error": {...}}}
        wrong.complete([{"role": "user", "content": "x"}])
    assert denied.value.status_code == 401 and "Invalid API key" in str(denied.value)
    assert "check WAPSI_LLM_API_KEY" in str(denied.value) and "rc-wrong" not in str(denied.value)
    assert wrong.report()["errors"] == 1 and wrong.report()["calls"] == 0
    tf.fail_429 = 99  # the queue never clears: give up after the retries, as a recorded error
    capped = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False, sleep=slept.append, jitter=0.0,
                                          max_retries=2, env={**os.environ, "WAPSI_LLM_API_KEY": KEY})
    with pytest.raises(endpoint.EndpointError) as gave_up:
        capped.complete([{"role": "user", "content": "x"}])
    assert gave_up.value.status_code == 429 and "giving up after 3 attempt(s)" in str(gave_up.value)
    assert capped.report()["rate_limited_429"] == 3 and len(slept) == 2
    assert llm.report()["calls"] == 0


def test_probe_identifies_the_endpoint_without_keeping_account_ids(tf):
    info = endpoint.probe(transport=tf.transport)
    assert info["ok"] is True and info["endpoint_host"] == TF_HOST and info["model"] == GEMMA
    assert info["model_served"] and info["models"] == [GEMMA, JUDGE, "Qwen3.8-27B"]
    assert info["provider"]["kind"] == "amd-token-factory" and info["gpu"]["side"] == "provider"
    card = info["model_card"]
    assert card["price_prompt_usd_per_token"] == PRICE_IN and card["structured_outputs"] is False
    assert card["context_length"] == 262144 and card["tools"] is True
    assert info["limits"] == {"rpm_limit": 20, "daily_cost_limit_usd": 1, "daily_cost_used_usd": 0.01,
                              "daily_cost_remaining_usd": 0.99, "daily_reset_at": "2026-10-14T16:00:00.000Z"}
    assert info["probe"]["ok"] and info["probe"]["json_object_ok"] and info["probe"]["cost_usd"] > 0
    blob = json.dumps(info)
    assert KEY not in blob and "org-SECRET-ACCOUNT-ID" not in blob and info["api_key_set"] is True
    assert len(tf.chats) == 1  # two GETs and one short completion

    missing = endpoint.probe(transport=tf.transport, model="Gemma-9-Ultra")
    assert missing["ok"] is False and "is not served" in missing["error"] and len(tf.chats) == 1
    assert endpoint.probe(env={})["ok"] is False  # no base URL: recorded, not raised
    denied = endpoint.probe(transport=tf.transport, env={"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": "rc-wrong"})
    assert denied["ok"] is False and "401" in denied["error"] and "rc-wrong" not in json.dumps(denied)


def test_preflight_rows_for_endpoint_mode(tf):
    rows = {c: (s, d) for c, s, d in endpoint.check_rows(transport=tf.transport)}
    assert rows["endpoint base URL"][0] == "PASS" and "AMD Token Factory" in rows["endpoint base URL"][1]
    assert rows["endpoint API key"] == ("PASS", "set in this shell (value not shown)")
    assert rows["endpoint /models"] == ("PASS", "3 models served") and rows["model served"] == ("PASS", GEMMA)
    assert rows["JSON mode"] == ("PASS", "json_object") and rows["endpoint limits"][0] == "PASS"
    assert KEY not in json.dumps(rows) and tf.chats == []  # the check makes no completion
    env = {"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": KEY, "WAPSI_LLM_MODEL": "Nope-1B",
           "WAPSI_LLM_JSON_MODE": "json_schema"}
    bad = {c: s for c, s, _ in endpoint.check_rows(env=env, transport=tf.transport)}
    assert bad["model served"] == "FAIL"
    strict = {c: s for c, s, _ in endpoint.check_rows(env={**env, "WAPSI_LLM_MODEL": GEMMA}, transport=tf.transport)}
    assert strict["JSON mode"] == "FAIL"  # no strict schema on this model: json_object is the mode
    assert [s for _, s, _ in endpoint.check_rows(env={})] == ["FAIL"]  # no base URL
    nokey = {c: s for c, s, _ in endpoint.check_rows(env={"WAPSI_LLM_BASE_URL": TF_URL}, offline=True)}
    assert nokey["endpoint API key"] == "FAIL" and nokey["endpoint /models"] == "SKIP"
    denied = {c: (s, d) for c, s, d in endpoint.check_rows(
        env={"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": "rc-wrong"}, transport=tf.transport)}
    assert denied["endpoint /models"][0] == "FAIL" and "check the key" in denied["endpoint /models"][1]


def test_a_model_that_left_the_catalogue_is_waited_for_or_replaced_only_on_request():
    """Seen live on 2026-10-14: Gemma-4-31B-IT was served, then GET /models no longer listed it and a call to it
    answered HTTP 400. The named model is never swapped silently and never called while it is not served."""
    tf = FakeTokenFactory(models=("Qwen3.8-27B", JUDGE, "DeepSeek-V4-Flash"))
    env = {"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": KEY, "WAPSI_LLM_MODEL": GEMMA}
    rows = {c: (s, d) for c, s, d in endpoint.check_rows(env=env, transport=tf.transport)}
    assert rows["model served"][0] == "FAIL" and "Gemma-4-31B-IT is not served right now" in rows["model served"][1]
    assert "WAPSI_ALLOW_MODEL_FALLBACK=1" in rows["model served"][1] and "Qwen3.8-27B" in rows["model served"][1]
    info = endpoint.probe(env=env, transport=tf.transport)
    assert info["ok"] is False and "is not served right now" in info["error"] and tf.chats == []  # no call was made

    allow = {**env, "WAPSI_ALLOW_MODEL_FALLBACK": "1"}
    rows = {c: (s, d) for c, s, d in endpoint.check_rows(env=allow, transport=tf.transport)}
    assert rows["model served"][0] == "WARN" and "the model plan uses Qwen3.8-27B" in rows["model served"][1]
    assert "not a Gemma model" in rows["model served"][1] and rows["JSON mode"][0] == "PASS"
    info = endpoint.probe(env=allow, transport=tf.transport)
    assert info["ok"] is True and info["model"] == "Qwen3.8-27B" and info["model_requested"] == GEMMA
    assert info["model_fallback"] is True and tf.chats[-1]["model"] == "Qwen3.8-27B"
    # nobody named a model: the list decides, and says that Gemma was not there
    free = {c: (s, d) for c, s, d in endpoint.check_rows(env={k: v for k, v in env.items() if k != "WAPSI_LLM_MODEL"},
                                                         transport=tf.transport)}
    assert free["model served"][0] == "WARN" and "Gemma-4-31B-IT is not served right now" in free["model served"][1]

    # wait-model: polls GET /models (no completion) until the model is back
    c, said, calls = Clock(), [], len(tf.chats)

    def nap(seconds: float) -> None:
        c.sleep(seconds)
        if len(c.slept) == 2:
            tf.models.append(GEMMA)  # Model Ops brought it back

    assert endpoint.wait_model(env=env, transport=tf.transport, timeout_s=600, interval_s=30, sleep=nap, clock=c.time,
                               say=said.append) is True
    assert c.slept == [30, 30] and "is served by developer.amd.com.cn" in said[-1] and len(tf.chats) == calls
    assert endpoint.wait_model(env=env, model="Gemma-9-Ultra", transport=tf.transport, timeout_s=60, interval_s=30,
                               sleep=c.sleep, clock=c.time, say=said.append) is False
    assert endpoint.wait_model(env={}, say=said.append) is False and KEY not in " ".join(said)


def test_a_model_that_is_listed_but_refuses_calls_counts_as_not_served():
    """Also seen live on 2026-10-14, minutes later: GET /models listed Gemma-4-31B-IT again while a call to it was
    still answered HTTP 400 "Unsupported model". Listed is not the same as answering."""

    class Flapping(FakeTokenFactory):
        refusing = True

        def handler(self, request):
            if self.refusing and request.method == "POST" and json.loads(request.content)["model"] == GEMMA:
                self.chats.append(json.loads(request.content))
                return httpx.Response(400, json={"error": {"message": "Unsupported model: Gemma-4-31B-IT"}})
            return super().handler(request)

    tf = Flapping()
    env = {"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": KEY, "WAPSI_LLM_MODEL": GEMMA}
    strict = endpoint.probe(env=env, transport=tf.transport)
    assert strict["ok"] is False and strict["models_refused"] == [GEMMA] and len(tf.chats) == 1  # one refused call
    assert "is listed by GET /models but refuses calls right now" in strict["error"]
    assert "WAPSI_ALLOW_MODEL_FALLBACK=1" in strict["error"]
    allowed = endpoint.probe(env={**env, "WAPSI_ALLOW_MODEL_FALLBACK": "1"}, transport=tf.transport)
    assert allowed["ok"] is True and allowed["model"] == "Qwen3.8-27B" and allowed["model_fallback"] is True
    assert allowed["models_refused"] == [GEMMA] and "error" not in allowed and allowed["probe"]["model"] == "Qwen3.8-27B"
    plan = model_plan.plan_endpoint([m for m in allowed["models"] if m not in allowed["models_refused"]],
                                    {**env, "WAPSI_ALLOW_MODEL_FALLBACK": "1"})
    assert plan["drafting"]["id"] == "Qwen3.8-27B"  # the models stage plans without the refused model too


    c, said = Clock(), []

    def nap(seconds: float) -> None:
        c.sleep(seconds)
        tf.refusing = len(c.slept) < 2

    assert endpoint.wait_model(env=env, call=True, transport=tf.transport, timeout_s=600, interval_s=30, sleep=nap,
                               clock=c.time, say=said.append) is True
    assert c.slept == [30, 30] and "is listed but does not answer yet" in said[0] and "and answers" in said[-1]
    tf.refusing = True  # without --call, listed is all that is asked
    assert endpoint.wait_model(env=env, transport=tf.transport, timeout_s=60, sleep=c.sleep, clock=c.time,
                               say=said.append) is True
    # any other failure of the probe is a failure, not a reason to try another model
    broken = endpoint.probe(env={**env, "WAPSI_LLM_API_KEY": "rc-wrong"}, transport=tf.transport)
    assert broken["ok"] is False and "models_refused" not in broken


def test_a_model_that_answers_http_200_with_garbage_does_not_count_as_answering():
    """Seen live on 2026-10-14 ~00:20-00:50 UTC: Gemma-4-31B-IT answered every request with HTTP 200 and a run of
    slashes. The probe (and ``wait-model --call``) must not record that as a model that answers."""

    class Degraded(FakeTokenFactory):
        def handler(self, request):
            if request.method == "POST" and json.loads(request.content)["model"] == GEMMA:
                self.chats.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "choices": [{"message": {"role": "assistant", "content": '{"' + "/" * 40 + "0000" * 6},
                                 "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 32, "total_tokens": 52, "cost": 0.000003}})
            return super().handler(request)

    assert endpoint.degenerate_reason('{"' + "/" * 40) == "runaway repetition"
    assert endpoint.degenerate_reason('{"ok": true}') is None and endpoint.degenerate_reason("") == "empty answer"
    tf = Degraded()
    env = {"WAPSI_LLM_BASE_URL": TF_URL, "WAPSI_LLM_API_KEY": KEY, "WAPSI_LLM_MODEL": GEMMA}
    strict = endpoint.probe(env=env, transport=tf.transport)
    assert strict["ok"] is False and strict["models_degenerate"] == [GEMMA]
    assert "answers with unusable output right now" in strict["error"] and KEY not in json.dumps(strict)
    allowed = endpoint.probe(env={**env, "WAPSI_ALLOW_MODEL_FALLBACK": "1"}, transport=tf.transport)
    assert allowed["ok"] is True and allowed["model"] != GEMMA and allowed["model_fallback"] is True
    said: list[str] = []
    assert endpoint.wait_model(env=env, transport=tf.transport, timeout_s=10, interval_s=30, call=True,
                               sleep=lambda s: None, say=said.append) is False
    assert "does not answer yet" in said[-1] and "unusable output" in said[-1]


# --------------------------------------------------------------------------- model plan


def test_model_plan_on_an_endpoint_prefers_gemma_and_a_different_judge():
    served = [GEMMA, JUDGE, "Qwen3.8-27B", "DeepSeek-V4-Flash"]
    plan = model_plan.plan_endpoint(served, {})
    assert plan["drafting"]["id"] == GEMMA and plan["drafting"]["google_open_model"] is True
    assert plan["drafting"]["source"] == "default" and plan["judge"]["id"] == JUDGE and plan["warnings"] == []
    # Gemma is not in the catalogue today: the first served fallback, flagged for the Google partner prize
    fallback = model_plan.plan_endpoint(["Qwen3.8-27B", "DeepSeek-V4-Flash"], {})
    assert fallback["drafting"]["id"] == "Qwen3.8-27B" and fallback["drafting"]["source"] == "fallback"
    assert fallback["drafting"]["google_open_model"] is False and fallback["judge"]["id"] == "DeepSeek-V4-Flash"
    assert any("not a Gemma model" in w for w in fallback["warnings"])
    # the judge is never the drafting model itself
    assert model_plan.plan_endpoint(["Qwen3.8-27B"], {})["judge"]["id"] is None
    # the operator decides: explicit ids, case-insensitive, checked against the catalogue
    env = {"WAPSI_LLM_MODEL": "gemma-4-31b-it", "WAPSI_JUDGE_MODEL": "Qwen3.8-27B"}
    explicit = model_plan.plan_endpoint(served, env)
    assert (explicit["drafting"]["id"], explicit["drafting"]["source"]) == (GEMMA, "WAPSI_LLM_MODEL")
    assert explicit["judge"]["id"] == "Qwen3.8-27B"
    assert explicit["fallback_in_use"] is False and plan["fallback_in_use"] is False and fallback["fallback_in_use"] is True
    # a model the operator named is never swapped silently: no drafting model until they wait or allow a fallback
    gone = model_plan.plan_endpoint(["Qwen3.8-27B", JUDGE], {"WAPSI_LLM_MODEL": GEMMA})
    assert gone["drafting"]["id"] is None and gone["requested"] == GEMMA
    assert "is not served right now" in gone["warnings"][0] and "WAPSI_ALLOW_MODEL_FALLBACK=1" in gone["warnings"][0]
    assert "first served: Qwen3.8-27B" in gone["warnings"][0] and "wait-model" in gone["warnings"][0]
    allowed = model_plan.plan_endpoint(["Qwen3.8-27B", JUDGE], {"WAPSI_LLM_MODEL": GEMMA, "WAPSI_ALLOW_MODEL_FALLBACK": "1"})
    assert allowed["drafting"]["id"] == "Qwen3.8-27B" and allowed["fallback_in_use"] is True
    assert any("not a Gemma model" in w for w in allowed["warnings"])
    assert model_plan.plan_endpoint(served, {"WAPSI_JUDGE_MODEL": "none"})["judge"]["id"] is None
    assert model_plan.plan_endpoint(served, {"WAPSI_LLM_FALLBACK_MODELS": "DeepSeek-V4-Flash, Qwen3.8-27B"})[
        "drafting"]["id"] == "DeepSeek-V4-Flash"
    assert model_plan.plan_endpoint([], {})["drafting"]["id"] is None  # nothing served: no plan, exit 1
    unprobed = model_plan.plan_endpoint(None, {})  # no /models answer: the default, marked unchecked
    assert unprobed["drafting"]["id"] == GEMMA and unprobed["served_checked"] is False
    lines = dict(line.split("=", 1) for line in model_plan.shell_lines(plan))
    assert lines == {"PLAN_PLATFORM": "endpoint", "PLAN_DRAFT_MODEL": GEMMA, "PLAN_JUDGE_MODEL": JUDGE,
                     "PLAN_GOOGLE_OPEN_MODEL": "1"}


def test_fallback_models_that_think_get_a_lower_effort_and_token_headroom(tf, monkeypatch):
    """Seen live: Qwen3.8-27B spent a 32-token budget on reasoning and returned no content. Gemma 4 needs nothing."""
    assert model_plan.request_defaults(GEMMA) == ({}, 0) and model_plan.request_defaults(JUDGE) == ({}, 0)
    assert model_plan.request_defaults("Qwen3.8-27B") == ({"reasoning_effort": "low"}, 1024)
    assert model_plan.request_defaults("DeepSeek-V4-Flash")[0] == {"reasoning_effort": "low"}
    gemma = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False)
    gemma.complete([{"role": "user", "content": "x"}], max_tokens=32)
    assert tf.chats[-1]["max_tokens"] == 32 and "reasoning_effort" not in tf.chats[-1]
    monkeypatch.setenv("WAPSI_LLM_MODEL", "Qwen3.8-27B")
    qwen = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False)
    qwen.complete([{"role": "user", "content": "x"}], max_tokens=32)
    assert tf.chats[-1]["max_tokens"] == 32 + 1024 and tf.chats[-1]["reasoning_effort"] == "low"
    assert qwen.report()["max_tokens_headroom"] == 1024 and qwen.report()["extra_body"] == {"reasoning_effort": "low"}
    monkeypatch.setenv("WAPSI_LLM_MAX_TOKENS_HEADROOM", "0")  # the operator's settings win
    monkeypatch.setenv("WAPSI_LLM_EXTRA_BODY", '{"reasoning_effort": "medium"}')
    tuned = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False)
    tuned.complete([{"role": "user", "content": "x"}], max_tokens=32)
    assert tf.chats[-1]["max_tokens"] == 32 and tf.chats[-1]["reasoning_effort"] == "medium"
    monkeypatch.setenv("WAPSI_LLM_BASE_URL", "http://127.0.0.1:8000/v1")  # your own vLLM: its own settings decide
    for k in ("WAPSI_LLM_MAX_TOKENS_HEADROOM", "WAPSI_LLM_EXTRA_BODY"):
        monkeypatch.delenv(k)
    local = endpoint.MeteredLLM.from_env(transport=tf.transport, cache=False)
    assert local.extra_body == {} and local.max_tokens_headroom == 0


def test_model_plan_on_a_gpu_box_picks_the_gemma_that_fits():
    big = model_plan.plan_self_hosted(192.0, {})
    assert big["drafting"]["id"] == "google/gemma-4-31B-it" and big["lora"] is True
    assert big["lora_config"] == "gemma4-31b-mi300x" and big["judge"]["id"] == "google/gemma-4-E4B-it"
    assert big["merged_name"] == "wapsi-gemma-4-31b-str-merged" and big["warnings"] == []
    mid = model_plan.plan_self_hosted(96.0, {})  # serves the 31B model, cannot fine-tune it: base model, no LoRA
    assert mid["drafting"]["id"] == "google/gemma-4-31B-it" and mid["lora"] is False and mid["judge"]["id"] is None
    assert any("LoRA stages are skipped" in w for w in mid["warnings"])
    assert model_plan.plan_self_hosted(96.0, {"WAPSI_LORA": "1"})["lora"] is True  # the operator forces it
    small = model_plan.plan_self_hosted(48.0, {})
    assert small["drafting"]["id"] == "google/gemma-4-E4B-it" and small["drafting"]["source"] == "fallback"
    assert small["lora"] is True and small["lora_config"] == "gemma4-e4b-mi300x" and small["judge"]["id"] is None
    tiny = model_plan.plan_self_hosted(12.0, {})
    assert tiny["drafting"]["id"] == "google/gemma-4-E4B-it" and any("endpoint mode" in w for w in tiny["warnings"])
    unknown = model_plan.plan_self_hosted(None, {})
    assert unknown["drafting"]["id"] == "google/gemma-4-31B-it" and any("GPU memory unknown" in w for w in unknown["warnings"])
    qwen = model_plan.plan_self_hosted(192.0, {"WAPSI_BASE_MODEL": "Qwen/Qwen3.6-35B-A3B"})
    assert qwen["drafting"]["google_open_model"] is False and qwen["lora_config"] == "qwen36-35b-a3b-mi300x"
    assert qwen["base_profile"] == "qwen-fallback" and any("not a Gemma model" in w for w in qwen["warnings"])
    assert all(c["id"].startswith("google/gemma-4") for c in model_plan.SELF_HOSTED_DRAFT[:2])  # Gemma first
    lines = dict(line.split("=", 1) for line in model_plan.shell_lines(mid))
    assert lines["PLAN_LORA"] == "0" and lines["PLAN_DRAFT_MODEL"] == "google/gemma-4-31B-it" and lines["PLAN_JUDGE_MODEL"] == ""


def test_model_plan_cli_reads_the_probe_and_the_torch_info(tmp_path, capsys, monkeypatch):
    for k in [k for k in os.environ if k.startswith("WAPSI_")]:
        monkeypatch.delenv(k)
    (tmp_path / "endpoint.json").write_text(json.dumps({"models_status": 200, "models": ["Qwen3.8-27B", JUDGE]}),
                                            encoding="utf-8")
    assert model_plan.main(["--mode", "endpoint", "--endpoint-json", str(tmp_path / "endpoint.json"),
                            "--out", str(tmp_path / "plan.json"), "--shell", str(tmp_path / "plan.env")]) == 0
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["schema"] == model_plan.SCHEMA and plan["drafting"]["id"] == "Qwen3.8-27B"
    assert "PLAN_DRAFT_MODEL=Qwen3.8-27B" in (tmp_path / "plan.env").read_text(encoding="utf-8")
    assert "WARNING" in capsys.readouterr().out
    (tmp_path / "torch.json").write_text(json.dumps({"available": True, "vram_total_mb": 49152}), encoding="utf-8")
    assert model_plan.main(["--mode", "notebook", "--torch-info", str(tmp_path / "torch.json"),
                            "--out", str(tmp_path / "nb.json")]) == 0
    nb = json.loads((tmp_path / "nb.json").read_text(encoding="utf-8"))
    assert nb["vram_gb"] == 48.0 and nb["drafting"]["id"] == "google/gemma-4-E4B-it" and nb["platform"] == "notebook"


# --------------------------------------------------------------------------- the tools in endpoint mode


def test_golden_eval_through_the_endpoint(tf, tmp_path):
    from editions.amd.kickoff import golden_eval

    assert golden_eval.main(["--endpoint", "--world", "showcase-memory", "--limit", "2", "--out", str(tmp_path)],
                            transport=tf.transport) == 0
    rep = json.loads((tmp_path / "golden_eval.json").read_text(encoding="utf-8"))
    assert rep["kind"] == "golden_eval" and rep["stub"] is False and rep["cases"] == 2
    assert rep["label"].startswith("AMD Token Factory, Gemma-4-31B-IT, measured ")
    assert rep["config"]["llm"] == f"{GEMMA}@{TF_HOST}" and rep["config"]["scorer"] == "heuristic-v2"
    m, ep = rep["metrics"], rep["endpoint"]
    assert m["hold_exact_match_rate"] == 1.0 and m["false_freeze_count"] == 0  # numbers from data, not from the LLM
    assert m["draft_verification_pass_rate"] == 1.0 and m["draft_numeric_accuracy"] == 1.0
    assert m["llm_only_hallucination_rate"] == 1.0  # the fake's raw summary states an amount that is not in the case
    assert ep["endpoint_host"] == TF_HOST and ep["calls"] >= 4 and ep["cost_usd"] > 0 and ep["errors"] == 0
    md = (tmp_path / "golden_eval.md").read_text(encoding="utf-8")
    assert "never come from the LLM" in md and "API-reported cost" in md
    calls = len(tf.chats)
    assert golden_eval.main(["--endpoint", "--world", "showcase-memory", "--limit", "2", "--out", str(tmp_path)],
                            transport=tf.transport) == 0
    assert len(tf.chats) == calls  # a re-run is answered from the cache: no call, no cost
    again = json.loads((tmp_path / "golden_eval.json").read_text(encoding="utf-8"))["endpoint"]
    assert again["calls"] == 0 and again["cache_hits"] == ep["calls"]
    assert not (ROOT / "evals" / "reports" / "golden_eval.json").exists()  # it never writes into evals/reports
    assert KEY not in _files_text(tmp_path)

    assert golden_eval.main(["--fake", "--world", "showcase-memory", "--limit", "1", "--out", str(tmp_path / "f")]) == 0
    fake = json.loads((tmp_path / "f" / "golden_eval.json").read_text(encoding="utf-8"))
    assert fake["stub"] is True and "not a model result" in fake["label"] and "endpoint" not in fake


def test_ab_through_the_endpoint_compares_like_with_like(tf, tmp_path):
    from editions.amd.kickoff import ab_hallucination as ab

    args = ["--endpoint", "--world", "showcase-memory", "--limit", "2", "--workers", "2",
            "--out", str(tmp_path / "ab.json")]
    assert ab.main(args, transport=tf.transport) == 0
    rep = json.loads((tmp_path / "ab.json").read_text(encoding="utf-8"))
    assert rep["stub"] is False and rep["cases_compared"] == 2 and rep["errors"] == []
    assert rep["label"].startswith("AMD Token Factory, Gemma-4-31B-IT, measured ") and "MI300X droplet" in rep["label"]
    raw, bound = rep["arms"]["raw"], rep["arms"]["bound"]
    assert raw["docs"] == 2 and raw["docs_with_wrong_amount"] == 2 and raw["invented_accounts"] == 2
    assert bound["str_only"]["docs"] == 2 and bound["docs_with_wrong_amount"] == 0 and bound["verifier_pass_rate"] == 1.0
    assert rep["endpoint"]["endpoint_host"] == TF_HOST and rep["endpoint"]["calls"] > 0
    md = (tmp_path / "ab.md").read_text(encoding="utf-8")
    assert "Endpoint: `developer.amd.com.cn`" in md and "GPU provider-side" in md
    assert KEY not in _files_text(tmp_path)


def test_e2e_bench_through_the_endpoint_has_no_gpu_hour_cost(tf, tmp_path, monkeypatch):
    from editions.amd.bench import e2e_bench

    monkeypatch.setenv("WAPSI_JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    assert e2e_bench.main(["--endpoint", "--world", "showcase-memory", "--no-search", "--concurrency", "2",
                           "--inr-per-usd", "88", "--name", "e2e_c2", "--out", str(tmp_path)],
                          transport=tf.transport) == 0
    rep = json.loads((tmp_path / "e2e_c2.json").read_text(encoding="utf-8"))
    m, c, ep = rep["metrics"], rep["cost"], rep["endpoint"]
    assert rep["label"].startswith("AMD Token Factory, Gemma-4-31B-IT") and rep["model"]["llm"] == f"{GEMMA}@{TF_HOST}"
    assert m["cases_ok"] == m["cases"] == 2 and m["draft_verification_pass_rate"] == 1.0
    assert m["drafts_per_hour"] > 0 and m["llm_seconds_per_case"]["n"] == 2
    assert c["usd_per_case"] is None and c["gpu_hour_usd"] is None and "placeholder" in c["note"]  # no GPU to price
    assert c["api_usd_per_case"] == round(ep["cost_usd"] / 2, 8)  # the report keeps 8 decimals
    assert c["api_inr_per_case"] == pytest.approx(c["api_usd_per_case"] * 88, rel=1e-4)
    assert ep["cache_hits"] == 0 and ep["calls"] == m["llm_calls"]  # a benchmark never reads the cache
    assert rep["settings"]["judge_endpoint"]["model"] == JUDGE and rep["model"]["guard"].startswith("rules+judge")
    assert {b["model"] for b in tf.chats} == {GEMMA, JUDGE}
    assert "## Endpoint" in (tmp_path / "e2e_c2.md").read_text(encoding="utf-8") and KEY not in _files_text(tmp_path)

    # a GPU box whose hourly price is not known: the cost stays empty instead of a made-up figure
    assert e2e_bench.main(["--world", "showcase-memory", "--fake-llm", "--no-search", "--limit", "1", "--no-gpu-cost",
                           "--name", "e2e_c1", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "e2e_c1.json").read_text(encoding="utf-8"))["cost"]["usd_per_case"] is None


def test_llm_bench_on_a_rate_limited_endpoint(tf, monkeypatch):
    from editions.amd.bench import llm_bench

    tf.fail_429 = 1
    cfg = llm_bench.BenchConfig(base_url=TF_URL, model=GEMMA, api_key=KEY, levels=(1, 2), requests=2, input_len=32,
                                output_len=16, warmup=0, ignore_eos=False, rpm=600, label="fixture")
    res = asyncio.run(llm_bench.run_bench(cfg, transport=tf.transport))
    first, second = res["levels"]
    assert res["label"] == "fixture" and res["model"] == GEMMA and res["settings"]["rpm"] == 600
    assert first["completed"] == 2 and first["errors"] == 0 and first["rate_limited"] == 1  # the 429 was retried
    assert second["rate_limited"] == 0 and second["paced_s"] == 0
    for lv in (first, second):
        assert lv["tokens_source"] == "usage" and lv["cost_usd"] > 0 and lv["request_tokens_per_s"]["n"] == 2
    assert "ignore_eos" not in tf.chats[-1] and tf.chats[-1]["stream"] is True
    assert "rate-limited" in llm_bench.to_markdown(res)

    # more requests than the 60 s window holds: the level waits for room first, and a request that still has to
    # wait inside the level records it (a fake clock stands in for the minute)
    c = Clock()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        c.sleep(delay)
        await real_sleep(0)

    monkeypatch.setattr(llm_bench.asyncio, "sleep", fake_sleep)
    limiter = endpoint.RateLimiter(rpm=2, max_concurrency=4, clock=c.time, sleep=c.sleep)
    limiter.acquire(); limiter.release()  # noqa: E702  (an earlier request is still inside the window)
    one = llm_bench.BenchConfig(base_url=TF_URL, model=GEMMA, api_key=KEY, ignore_eos=False)

    async def level(n: int) -> dict:
        async with httpx.AsyncClient(transport=tf.transport, headers={"Authorization": f"Bearer {KEY}"}) as client:
            return await llm_bench.run_level(client, one, GEMMA, n, llm_bench.synthetic_prompts(n, 16, 8), limiter)

    burst = asyncio.run(level(2))
    assert c.slept[0] == pytest.approx(60.0)  # waited for room for the whole burst before starting it
    assert burst["completed"] == 2 and burst["paced_s"] == 0  # then ran as one burst: a clean level
    paced = asyncio.run(level(3))  # three requests can never start together under 2 a minute
    assert paced["completed"] == 3 and paced["paced_s"] > 0


def test_buffered_streams_are_flagged_not_mistaken_for_speed():
    from editions.amd.bench.llm_bench import RequestRecord, summarize_level

    burst = [RequestRecord(ok=True, start=0.0, end=4.0, ttft=3.99, chunk_times=[3.99 + i * 1e-4 for i in range(40)],
                           output_tokens=40) for _ in range(3)]
    lv = summarize_level(burst, 1, 12.0)
    assert lv["stream_buffered"] is True and lv["output_tokens_per_s_per_user"] is None and lv["itl_s"]["n"] == 0
    assert lv["request_tokens_per_s"]["p50"] == 10.0 and lv["output_tokens_per_s"] == 10.0  # 120 tokens in 12 s
    spread = [RequestRecord(ok=True, start=0.0, end=4.0, ttft=0.5, chunk_times=[0.5 + i * 0.08 for i in range(40)],
                            output_tokens=40)]
    assert summarize_level(spread, 1, 4.0)["stream_buffered"] is False


# --------------------------------------------------------------------------- one bundle, three modes


def _stage(evid: Path, idx: int, name: str, mode: str, started: str, notes: dict | None = None) -> None:
    notes_file = evid / "stages" / f"{idx:02d}-{name}.notes"
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    notes_file.write_text("".join(f"{k}={v}\n" for k, v in (notes or {}).items()), encoding="utf-8")
    stagelog.record(evid, index=idx, stage=name, mode=mode, status="ok", exit_code=0, started=started, ended=started,
                    notes=notes_file, budget_info=stagelog.budget(evid, mode))


def make_endpoint_evidence(tf: FakeTokenFactory, root: Path, *, date: str = "2026-10-14",
                           started: str = "2026-10-14T06:00:00Z", env: dict | None = None) -> Path:
    """An endpoint-mode evidence folder written by the real tools against the fake endpoint (showcase cases)."""
    from editions.amd.bench import e2e_bench, llm_bench
    from editions.amd.kickoff import ab_hallucination, golden_eval

    evid = root / "endpoint" / date
    (evid / "bench").mkdir(parents=True)
    info = endpoint.probe(transport=tf.transport, env=env)
    (evid / "endpoint.json").write_text(json.dumps(info), encoding="utf-8")
    plan = model_plan.plan_endpoint(info.get("models"), env or os.environ)
    (evid / "model_plan.json").write_text(json.dumps({"schema": model_plan.SCHEMA, **plan}), encoding="utf-8")
    world = ["--world", "showcase-memory"]
    assert golden_eval.main(["--endpoint", *world, "--out", str(evid)], transport=tf.transport) == 0
    assert ab_hallucination.main(["--endpoint", *world, "--workers", "1", "--out", str(evid / "ab_hallucination.json")],
                                 transport=tf.transport) == 0
    assert e2e_bench.main(["--endpoint", *world, "--no-search", "--concurrency", "2", "--name", "e2e_c2",
                           "--out", str(evid / "bench")], transport=tf.transport) == 0
    for name, levels in (("llm_bench", (1, 4, 8)), ("llm_bench_wapsi_prompts", (1, 4))):
        cfg = llm_bench.BenchConfig(base_url=TF_URL, model=info["model"], api_key=tf.key, levels=levels, requests=2,
                                    input_len=32, output_len=16, warmup=0, ignore_eos=False,
                                    prompts="wapsi" if "wapsi" in name else "synthetic", json_mode="json_object",
                                    label=endpoint.measured_label(TF_URL, info["model"], date))
        res = asyncio.run(llm_bench.run_bench(cfg, transport=tf.transport))
        (evid / "bench" / f"{name}.json").write_text(json.dumps(res, default=str), encoding="utf-8")
    for i, name in ((1, "env"), (9, "eval"), (10, "bench"), (11, "ab")):
        _stage(evid, i, name, "endpoint", started, {"git_commit": "f1x7ure"} if i == 1 else None)
    return evid


def test_endpoint_bundle_names_the_endpoint_and_never_a_gpu(tf, tmp_path, monkeypatch):
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    evid = make_endpoint_evidence(tf, tmp_path)
    assert collect_evidence.main(["--evidence-dir", str(evid), "--mode", "endpoint", "--quiet"]) == 0
    ev = json.loads((evid / "evidence.json").read_text(encoding="utf-8"))
    h, where = ev["headline"], ev["compute"]
    assert ev["schema"] == collect_evidence.SCHEMA and ev["mode"] == "endpoint"
    assert ev["results_valid"] is True and ev["gpu_results_valid"] is False and ev["invalid_reasons"] == []
    assert where == {**where, "kind": "provider-endpoint", "gpu_side": "provider", "gpu_name": None,
                     "endpoint_host": TF_HOST, "model_id": GEMMA, "amd_served": True,
                     "short": "AMD Token Factory (Gemma-4-31B-IT on AMD GPUs)"}
    assert "not an MI300X droplet benchmark" in where["label"] and h["gpu_name"] is None
    assert (h["endpoint_host"], h["model_id"], h["judge_model_id"]) == (TF_HOST, GEMMA, JUDGE)
    # the same headline keys as a GPU run where the meaning is the same
    assert h["llm_tok_s_c1"] > 0 and h["llm_tok_s_c8"] > 0 and h["llm_e2e_p50_s_c1"] is not None
    assert h["wapsi_prompts_e2e_p95_s_c1"] is not None and h["llm_req_tok_s_p50_c4"] > 0
    assert h["wapsi_prompts_tok_s_c4"] > 0  # the real drafting prompts ask for JSON without a strict schema
    assert h["e2e_cases_per_hour_c2"] > 0 and h["e2e_best_concurrency"] == 2 and h["e2e_best_drafts_per_hour"] > 0
    assert h["e2e_best_llm_seconds_per_case_p50"] is not None and h["e2e_best_llm_calls_per_case"] > 0
    assert (h["golden_cases"], h["golden_draft_numeric_accuracy"], h["golden_false_freeze_count"]) == (2, 1.0, 0)
    assert h["golden_llm_only_hallucination_rate"] == 1.0
    assert (h["ab_raw_docs"], h["ab_raw_docs_with_wrong_amount"], h["ab_bound_str_docs_with_wrong_amount"]) == (2, 2, 0)
    # cost: what the API reported; the GPU-hour figures are placeholders for a run that has a GPU to price
    assert h["e2e_best_api_usd_per_case"] > 0 and h["api_cost_usd_total"] > h["e2e_best_api_usd_per_case"]
    assert h["e2e_best_usd_per_case"] is None and h["e2e_best_drafts_per_gpu_hour"] is None
    assert "e2e_best_usd_per_case" in ev["missing"] and not any(k.startswith(("vram_", "gnn_", "lora_")) for k in h)
    assert ev["sources"]["golden_draft_numeric_accuracy"] == "golden_eval.json: metrics.draft_numeric_accuracy"
    assert ev["results"]["endpoint"]["endpoint"]["limits"]["rpm_limit"] == 20
    assert ev["budget"]["metered"] is False and ev["stages"][0]["window"] == "inside"

    md = (evid / "evidence.md").read_text(encoding="utf-8")
    assert md.startswith("# Wapsi on AMD Token Factory (Gemma-4-31B-IT on AMD GPUs): evidence, 2026-10-14")
    assert "> **Endpoint run.**" in md and "These are not MI300X droplet benchmarks" in md
    assert "## Endpoint (`endpoint.py probe`)" in md and "| Endpoint host | developer.amd.com.cn |" in md
    assert "Google open model (Gemma)" in md and "GPU-hour cost per case | n/a" in md
    assert "Hardware (`amd-smi static`)" not in md and "AMD Instinct MI300X" not in md
    blob = _files_text(evid)
    assert KEY not in blob and "org-SECRET-ACCOUNT-ID" not in blob


def test_endpoint_bundle_validity_rules(tf, tmp_path, monkeypatch):
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    evid = make_endpoint_evidence(tf, tmp_path / "ok")
    assert collect_evidence.build(evid, "endpoint")["results_valid"] is True
    early = make_endpoint_evidence(tf, tmp_path / "early", date="2026-10-10", started="2026-10-10T06:00:00Z")
    ev = collect_evidence.build(early, "endpoint")
    assert ev["results_valid"] is False and any("outside the event window" in r for r in ev["invalid_reasons"])
    (evid / "endpoint.json").unlink()  # nobody recorded who answered
    ev = collect_evidence.build(evid, "endpoint")
    assert ev["results_valid"] is False and any("endpoint.json is missing" in r for r in ev["invalid_reasons"])
    assert ev["headline"]["endpoint_host"] == TF_HOST  # still read from the result files

    def with_probe(change) -> dict:
        info = endpoint.probe(transport=tf.transport)
        change(info)
        (evid / "endpoint.json").write_text(json.dumps(info), encoding="utf-8")
        return collect_evidence.build(evid, "endpoint")

    other = with_probe(lambda i: i.update(provider=endpoint.provider_of("https://llm.example.org/v1", {})))
    assert other["results_valid"] is False and any("not an AMD host" in r for r in other["invalid_reasons"])
    declared = with_probe(lambda i: i.update(provider=endpoint.provider_of(
        "https://llm.example.org/v1", {"WAPSI_ENDPOINT_HARDWARE": "AMD Instinct MI300X (our droplet)"})))
    assert declared["results_valid"] is True and "as declared" in declared["compute"]["short"]
    assert "declared by the operator, not measured" in declared["compute"]["label"]
    local = with_probe(lambda i: i.update(provider=endpoint.provider_of("http://127.0.0.1:8000/v1", {})))
    assert local["results_valid"] is False and any("notebook or droplet mode" in r for r in local["invalid_reasons"])
    failed = with_probe(lambda i: i.update(ok=False, error="HTTP 401"))
    assert failed["results_valid"] is False and any("probe failed" in r for r in failed["invalid_reasons"])
    # an endpoint folder is never GPU evidence, and stub results in it are never numbers
    assert collect_evidence.build(evid, "droplet")["gpu_results_valid"] is False
    from editions.amd.kickoff import ab_hallucination, golden_eval

    assert golden_eval.main(["--fake", "--world", "showcase-memory", "--limit", "1", "--out", str(evid)]) == 0
    assert ab_hallucination.main(["--fake", "--world", "showcase-memory", "--limit", "1",
                                  "--out", str(evid / "ab_hallucination.json")]) == 0
    stub = with_probe(lambda i: None)
    assert stub["headline"]["golden_cases"] is None and "ab_raw_docs" not in stub["headline"]
    assert sum("stub LLM" in w for w in stub["warnings"]) == 2


def test_a_run_whose_prose_was_mostly_the_fixed_paragraph_is_not_valid(tf, tmp_path, monkeypatch):
    """A degraded deployment answers HTTP 200 with garbage; the prose validator rejects it and the draft gets its
    fixed paragraph, so every draft still "passes". Such a run measured the fallback text, not the model."""
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    assert collect_evidence.model_health({"golden_llm_prose_fallback_rate": 0.24}) == []  # healthy Gemma 4
    assert collect_evidence.model_health({"golden_llm_prose_fallback_rate": 1.0})
    assert collect_evidence.model_health({"e2e_best_prose_fallback_rate": 0.9}, "endpoint")
    assert collect_evidence.model_health({"e2e_best_prose_fallback_rate": 0.9}, "droplet") == []  # LoRA's business
    evid = make_endpoint_evidence(tf, tmp_path / "ok")
    rep_path = evid / "golden_eval.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else None
    if rep is None or rep.get("stub"):
        pytest.skip("the endpoint fixture wrote no real-model golden report")
    rep.setdefault("metrics", {})["llm_prose_fallback_rate"] = 1.0
    rep_path.write_text(json.dumps(rep), encoding="utf-8")
    ev = collect_evidence.build(evid, "endpoint")
    assert ev["results_valid"] is False and any("fixed paragraph" in r for r in ev["invalid_reasons"])


def make_notebook_evidence(root: Path, *, gpu: str = "AMD Radeon PRO W7900", static: bool = False,
                           hip: str | None = "7.1.0") -> Path:
    """A notebook-mode folder: the fixture files of the droplet tests plus the notebook's own GPU identity."""
    from test_amd_kickoff import make_evidence

    evid = make_evidence(root / "notebook", date="2026-10-15", started="2026-10-15T09:00:00Z", gpu=gpu, static=static)
    (evid / "torch_info.json").write_text(json.dumps({"available": True, "torch": "2.9.1+rocm7.1", "hip": hip,
                                                      "device_name": gpu, "vram_total_mb": 49152}), encoding="utf-8")
    return evid


def test_notebook_bundle_reports_the_gpu_the_notebook_has(tmp_path):
    evid = make_notebook_evidence(tmp_path)
    (evid / "bench" / "telemetry_summary.json").unlink()  # no amd-smi in this notebook: torch names the GPU
    ev = collect_evidence.build(evid, "notebook")
    assert ev["mode"] == "notebook" and ev["results_valid"] is True and ev["gpu_results_valid"] is True
    assert ev["hardware"]["gpu_name_source"].startswith("torch_info.json") and ev["hardware"]["torch"]["hip"] == "7.1.0"
    assert ev["compute"]["kind"] == "amd-rocm-notebook" and ev["compute"]["short"] == "AMD Radeon PRO W7900 (AMD ROCm notebook)"
    md = collect_evidence.to_markdown(ev)
    assert md.startswith("# Wapsi on AMD Radeon PRO W7900 (AMD ROCm notebook): evidence, 2026-10-15")
    assert "MI300X" not in md.split("## Headline")[0]  # never called an MI300X
    mi = collect_evidence.build(make_notebook_evidence(tmp_path / "mi", gpu="AMD Instinct MI300X", static=True), "notebook")
    assert mi["compute"]["short"] == "MI300X (AMD ROCm notebook)" and mi["hardware"]["gpu_name_source"] == "amd_smi_static.json"
    cuda = make_notebook_evidence(tmp_path / "cuda", gpu="Some CUDA GPU", hip=None)  # not a ROCm build: no identity
    (cuda / "bench" / "telemetry_summary.json").unlink()
    ev = collect_evidence.build(cuda, "notebook")
    assert ev["results_valid"] is False and any("no GPU identity" in r for r in ev["invalid_reasons"])
    # the same folder is not a droplet run: the droplet rule still needs an MI300X
    assert collect_evidence.build(evid, "droplet")["results_valid"] is False


# --------------------------------------------------------------------------- fill_pitch, any mode


def _checker():
    spec = importlib.util.spec_from_file_location("pitch_check_numbers_modes", ROOT / "pitch" / "check_numbers.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pitch_copy(tmp_path: Path, extra_page: str = "", extra_ids: tuple[str, ...] = ()) -> Path:
    from test_amd_kickoff import _pitch_copy as copy_pitch

    return copy_pitch(tmp_path, extra_page, extra_ids)


def _fixture(evid: Path, mode: str) -> Path:
    ev = collect_evidence.build(evid, mode)
    ev["synthetic_fixture"] = True
    path = evid / "evidence.json"
    path.write_text(json.dumps(ev), encoding="utf-8")
    return path


EP_PAGE = """
## Measured through the AMD Token Factory API

Measured on TBD(ep_where), through the shared trial API. The A/B: TBD(ab_where).

| Metric | Value |
|---|---|
| Request latency p50 / p95, one at a time | TBD(ep_llm_latency) |
| Output tokens per second at concurrency 1 / 4 / 8 | TBD(ep_llm_tps) |
| Verified drafts per hour at the trial key's request cap | TBD(ep_drafts_hour) |
| API-reported cost per case | TBD(ep_cost_case) |
| Golden set: numeric accuracy / citation validity / verifier pass | TBD(ep_golden) |
| STR against STR | TBD(ab_str_wrong) |
| On the GPU box (TBD(gpu_where)) | TBD(gpu_ttft) |

By hand: TBD(evolus_blocks).
"""
EP_IDS = ("ep_where", "ab_where", "ep_llm_latency", "ep_llm_tps", "ep_drafts_hour", "ep_cost_case", "ep_golden",
          "ab_str_wrong", "gpu_where", "gpu_ttft")


def test_fill_pitch_from_an_endpoint_bundle_words_it_as_an_endpoint_run(tf, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    pitch = _pitch_copy(tmp_path, EP_PAGE, EP_IDS)
    deck_path = pitch / "amd" / "DECK_OUTLINE.md"
    bundle = _fixture(make_endpoint_evidence(tf, tmp_path), "endpoint")
    args = ["--evidence", str(bundle), "--pitch-dir", str(pitch), "--allow-fixture", "--strict"]
    before = deck_path.read_text(encoding="utf-8")
    assert fill_pitch.main(args) == 0 and deck_path.read_text(encoding="utf-8") == before  # dry run
    assert "endpoint run, AMD Token Factory (Gemma-4-31B-IT on AMD GPUs)" in capsys.readouterr().out
    assert fill_pitch.main([*args, "--write"]) == 0
    out = capsys.readouterr().out
    deck = deck_path.read_text(encoding="utf-8")
    video = (pitch / "amd" / "VIDEO_SCRIPT.md").read_text(encoding="utf-8")
    where = "AMD Token Factory (Gemma-4-31B-IT on AMD GPUs)"
    # the real deck's Token Factory sentences (they say "Gemma 4", the bundle's model is Gemma) are filled
    assert re.search(r"<!-- slot:tf_demo_pass -->1\.0 \(\d+ cases\)<!-- /slot -->", deck)
    assert "TBD(tf_demo_pass)" not in video  # every place of a slot, in every file
    for sid in ("tf_demo_fallback", "tf_demo_hold_exact", "tf_call_latency", "ab_raw_wrong", "ab_bound_wrong"):
        assert f"<!-- slot:{sid} -->" in deck and f"TBD({sid})" not in deck, sid
    assert re.search(r"<!-- slot:tf_call_latency -->\d+\.\d\d s<!-- /slot -->", deck)
    # the optional endpoint slots the author placed say where they were measured
    assert f"Measured on <!-- slot:ep_where -->{where}, 2026-10-14<!-- /slot -->, through the shared trial API." in deck
    assert f"The A/B: <!-- slot:ab_where -->{where}, 2026-10-14<!-- /slot -->." in deck
    assert "<!-- slot:ab_str_wrong -->2 of 2 raw STRs, 0 of 2 Wapsi-bound STRs<!-- /slot -->" in deck
    for sid in ("ep_llm_latency", "ep_llm_tps", "ep_drafts_hour", "ep_cost_case", "ep_golden"):
        assert f"TBD({sid})" not in deck, sid
    assert "<!-- slot:ep_golden -->1.0 / 1.0 / 1.0<!-- /slot -->" in deck and "<!-- slot:ep_cost_case -->$0.0" in deck
    # nothing an endpoint cannot know: every GPU-box slot stays open, and so does every hand-filled one
    for sid in ("droplet_vram", "droplet_tokens_s", "droplet_cases_hour", "gnn_amd_auc", "gnn_amd_epoch", "gpu_where",
                "gpu_ttft", "evolus_blocks"):
        assert f"TBD({sid})" in deck, sid
    assert "measured, not in the pitch" in out and "ep_cases_hour" in out  # measured, simply not placed

    check = _checker()
    rows = check.parse_rows(pitch / "NUMBERS.md")
    amd_rows = [r for r in rows if r.rid.startswith("amd_")]
    errors, resolved = check.check_numbers(amd_rows)  # each row re-reads the endpoint bundle
    assert errors == [] and resolved["amd_ab_raw_docs"] == 2 and "amd_e2e_best_api_usd_per_case" in resolved
    assert not any(r.rid.startswith(("amd_vram", "amd_gnn", "amd_lora")) for r in amd_rows)
    registered = {check._norm(t) for r in rows for t in check.TOKEN_RE.findall(r.pitched)}
    for page in (deck, video):
        for tok in check.TOKEN_RE.findall(page):
            assert check._norm(tok) in registered, tok
    numbers = (pitch / "NUMBERS.md").read_text(encoding="utf-8")
    assert f"## AMD edition: measured on {where}" in numbers and "of the endpoint runs on 2026-10-14 (UTC)" in numbers
    listed = set(check.parse_slots(pitch / "NUMBERS.md"))  # filled slots leave the open-slots table, open ones stay
    assert not {"tf_demo_pass", "tf_call_latency", "ab_bound_wrong", "ep_golden"} & listed
    assert {"droplet_vram", "gnn_amd_auc", "gpu_ttft", "evolus_blocks"} <= listed
    assert fill_pitch.main([*args, "--write"]) == 0 and deck_path.read_text(encoding="utf-8") == deck  # idempotent


def test_fill_pitch_never_puts_a_run_under_words_that_do_not_fit(tf, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    # 1. the endpoint serves no Gemma today: the fallback model must not fill a "Gemma 4 on AMD" sentence
    monkeypatch.setenv("WAPSI_LLM_MODEL", "Qwen3.8-27B")
    qwen = FakeTokenFactory(models=("Qwen3.8-27B", JUDGE))
    bundle = _fixture(make_endpoint_evidence(qwen, tmp_path / "q"), "endpoint")
    ev = json.loads(bundle.read_text(encoding="utf-8"))
    assert ev["headline"]["model_id"] == "Qwen3.8-27B" and any("not a Gemma model" in w for w in ev["warnings"])
    pitch = _pitch_copy(tmp_path / "q")
    assert fill_pitch.main(["--evidence", str(bundle), "--pitch-dir", str(pitch), "--allow-fixture", "--write"]) == 0
    out = capsys.readouterr().out
    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    for sid in ("tf_demo_pass", "tf_demo_fallback", "tf_call_latency", "ab_raw_wrong", "ab_bound_wrong"):
        assert f"TBD({sid})" in deck and f"<!-- slot:{sid} -->" not in deck, sid  # untouched
    assert "the pitch words it as a Gemma 4 figure and the drafting model is Qwen3.8-27B" in out
    assert fill_pitch.END not in (pitch / "NUMBERS.md").read_text(encoding="utf-8")
    assert "tf_demo_pass" in _checker().parse_slots(pitch / "NUMBERS.md")  # still an open slot
    # 2. a notebook GPU that is not an MI300X never fills what the pitch words as a droplet figure
    nb = _fixture(make_notebook_evidence(tmp_path / "n"), "notebook")
    pitch = _pitch_copy(tmp_path / "n")
    assert fill_pitch.main(["--evidence", str(nb), "--pitch-dir", str(pitch), "--allow-fixture", "--write"]) == 0
    out = capsys.readouterr().out
    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    for sid in ("droplet_tokens_s", "droplet_cases_hour", "droplet_vram"):
        assert f"TBD({sid})" in deck, sid
    assert ("the pitch words it as an MI300X figure and this run was measured on AMD Radeon PRO W7900 (AMD ROCm "
            "notebook)") in out
    # what it did measure is filled and names the GPU it ran on
    assert "<!-- slot:gnn_amd_auc -->0.985 (AMD Radeon PRO W7900, AMD ROCm notebook)<!-- /slot -->" in deck
    assert "<!-- slot:ab_bound_wrong -->0 (of 180 drafts)<!-- /slot -->" in deck
    # 3. a notebook whose GPU is an MI300X fills them like the droplet does
    mi = _fixture(make_notebook_evidence(tmp_path / "m", gpu="AMD Instinct MI300X", static=True), "notebook")
    pitch = _pitch_copy(tmp_path / "m")
    assert fill_pitch.main(["--evidence", str(mi), "--pitch-dir", str(pitch), "--allow-fixture", "--write", "--strict"]) == 0
    assert "-->50 / 180 / 600 tokens/s" in (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    # 4. an invalid endpoint bundle is refused like any other
    bad = collect_evidence.build(make_endpoint_evidence(tf, tmp_path / "b", date="2026-10-10",
                                                        started="2026-10-10T06:00:00Z"), "endpoint")
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    assert fill_pitch.main(["--evidence", str(tmp_path / "bad.json"), "--write"]) == fill_pitch.EXIT_REFUSED
    assert "results_valid is false" in capsys.readouterr().out
    # 5. --strict fails on a TBD(id) nobody defines (a typo would otherwise stay open for ever)
    pitch = _pitch_copy(tmp_path / "t", "\nTypo: TBD(tf_demo_passs).\n", ("tf_demo_passs",))
    args = ["--evidence", str(mi), "--pitch-dir", str(pitch), "--allow-fixture"]
    assert fill_pitch.main(args) == 0 and fill_pitch.main([*args, "--strict"]) == 1
    assert "tf_demo_passs" in capsys.readouterr().out


def test_fill_pitch_merges_an_endpoint_run_and_a_droplet_run(tf, tmp_path, monkeypatch):
    from test_amd_kickoff import make_evidence

    monkeypatch.setenv("WAPSI_LLM_RPM", "600")
    pitch = _pitch_copy(tmp_path, EP_PAGE, EP_IDS)
    common = ["--pitch-dir", str(pitch), "--allow-fixture", "--write", "--strict"]
    first = _fixture(make_endpoint_evidence(tf, tmp_path), "endpoint")  # Wednesday: the endpoint
    assert fill_pitch.main(["--evidence", str(first), *common]) == 0
    droplet = make_evidence(tmp_path / "d", date="2026-10-16", started="2026-10-16T09:00:00Z")  # Friday: an MI300X
    (droplet / "ab_hallucination.json").unlink()  # no A/B on the droplet: Wednesday's stays
    second = _fixture(droplet, "droplet")
    assert fill_pitch.main(["--evidence", str(second), *common]) == 0
    deck = (pitch / "amd" / "DECK_OUTLINE.md").read_text(encoding="utf-8")
    where = "AMD Token Factory (Gemma-4-31B-IT on AMD GPUs)"
    assert "| On the GPU box (<!-- slot:gpu_where -->MI300X, 2026-10-16<!-- /slot -->) | <!-- slot:gpu_ttft -->" in deck
    assert "<!-- slot:droplet_tokens_s -->50 / 180 / 600 tokens/s" in deck  # the droplet's sweep
    assert f"The A/B: <!-- slot:ab_where -->{where}, 2026-10-14<!-- /slot -->." in deck  # the A/B, still Wednesday's
    assert "<!-- slot:ab_str_wrong -->2 of 2 raw STRs, 0 of 2 Wapsi-bound STRs<!-- /slot -->" in deck
    assert re.search(r"<!-- slot:tf_demo_pass -->1\.0 \(\d+ cases\)<!-- /slot -->", deck)  # and so are the endpoint's
    check = _checker()
    rows = [r for r in check.parse_rows(pitch / "NUMBERS.md") if r.rid.startswith("amd_")]
    assert check.check_numbers(rows)[0] == []
    by_id = {r.rid: r for r in rows}
    assert by_id["amd_ab_raw_docs"].source.endswith("endpoint/2026-10-14/evidence.json")
    assert by_id["amd_llm_tok_s_c16"].source.endswith("2026-10-16/evidence.json")
    numbers = (pitch / "NUMBERS.md").read_text(encoding="utf-8")
    assert "the endpoint and MI300X runs on 2026-10-14, 2026-10-16 (UTC)" in numbers
    assert "measured on AMD Token Factory (Gemma-4-31B-IT on AMD GPUs) and MI300X" in numbers


def test_fill_pitch_lists_its_slots(capsys):
    assert fill_pitch.main(["--slots"]) == 0
    out = capsys.readouterr().out
    assert "| `ep_cost_case` | API-reported cost per case in dollars | endpoint | optional: add `ep_cost_case` to " in out
    assert "| `ab_bound_wrong` |" in out and "(Gemma only) | write `TBD(ab_bound_wrong)`; listed in" in out
    assert "| `tf_demo_pass` |" in out and "| endpoint (Gemma only) | optional: add `tf_demo_pass` to " in out
    assert "| `droplet_tokens_s` |" in out and "droplet / notebook (MI300X only)" in out
    assert "droplet / notebook / endpoint (Gemma only)" in out
    assert "| `adk_tool_calls` | (no evidence bundle carries it) | by hand, from editions.amd.app.agent_demo" in out
    listed = [line for line in out.splitlines() if line.startswith("| `")]
    assert len(listed) == len(fill_pitch.ALL_SLOTS) + len(fill_pitch.MANUAL_SLOTS)
    assert len({s.sid for s in fill_pitch.ALL_SLOTS}) == len(fill_pitch.ALL_SLOTS)


# --------------------------------------------------------------------------- mode detection, budget


def _runner(answers: dict[str, str]):
    def run(cmd: list[str]) -> str | None:
        return answers.get(" ".join(cmd[:2]))

    return run


def test_detect_mode():
    gpu = {"rocm-smi --showmeminfo": json.dumps({"card0": {"VRAM Total Memory (B)": "51521781760",
                                                           "VRAM Total Used Memory (B)": "1048576"}})}
    docker = {"docker version": "27.3.1\n"}
    no_torch = lambda: {"available": False}  # noqa: E731
    rocm_torch = lambda: {"available": True, "rocm_build": True, "hip": "7.1.0", "device_name": "AMD Radeon"}  # noqa: E731
    cuda_torch = lambda: {"available": True, "rocm_build": False, "device_name": "NVIDIA"}  # noqa: E731
    yes, no = (lambda name: f"/usr/bin/{name}"), (lambda name: None)
    detect = stagelog.detect_mode
    assert detect({}, runner=_runner({**gpu, **docker}), which=yes, torch_probe=no_torch)[0] == "droplet"
    assert detect({}, runner=_runner(gpu), which=no, torch_probe=no_torch)[0] == "notebook"
    assert detect({}, runner=_runner(gpu), which=yes, torch_probe=no_torch)[0] == "notebook"  # docker CLI, no daemon
    assert detect({}, runner=_runner({}), which=no, torch_probe=rocm_torch)[0] == "notebook"  # no amd-smi: torch
    env = {"WAPSI_LLM_BASE_URL": TF_URL}
    mode, why = detect(env, runner=_runner(docker), which=yes, torch_probe=cuda_torch)
    assert mode == "endpoint" and "WAPSI_LLM_BASE_URL is set" in why  # a laptop with Docker and a key: no AMD GPU
    assert detect(env, runner=_runner({**gpu, **docker}), which=yes, torch_probe=no_torch)[0] == "droplet"  # the GPU wins
    assert detect({}, runner=_runner(docker), which=yes, torch_probe=no_torch)[0] == "none"
    assert detect({"WAPSI_MODE": "gpu"}, runner=_runner({}), which=no, torch_probe=no_torch) == ("droplet", ["WAPSI_MODE=droplet"])
    assert stagelog.canon_mode("gpu") == "droplet" and stagelog.canon_mode("endpoint") == "endpoint"


def test_budget_is_metered_only_where_gpu_hours_are_billed(tmp_path, monkeypatch):
    monkeypatch.setenv("WAPSI_NOW", "2026-10-14T20:00:00Z")
    monkeypatch.setenv("WAPSI_DROPLET_STARTED_AT", "2026-10-14T10:00:00Z")
    for k in ("WAPSI_GPU_HOUR_USD", "WAPSI_BUDGET_USD", "WAPSI_SPENT_USD", "WAPSI_BUDGET_WARN_PCT", "WAPSI_BUDGET_STOP_PCT"):
        monkeypatch.delenv(k, raising=False)
    ep = stagelog.budget(tmp_path, "endpoint")
    assert ep["metered"] is False and ep["billed"] is False and ep["total_usd"] == 0 and ep["state"] == "ok"
    assert "no GPU-hour bill" in stagelog.budget_line(ep) and "daily cost cap" in ep["label"]
    assert stagelog.main(["budget", "--evid", str(tmp_path), "--mode", "endpoint", "--stage", "bench"]) == 0
    nb = stagelog.budget(tmp_path, "notebook")
    assert nb["metered"] is False and "not metered" in nb["label"]
    monkeypatch.setenv("WAPSI_GPU_HOUR_USD", "9.5")  # the operator says what an hour of the notebook costs
    monkeypatch.setenv("WAPSI_BUDGET_USD", "100")
    nb = stagelog.budget(tmp_path, "notebook")
    assert nb["metered"] and nb["billed"] and nb["droplet_usd"] == 95.0 and nb["state"] == "stop"
    assert "DELETE the notebook instance" in stagelog.budget_line({**nb, "state": "warn"})
    rec = json.loads(stagelog.record(tmp_path, index=9, stage="eval", mode="endpoint", status="ok", exit_code=0,
                                     started="2026-10-14T06:00:00Z", ended="2026-10-14T06:10:00Z").read_text())
    assert rec["mode"] == "endpoint" and "no GPU on this machine" in rec["label"] and rec["window"] == "inside"
    assert "CPU" not in stagelog.RUN_LABELS["notebook"] and stagelog.RUN_LABELS["droplet"] == "MI300X run"


def test_torch_info_never_raises(tmp_path, capsys):
    info = stagelog.torch_info()
    assert set(info) >= {"available", "captured_at", "python"} and isinstance(info["available"], bool)
    assert stagelog.main(["torch-info", str(tmp_path / "t.json")]) == 0
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))["available"] is info["available"]
    assert "ROCm (HIP)" in capsys.readouterr().out


# --------------------------------------------------------------------------- run_all.sh and preflight.sh


def _bash_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WAPSI_", "VLLM_", "HF_", "AMLWORLD", "ROCM_PYTORCH", "SCRATCH", "RADEON_"))}
    env.update({"NO_COLOR": "1"})
    env.update(extra)
    return env


def _sh(bash: str, script: str, *args: str, env: dict[str, str] | None = None, timeout: float = 180):
    return subprocess.run([bash, str(KICK / script), *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env or _bash_env(), timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _headers(out: str) -> list[str]:
    return [line.split("] ")[1].split(":")[0] for line in out.splitlines() if line.startswith("== [")]


def test_endpoint_dry_run_is_deterministic_gpu_free_and_keeps_the_key_out(bash):
    env = _bash_env(WAPSI_LLM_BASE_URL=TF_URL, WAPSI_LLM_API_KEY=KEY, RADEON_API_KEY=KEY, WAPSI_INR_PER_USD="88.5")
    a = _sh(bash, "run_all.sh", "--mode", "endpoint", "--dry-run", "--force", "--date", "2026-10-14", env=env)
    b = _sh(bash, "run_all.sh", "--mode", "endpoint", "--dry-run", "--force", "--date", "2026-10-14", env=env)
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout
    out = a.stdout
    assert _headers(out) == STAGES
    skipped = [line.split("] ")[1].split(":")[0] for line in out.splitlines() if "not part of endpoint mode" in line]
    assert skipped == ["gnn", "lora-data", "lora-train", "merge", "lora-eval", "serve"]  # they need a GPU box
    assert KEY not in out and KEY not in a.stderr and "key set (value not shown)" in out
    commands = " ".join(line for line in out.splitlines() if line.startswith("  $"))
    for forbidden in ("docker ", "hf download", "--device cuda", "amd-smi", "vllm", "HF_TOKEN", "stagelog.py static"):
        assert forbidden not in commands, forbidden
    for needle in (
        "editions/amd/evidence/endpoint/2026-10-14",
        "preflight.sh --mode endpoint --json",
        "endpoint.py probe --out editions/amd/evidence/endpoint/2026-10-14/endpoint.json",
        "model_plan.py --mode endpoint --endpoint-json",
        "editions.amd.kickoff.golden_eval --endpoint --world demo --limit 10 --out",
        f"editions.amd.bench.llm_bench --base-url {TF_URL} --model Gemma-4-31B-IT --concurrency 1 4 8 --requests 6 --rpm 16 --no-ignore-eos",
        "--prompts wapsi --concurrency 1 4 --requests 4 --rpm 16",
        "editions.amd.bench.e2e_bench --endpoint --world demo --limit 6 --concurrency 2 --inr-per-usd 88.5 --name e2e_c2",
        "editions.amd.kickoff.ab_hallucination --endpoint --world demo --limit 10 --workers 2",
        "collect_evidence.py --evidence-dir editions/amd/evidence/endpoint/2026-10-14 --mode endpoint",
        "fill_pitch.py --evidence editions/amd/evidence/endpoint/2026-10-14/evidence.json",
        "16 requests/min client-side cap",
        "the GPU is the provider's",
        "(dry run: nothing was executed)",
    ):
        assert needle in out, needle
    # sizes are the operator's: every golden case (0 = all), a slower key, another world or model
    env.update(WAPSI_EP_FULL="1", WAPSI_LLM_RPM="8", WAPSI_LLM_MODEL="Qwen3.8-27B", WAPSI_E2E_WORLD="full")
    custom = _sh(bash, "run_all.sh", "--mode", "endpoint", "--dry-run", "--date", "2026-10-14", "--stage", "eval,bench,ab",
                 env=env).stdout
    assert "golden_eval --endpoint --world full --limit 0 --out" in custom and "--model Qwen3.8-27B" in custom
    assert "--rpm 8" in custom and "e2e_bench --endpoint --world full --limit 20" in custom
    assert "ab_hallucination --endpoint --world full --limit 0" in custom and "eval 0 cases, A/B 0, e2e 20 (0 = all)" in custom
    assert not (ROOT / "editions" / "amd" / "evidence" / "endpoint" / "2026-10-14" / "stages").exists()  # a dry run writes nothing


def test_notebook_dry_run_is_plain_python_without_docker(bash):
    env = _bash_env(SCRATCH="/scratch", WAPSI_PYTHON="python3", HF_TOKEN="hf_TESTONLY_never_print_this")
    a = _sh(bash, "run_all.sh", "--mode", "notebook", "--dry-run", "--force", "--date", "2026-10-15", env=env)
    b = _sh(bash, "run_all.sh", "--mode", "notebook", "--dry-run", "--force", "--date", "2026-10-15", env=env)
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout and "hf_TESTONLY" not in a.stdout
    out = a.stdout
    assert _headers(out) == STAGES and "not part of notebook mode" not in out
    commands = " ".join(line for line in out.splitlines() if line.startswith("  $"))
    for forbidden in ("docker", "/work", "uvx", "pull_and_pin", "DROPLET"):
        assert forbidden not in commands, forbidden
    for needle in (
        "editions/amd/evidence/notebook/2026-10-15",
        "preflight.sh --mode notebook --json",
        "stagelog.py torch-info editions/amd/evidence/notebook/2026-10-15/torch_info.json",
        "python3 -m pip install -q --no-deps -r /scratch/wapsi-work/requirements-rocm.txt",
        "grep -Ev '^(torch|triton|pytorch-triton|nvidia-|cuda-)'",  # the image's ROCm torch is never replaced
        "editions.amd.kickoff.train_image_check --require-gpu",
        "model_plan.py --mode notebook --torch-info",
        "snapshot_download",
        "editions.amd.gnn.train --worlds",
        "--device cuda",
        "bash editions/amd/serving/start_llm.sh python gemma4-base",
        "VLLM_HOST=127.0.0.1",
        "editions.amd.lora.train --config gemma4-31b-mi300x --data /scratch/wapsi-work/lora-data",
        "editions.amd.lora.merge --run /scratch/wapsi-work/lora/run --out /scratch/models/wapsi-gemma-4-31b-str-merged",
        "editions.amd.lora.evaluate --model base=hf:google/gemma-4-31B-it",
        "editions.amd.gnn.service",
        "gpu_telemetry.py --interval 2",
        "--no-gpu-cost",  # no price for the notebook's hour: the cost per case stays empty
        "editions.amd.kickoff.golden_eval --llm-url http://127.0.0.1:8000/v1",
        "collect_evidence.py --evidence-dir editions/amd/evidence/notebook/2026-10-15 --mode notebook",
    ):
        assert needle in out, needle
    # without vLLM the same stages serve with transformers, one request at a time; with a price the cost is computed
    env.update(WAPSI_NB_SERVER="transformers", WAPSI_GPU_HOUR_USD="2.5")
    tr = _sh(bash, "run_all.sh", "--mode", "notebook", "--dry-run", "--force", "--date", "2026-10-15", "--from", "serve",
             env=env).stdout
    assert "editions.amd.lora.mock_endpoint --llm hf:/scratch/models/wapsi-gemma-4-31b-str-merged" in tr
    assert "start_llm.sh" not in tr and "transformers (no vLLM in this notebook: one request at a time)" in tr
    assert "--concurrency 1 --input-len 1024" in tr and "--gpu-hour-usd 2.5" in tr and "--no-gpu-cost" not in tr
    assert "llm_bench_verifier" not in tr  # no second model next to a single-stream server


def test_modes_are_detected_or_explained(bash):
    if shutil.which("amd-smi") or shutil.which("rocm-smi"):
        pytest.skip("this machine has an AMD GPU tool: detection would pick a GPU mode")
    detected = _sh(bash, "preflight.sh", "--detect", env=_bash_env(WAPSI_LLM_BASE_URL=TF_URL))
    assert detected.returncode == 0 and detected.stdout.strip() == "endpoint"
    nothing = _sh(bash, "preflight.sh", "--detect")
    assert nothing.returncode == 1 and "nothing to run on" in nothing.stderr and "--rehearsal" in nothing.stderr
    auto = _sh(bash, "run_all.sh", "--dry-run", "--date", "2026-10-14", "--stage", "evidence",
               env=_bash_env(WAPSI_LLM_BASE_URL=TF_URL))
    assert auto.returncode == 0 and "mode=endpoint (dry run)" in auto.stdout
    lost = _sh(bash, "run_all.sh", "--dry-run", "--date", "2026-10-14")
    assert lost.returncode == 2 and "cannot tell how to run here" in lost.stderr and "--mode endpoint" in lost.stderr
    forced = _sh(bash, "run_all.sh", "--dry-run", "--date", "2026-10-14", "--stage", "evidence",
                 env=_bash_env(WAPSI_MODE="droplet"))
    assert "mode=droplet (dry run)" in forced.stdout


def test_a_gpu_box_sets_a_provider_endpoint_aside(bash):
    env = _bash_env(WAPSI_LLM_BASE_URL=TF_URL, WAPSI_LLM_API_KEY=KEY, WAPSI_LLM_MODEL=GEMMA, SCRATCH="/scratch")
    out = _sh(bash, "run_all.sh", "--mode", "notebook", "--dry-run", "--date", "2026-10-15", "--stage", "serve",
              env=env).stdout
    assert f"the provider endpoint in this shell ({TF_URL}) is set aside" in out
    assert "WAPSI_LLM_MODEL=/scratch/models/wapsi-gemma-4-31b-str-merged" in out  # the checkpoint, not the endpoint id
    assert KEY not in out and "WAPSI_LLM_MODEL=Gemma-4-31B-IT" not in out


def test_endpoint_run_needs_its_endpoint(bash):
    res = _sh(bash, "run_all.sh", "--mode", "endpoint", "--date", "2099-01-01", "--no-baseline-check",
              env=_bash_env(WAPSI_NOW="2026-10-14T10:00:00Z"))
    assert res.returncode == 2 and "endpoint mode needs WAPSI_LLM_BASE_URL" in res.stderr
    assert not (ROOT / "editions" / "amd" / "evidence" / "endpoint" / "2099-01-01").exists()


def test_preflight_endpoint_table_offline(bash, tmp_path):
    env = _bash_env(WAPSI_LLM_BASE_URL=TF_URL, WAPSI_LLM_API_KEY=KEY, WAPSI_NOW="2026-10-14T10:00:00Z")
    res = _sh(bash, "preflight.sh", "--mode", "endpoint", "--offline", "--json", (tmp_path / "pf.json").as_posix(), env=env)
    assert KEY not in res.stdout and KEY not in res.stderr
    assert "endpoint mode, no GPU box" in res.stdout and "set in this shell (value not shown)" in res.stdout
    data = json.loads((tmp_path / "pf.json").read_text(encoding="utf-8"))
    rows = {c["check"]: c["status"] for c in data["checks"]}
    assert data["mode"] == "endpoint" and rows["endpoint base URL"] == "PASS" and rows["endpoint API key"] == "PASS"
    assert rows["endpoint /models"] == "SKIP" and rows["GPU"] == "SKIP" and rows["docker"] == "SKIP"
    assert rows["HF_TOKEN"] == "SKIP" and rows["event window"] == "PASS" and rows["golden cases"] == "PASS"
    assert "AMLworld HI-Small" not in rows and "droplet start" not in rows  # nothing of the GPU run is asked for
    assert res.returncode == (1 if data["fails"] else 0)
    nokey = _sh(bash, "preflight.sh", "--mode", "endpoint", "--offline", env=_bash_env(WAPSI_LLM_BASE_URL=TF_URL))
    assert nokey.returncode == 1 and "read -rs WAPSI_LLM_API_KEY" in nokey.stdout
    bad = _sh(bash, "preflight.sh", "--mode", "cluster")
    assert bad.returncode == 64 and "unknown mode" in bad.stderr


def test_start_llm_python_mode_serves_without_docker(bash):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "WAPSI_"))}
    res = subprocess.run([bash, (ROOT / "editions/amd/serving/start_llm.sh").as_posix(), "print", "gemma4-base"],
                         capture_output=True, text=True, env={**env, "VLLM_API_KEY": "s3cret-key"}, timeout=60)
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if "vllm.entrypoints.openai.api_server" in ln)
    assert line.startswith("python3 -m vllm.entrypoints.openai.api_server --model google/gemma-4-31B-it --host 127.0.0.1 ")
    for flag in ("--served-model-name wapsi-llm", "--attention-backend TRITON_ATTN", "--gpu-memory-utilization 0.55"):
        assert flag in line, flag
    assert "s3cret-key" not in res.stdout and "docker" not in line


# --------------------------------------------------------------------------- the notebook


def test_notebook_is_valid_and_calls_the_same_scripts():
    path = KICK / "wapsi_amd_kickoff.ipynb"
    text = path.read_text(encoding="utf-8")
    assert text == make_notebook.render()  # the committed file is the generator's output
    assert make_notebook.main(["--check"]) == 0
    nb = json.loads(text)
    assert nb["nbformat"] == 4 and nb["nbformat_minor"] == 5 and nb["metadata"]["kernelspec"]["name"] == "python3"
    ids = [c["id"] for c in nb["cells"]]
    assert len(ids) == len(set(ids)) and all(i.replace("-", "").replace("_", "").isalnum() and len(i) <= 64 for i in ids)
    code = []
    for cell in nb["cells"]:
        assert cell["cell_type"] in ("markdown", "code") and cell["metadata"] == {}
        assert isinstance(cell["source"], list) and all(isinstance(s, str) for s in cell["source"])
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None  # no outputs are stored
            ast.parse("".join(cell["source"]))  # plain python: no shell magics
            code.append("".join(cell["source"]))
        else:
            assert "outputs" not in cell
    src = "\n".join(code)
    assert "editions/amd/kickoff/run_all.sh" in src and "editions/amd/kickoff/preflight.sh" in src
    assert '"--mode", MODE' in src and 'MODE = "notebook"' in src and '"--dry-run"' in src
    staged = [name for call in ast.walk(ast.parse(src)) if isinstance(call, ast.Call)
              and getattr(call.func, "id", "") == "stage" for name in (a.value for a in call.args)]
    assert staged == STAGES  # every stage of the pipeline, in order, and nothing else
    assert "getpass.getpass" in src and "print(value" not in src and "hf_" not in text and "rc-" not in text
    for forbidden in ("docker", "pip install", "import torch", "vllm"):  # no logic of its own
        assert forbidden not in src, forbidden
    try:
        import nbformat
    except ImportError:
        return
    nbformat.validate(nbformat.reads(text, as_version=4))


# --------------------------------------------------------------------------- endpoint mode end to end (marked)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.rehearsal
def test_endpoint_mode_end_to_end_against_a_local_stub(bash, tmp_path):
    """run_all.sh --mode endpoint, for real, against the repository's mock OpenAI server on loopback: stage records,
    markers, skipped stages, the bundle. A loopback endpoint has no AMD identity, so the bundle is invalid by design
    and fill_pitch.py refuses it: the plumbing is proven and no stub number can reach the pitch."""
    port = _free_port()
    server = subprocess.Popen([sys.executable, "-m", "editions.amd.lora.mock_endpoint", "--port", str(port), "--llm",
                               "fake", "--served-name", "stub-gemma"], cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"})
    try:
        url = f"http://127.0.0.1:{port}/v1"
        assert stagelog.wait_http(f"{url}/models", 60, 0.5)
        env = _bash_env(WAPSI_LLM_BASE_URL=url, WAPSI_LLM_MODEL="stub-gemma", WAPSI_NOW="2026-10-14T10:00:00Z",
                        WAPSI_E2E_WORLD="showcase-memory", WAPSI_SKIP_DOCS="1", WAPSI_LLM_RPM="600",
                        WAPSI_EP_BENCH_LEVELS="1 2", WAPSI_EP_BENCH_REQUESTS="2",
                        WAPSI_LLM_CACHE_DIR=(tmp_path / "cache").as_posix())
        args = ["--mode", "endpoint", "--skip-preflight", "--date", "2026-10-14", "--evidence-root",
                (tmp_path / "evidence").as_posix(), "--work", (tmp_path / "work").as_posix()]
        res = _sh(bash, "run_all.sh", *args, env=env, timeout=900)
        assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]
    finally:
        server.terminate()
        server.wait(10)
    evid = tmp_path / "evidence" / "endpoint" / "2026-10-14"
    recs = {p.stem.split("-", 1)[1]: json.loads(p.read_text(encoding="utf-8")) for p in (evid / "stages").glob("*.json")}
    assert set(recs) == set(STAGES)
    assert {n for n, r in recs.items() if r["status"] == "skipped"} == {"gnn", "lora-data", "lora-train", "merge",
                                                                        "lora-eval", "serve"}
    for name in ("env", "models", "eval", "bench", "ab", "evidence"):
        assert recs[name]["status"] == "ok" and recs[name]["mode"] == "endpoint", name
        assert (evid / "stages" / f"{STAGES.index(name) + 1:02d}-{name}.ok").exists()
        assert recs[name]["budget"]["metered"] is False
    assert not (evid / "amd_smi").exists()  # no GPU here: nothing to snapshot
    assert recs["models"]["notes"]["drafting_model"] == "stub-gemma"
    for rel in ("endpoint.json", "model_plan.json", "golden_eval.json", "ab_hallucination.json", "bench/llm_bench.json",
                "bench/llm_bench_wapsi_prompts.json", "bench/e2e_c2.json", "evidence.json", "evidence.md"):
        assert (evid / rel).exists(), rel
    ev = json.loads((evid / "evidence.json").read_text(encoding="utf-8"))
    assert ev["mode"] == "endpoint" and ev["headline"]["model_id"] == "stub-gemma" and ev["headline"]["golden_cases"] == 2
    assert ev["results_valid"] is False and any("notebook or droplet mode" in r for r in ev["invalid_reasons"])
    assert "REFUSED" in res.stdout and "results_valid is false" in res.stdout  # fill_pitch.py would not take it
    again = _sh(bash, "run_all.sh", *args, "--dry-run", env=env)
    assert again.stdout.count("done earlier") == 6 and not (tmp_path / "evidence" / "endpoint" / ".active").exists()
