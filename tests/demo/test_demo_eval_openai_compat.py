"""``python -m evals.harness --llm openai-compat``: the env-configured OpenAI-compatible endpoint (AMD Token Factory,
vLLM on AMD) behind ``ResilientLLM``. A mocked endpoint answers HTTP 429 + Retry-After, then succeeds; the rerun is
served from the prompt-hash cache. No network, no real key."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from evals import harness
from wapsi.adapters.llm_env import API_KEY_ENV, BASE_URL_ENV, MODEL_ENV, SETUP_MESSAGE, make_endpoint_llm
from wapsi.adapters.llm_fake import FakeLLM

SENTINEL = "rc-SENTINEL-eval-key-never-printed-0123456789"
HOST = "developer.amd.com.cn"


class Endpoint:
    """Answers like an OpenAI-compatible server: prose requests (JSON mode) get FakeLLM's valid placeholder-only
    prose; the plain-text LLM-only baseline gets a summary with one invented rupee amount."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.models: set[str] = set()
        self.auth: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = json.loads(request.content)
        self.models.add(body["model"])
        self.auth.add(request.headers.get("authorization", ""))
        if self.fail_first and self.calls == 1:
            return httpx.Response(429, json={"detail": {"error": {"message": "rate limit exceeded: 20 rpm"}}},
                                  headers={"Retry-After": "7"})
        if body.get("response_format"):
            text = FakeLLM().complete(body["messages"], json_schema={"type": "object"})
        else:
            text = "The victims lost money; Rs 12,345.67 was moved through the mule accounts."
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 60, "total_tokens": 460, "cost": 0.00002}})


@pytest.fixture()
def showcase_world(tmp_path: Path) -> Path:
    from wapsi.synth.__main__ import main as synth

    out = tmp_path / "showcase"
    assert synth(["--size", "showcase", "--out", str(out), "--golden-dir", ""]) == 0
    return out


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV, "WAPSI_LLM_JSON_MODE", "WAPSI_LLM_EXTRA_BODY",
                 "WAPSI_LLM_PLATFORM", "WAPSI_LLM_RPM", "WAPSI_LLM_RPD", "WAPSI_LLM_MIN_INTERVAL_S",
                 "WAPSI_LLM_PRICE_IN", "WAPSI_LLM_PRICE_OUT", "WAPSI_LLM_MODELS", "WAPSI_LLM_FALLBACK_MODELS"):
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, endpoint: Endpoint) -> None:
    monkeypatch.setenv(BASE_URL_ENV, f"https://{HOST}/radeon/api/v1")
    monkeypatch.setenv(API_KEY_ENV, SENTINEL)
    monkeypatch.setenv(MODEL_ENV, "Gemma-4-31B-IT")
    monkeypatch.setattr(harness, "make_endpoint",
                        lambda cfg: make_endpoint_llm(cfg, transport=httpx.MockTransport(endpoint)))


def test_unconfigured_endpoint_fails_fast_with_the_setup_message():
    for kind in ("openai-compat", "endpoint", "openai-compat:Qwen3.8-27B"):
        with pytest.raises(SystemExit) as e:
            harness.make_llm(kind, cache_dir=None)
        assert str(e.value) == SETUP_MESSAGE and BASE_URL_ENV in SETUP_MESSAGE and "rc-" not in SETUP_MESSAGE


