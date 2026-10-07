"""``python -m evals.harness --llm gemini`` with a mocked google-genai client: free-tier 429 then success, the
prompt-hash cache makes the rerun free, and the report carries model id, tokens, cost, fallback / verification rates
and the raw-LLM-vs-Wapsi A/B. No network, no key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from evals import harness
from wapsi.adapters.gemini_llm import MISSING_KEY_MESSAGE, GeminiLLM
from wapsi.adapters.llm_fake import FakeLLM

SENTINEL = "AIzaSy-SENTINEL-eval-key-never-printed-0123"


def free_tier_429() -> genai_errors.APIError:
    return genai_errors.ClientError(429, {"error": {
        "code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})


class FakeModels:
    """Answers like Gemini would: JSON prose requests are answered by FakeLLM (valid, placeholder-only prose);
    the plain-text LLM-only baseline gets a summary with one invented rupee amount."""

    def __init__(self, fail_first: bool) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.models: set[str] = set()

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.models.add(model)
        if self.fail_first and self.calls == 1:
            raise free_tier_429()
        messages = [{"role": "system", "content": config.get("system_instruction", "")}]
        for c in contents:
            role = "assistant" if c["role"] == "model" else "user"
            messages += [{"role": role, "content": p["text"]} for p in c["parts"]]
        if config.get("response_json_schema"):
            text = FakeLLM().complete(messages, json_schema=config["response_json_schema"])
        else:
            text = "The victims lost money; Rs 12,345.67 was moved through the mule accounts."
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]),
                                        finish_reason="STOP")],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=400, candidates_token_count=60, thoughts_token_count=10, total_token_count=470))


class FakeClient:
    def __init__(self, fail_first: bool = False) -> None:
        self.models = FakeModels(fail_first)


@pytest.fixture()
def showcase_world(tmp_path: Path) -> Path:
    from wapsi.synth.__main__ import main as synth

    out = tmp_path / "showcase"
    assert synth(["--size", "showcase", "--out", str(out), "--golden-dir", ""]) == 0
    return out


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "WAPSI_GEMINI_BACKEND", "WAPSI_MODEL", "WAPSI_LLM_RPM",
                 "WAPSI_LLM_RPD"):
        monkeypatch.delenv(name, raising=False)


def test_gemini_without_a_key_fails_fast_with_the_setup_message(monkeypatch):
    with pytest.raises(SystemExit) as e:
        harness.make_llm("gemini", cache_dir=None)
    assert str(e.value) == MISSING_KEY_MESSAGE


def test_make_llm_gemini_wraps_the_adapter(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SENTINEL)
    llm = harness.make_llm("gemini:gemini-3.5-flash-lite", cache_dir=None, rpm=5, rpd=50)
    assert llm.model_id == "gemini-3.5-flash-lite" and llm.backend == "aistudio"
    assert isinstance(llm.inner, GeminiLLM) and llm.inner.max_retries == 0  # ResilientLLM owns the retries
    assert (llm.limiter.rpm, llm.limiter.rpd) == (5, 50)
    assert SENTINEL not in repr(llm) and SENTINEL not in json.dumps(llm.report())
    assert harness.report_name("demo", "heuristic", "gemini") == "demo-gemini"
    assert harness.report_name("hard", "heuristic", "gemini:gemini-3.8-flash") == "hard-gemini"
    assert harness.report_name("demo", "heuristic", "fake") == "latest"


def test_harness_gemini_429_then_success_and_cached_rerun(monkeypatch, tmp_path, showcase_world, capsys):
    client = FakeClient(fail_first=True)
    monkeypatch.setattr(harness, "make_gemini",
                        lambda model=None, **kw: GeminiLLM(model or "gemini-3.8-flash", client=client, max_retries=0))
    slept: list[float] = []
    monkeypatch.setattr("wapsi.adapters.llm_resilience.time.sleep", slept.append)
    out, cache = tmp_path / "reports", tmp_path / "cache"
    argv = ["--world", "showcase", "--world-dir", str(showcase_world), "--llm", "gemini", "--out", str(out),
            "--cache-dir", str(cache), "--rpm", "0"]

    assert harness.main(argv) == 0
    rep = json.loads((out / "showcase-gemini.json").read_text(encoding="utf-8"))
    assert not (out / "latest.json").exists()  # a real-model run never replaces the offline reference report
    llm, m, ab = rep["llm"], rep["metrics"], rep["ab_test"]
    assert llm["model_id"] == "gemini-3.8-flash" and llm["backend"] == "aistudio" and rep["cases"] == 2
    assert llm["rate_limited_429"] == 1 and llm["retries"] == 1 and llm["errors"] == 0 and slept == [7.0]
    assert llm["api_calls"] == llm["requests"] + 1 and llm["cache_hits"] == 0
    assert llm["tokens"]["prompt_tokens"] == 400 * llm["ok"] and llm["tokens"]["total_tokens"] > 0
    assert llm["cost_estimate_usd"]["this_run"] == 0.0 and llm["cost_estimate_usd"]["paid_tier_equivalent"] > 0
    assert m["llm_prose_fallback_rate"] == 0.0 and m["draft_verification_pass_rate"] == 1.0
    assert m["hold_exact_match_rate"] == 1.0 and m["llm_only_hallucination_rate"] == 1.0
    assert ab["llm_only"]["hallucination_rate"] == 1.0 and ab["wapsi_bound"]["hallucination_rate"] == 0.0
    md = capsys.readouterr().out
    assert "## LLM" in md and "## A/B: raw LLM vs Wapsi-bound drafts" in md and "gemini-3.8-flash" in md
    first_calls = client.models.calls

    # rerun: every answer comes from the prompt-hash cache, so it costs nothing and is identical
    assert harness.main(argv) == 0
    again = json.loads((out / "showcase-gemini.json").read_text(encoding="utf-8"))
    assert client.models.calls == first_calls
    assert again["llm"]["api_calls"] == 0 and again["llm"]["cache_hits"] == again["llm"]["requests"] > 0
    assert again["llm"]["cache_hit_rate"] == 1.0 and again["llm"]["tokens"]["total_tokens"] == 0
    assert again["metrics"] == {**rep["metrics"], **{k: again["metrics"][k] for k in again["metrics"]
                                                     if k.startswith(("p50_", "p95_"))}}
    assert again["ab_test"] == rep["ab_test"]


def test_daily_quota_mid_run_falls_back_and_is_reported(monkeypatch, tmp_path, showcase_world):
    daily = genai_errors.ClientError(429, {"error": {
        "code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})

    class Exhausted:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **kw):
            self.calls += 1
            raise daily

    client = type("C", (), {})()
    client.models = Exhausted()
    monkeypatch.setattr(harness, "make_gemini",
                        lambda model=None, **kw: GeminiLLM("gemini-3.8-flash", client=client, max_retries=0))
    rep = harness.evaluate("showcase", "gemini", world_dir=showcase_world,
                           llm=harness.make_llm("gemini", cache_dir=None, rpm=0))
    assert client.models.calls == 1  # the daily quota is never retried; later calls fail fast without the API
    assert rep["llm"]["quota_exhausted"] is True
    # every LLM-written paragraph fell back to its deterministic version, and the drafts still verify
    offline = harness.evaluate("showcase", "fake", world_dir=showcase_world)
    assert rep["metrics"]["llm_prose_fallback_rate"] > offline["metrics"]["llm_prose_fallback_rate"] == 0.0
    assert rep["metrics"]["draft_verification_pass_rate"] == 1.0


def test_self_hosted_model_report_has_no_gemini_token_price(monkeypatch, showcase_world):
    """``--llm openai:<vLLM url>|<model>`` (the AMD MI300X path): Gemini list prices would be meaningless there, so
    the cost block says n/a instead of printing an invented dollar figure."""
    from wapsi.adapters.llm_resilience import ResilientLLM

    for name in ("WAPSI_LLM_PRICE_IN", "WAPSI_LLM_PRICE_OUT"):
        monkeypatch.delenv(name, raising=False)
    made = harness.make_llm("openai:http://127.0.0.1:9/v1|google/gemma-4-E4B-it", cache_dir=None)
    assert made.backend == "openai-compatible" and made.cache is None
    assert made.report()["cost_estimate_usd"]["this_run"] is None

    llm = ResilientLLM(FakeLLM(), model_id="google/gemma-4-E4B-it", backend="openai-compatible", rpm=0, rpd=0)
    rep = harness.evaluate("showcase", "openai:x|google/gemma-4-E4B-it", world_dir=showcase_world, llm=llm)
    c = rep["llm"]["cost_estimate_usd"]
    assert c["this_run"] is None and c["paid_tier_equivalent"] is None and "self-hosted" in c["basis"]
    assert "Cost this run: n/a" in harness.to_markdown(rep)
    monkeypatch.setenv("WAPSI_LLM_PRICE_IN", "0.1")
    monkeypatch.setenv("WAPSI_LLM_PRICE_OUT", "0.2")
    assert llm.report()["cost_estimate_usd"]["price_per_1m_tokens"] == {"input": 0.1, "output": 0.2}