def test_make_llm_wraps_the_env_configured_endpoint(monkeypatch):
    configure(monkeypatch, Endpoint())
    llm = harness.make_llm("openai-compat", cache_dir=None, rpm=5, min_interval_s=4.0)
    assert llm.model_id == "Gemma-4-31B-IT" and llm.backend == "openai-compatible"
    assert llm.name == f"Gemma-4-31B-IT on AMD @ {HOST}" and llm.inner.max_retries == 0  # ResilientLLM retries
    assert (llm.limiter.rpm, llm.limiter.rpd, llm.limiter.min_interval_s) == (5, 0, 4.0)
    assert llm.cache_namespace == HOST
    assert SENTINEL not in repr(llm) and SENTINEL not in json.dumps(llm.report())
    other = harness.make_llm("openai-compat:Qwen3.8-27B", cache_dir=None)
    assert other.model_id == "Qwen3.8-27B" and other.name == f"Qwen3.8-27B on AMD @ {HOST}"
    assert harness.is_endpoint_kind("endpoint") and not harness.is_endpoint_kind("openai:http://x|m")
    assert harness.report_name("demo", "heuristic", "openai-compat") == "demo-openai-compat"
    assert harness.report_name("demo", "heuristic", "openai-compat:Qwen3.8-27B") == "demo-openai-compat"
    assert harness.report_name("demo", "heuristic", "fake") == "latest"


def test_harness_openai_compat_429_then_success_and_cached_rerun(monkeypatch, tmp_path, showcase_world, capsys):
    endpoint = Endpoint(fail_first=True)
    configure(monkeypatch, endpoint)
    slept: list[float] = []
    monkeypatch.setattr("wapsi.adapters.llm_resilience.time.sleep", slept.append)
    out, cache = tmp_path / "reports", tmp_path / "cache"
    argv = ["--world", "showcase", "--world-dir", str(showcase_world), "--llm", "openai-compat", "--out", str(out),
            "--cache-dir", str(cache), "--rpm", "0"]

    assert harness.main(argv) == 0
    rep = json.loads((out / "showcase-openai-compat.json").read_text(encoding="utf-8"))
    assert not (out / "latest.json").exists()  # a real-model run never replaces the offline reference report
    llm, m, ab = rep["llm"], rep["metrics"], rep["ab_test"]
    assert rep["cases"] == 2 and rep["config"]["llm"] == f"Gemma-4-31B-IT on AMD @ {HOST}"
    assert llm["model_id"] == "Gemma-4-31B-IT" and llm["backend"] == "openai-compatible"
    assert llm["endpoint_host"] == HOST and endpoint.models == {"Gemma-4-31B-IT"}
    assert endpoint.auth == {f"Bearer {SENTINEL}"}
    assert llm["rate_limited_429"] == 1 and llm["retries"] == 1 and llm["errors"] == 0 and slept == [7.0]
    assert llm["api_calls"] == llm["requests"] + 1 == endpoint.calls and llm["cache_hits"] == 0
    assert llm["tokens"]["prompt_tokens"] == 400 * llm["ok"] and llm["tokens"]["output_tokens"] == 60 * llm["ok"]
    cost = llm["cost_estimate_usd"]
    assert cost["this_run"] == pytest.approx(0.00002 * llm["ok"]) and cost["paid_tier_equivalent"] is None
    assert "reported by the endpoint" in cost["basis"]
    assert m["llm_prose_fallback_rate"] == 0.0 and m["draft_verification_pass_rate"] == 1.0
    assert list(rep["prose_by_model"]) == [f"Gemma-4-31B-IT on AMD @ {HOST}"]  # who wrote the prose
    assert m["hold_exact_match_rate"] == 1.0 and m["llm_only_hallucination_rate"] == 1.0
    assert ab["llm_only"]["hallucination_rate"] == 1.0 and ab["wapsi_bound"]["hallucination_rate"] == 0.0
    md = capsys.readouterr().out
    assert "## LLM" in md and f"openai-compatible @ {HOST}" in md and "## A/B: raw LLM vs Wapsi-bound drafts" in md
    assert "Cost this run: $" in md and "reported by the endpoint" in md
    first_calls = endpoint.calls

    # rerun: every answer comes from the prompt-hash cache, so it sends nothing and is identical
    assert harness.main(argv) == 0
    again = json.loads((out / "showcase-openai-compat.json").read_text(encoding="utf-8"))
    assert endpoint.calls == first_calls
    assert again["llm"]["api_calls"] == 0 and again["llm"]["cache_hits"] == again["llm"]["requests"] > 0
    assert again["llm"]["cache_hit_rate"] == 1.0 and again["llm"]["tokens"]["total_tokens"] == 0
    assert again["metrics"] == {**rep["metrics"], **{k: again["metrics"][k] for k in again["metrics"]
                                                     if k.startswith(("p50_", "p95_"))}}
    assert again["ab_test"] == rep["ab_test"]
    for path in out.iterdir():  # reports are safe to publish
        assert SENTINEL not in path.read_text(encoding="utf-8")
    assert SENTINEL not in md and not [p for p in cache.rglob("*.json") if SENTINEL in p.read_text(encoding="utf-8")]


def test_a_garbling_endpoint_is_scored_as_it_is(monkeypatch, showcase_world):
    """An evaluation does not hide a broken model: degenerate answers (HTTP 200, a run of slashes) are rejected by
    the prose validator, every paragraph falls back, and the drafts still verify."""

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"' + "/" * 600},
                                                      "finish_reason": "length"}],
                                         "usage": {"prompt_tokens": 400, "completion_tokens": 700}})

    monkeypatch.setenv(BASE_URL_ENV, f"https://{HOST}/radeon/api/v1")
    monkeypatch.setenv(MODEL_ENV, "Gemma-4-31B-IT")
    monkeypatch.setattr(harness, "make_endpoint",
                        lambda cfg: make_endpoint_llm(cfg, transport=httpx.MockTransport(garbage)))
    rep = harness.evaluate("showcase", "openai-compat", world_dir=showcase_world,
                           llm=harness.make_llm("openai-compat", cache_dir=None, rpm=0))
    drafts = rep["ab_test"]["wapsi_bound"]["drafts"]
    assert rep["metrics"]["llm_prose_fallback_rate"] == round(5 / drafts, 4)  # all five prose fields fell back
    assert rep["metrics"]["draft_verification_pass_rate"] == 1.0 and rep["metrics"]["hold_exact_match_rate"] == 1.0
    assert rep["llm"]["errors"] == 0 and rep["llm"]["unusable_answers"] == 0  # no accept hook in an evaluation


def test_harness_fails_over_along_the_model_list_and_counts_prose_per_model(monkeypatch, tmp_path, showcase_world):
    """2026-10-14: Gemma-4-31B-IT answered HTTP 200 with runs of slashes. With ``WAPSI_LLM_MODELS`` set, the next
    model writes the prose; the report says which model wrote how many drafts. Without the list nothing is swapped."""

    class Degraded(Endpoint):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["model"] == "Gemma-4-31B-IT" and body.get("response_format"):
                self.calls += 1
                self.models.add(body["model"])
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                          "content": '{"' + "/" * 80},
                                                              "finish_reason": "length"}],
                                                 "usage": {"prompt_tokens": 400, "completion_tokens": 700}})
            return super().__call__(request)

    endpoint = Degraded()
    configure(monkeypatch, endpoint)
    monkeypatch.setenv("WAPSI_LLM_MODELS", "Gemma-4-31B-IT,DeepSeek-V4-Flash")
    out = tmp_path / "reports"
    argv = ["--world", "showcase", "--world-dir", str(showcase_world), "--llm", "openai-compat", "--out", str(out),
            "--cache-dir", str(tmp_path / "cache"), "--rpm", "0"]
    assert harness.main(argv) == 0
    rep = json.loads((out / "showcase-openai-compat.json").read_text(encoding="utf-8"))
    assert rep["config"]["llm"].endswith("failover: DeepSeek-V4-Flash")
    assert list(rep["prose_by_model"]) == [f"DeepSeek-V4-Flash on AMD @ {HOST}"]
    assert rep["metrics"]["llm_prose_fallback_rate"] == 0.0 and rep["metrics"]["draft_verification_pass_rate"] == 1.0
    assert rep["llm"]["rejected_by_model"] == {"Gemma-4-31B-IT": 1}  # garbage once, then it rests
    assert "DeepSeek-V4-Flash" in rep["llm"]["answers_by_model"] and SENTINEL not in json.dumps(rep)
