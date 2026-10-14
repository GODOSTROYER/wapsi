"""The prose of the drafts fails over along ``WAPSI_LLM_MODELS`` when a model answers in garbage or is withdrawn.

On 2026-10-14 the AMD Token Factory's ``Gemma-4-31B-IT`` answered HTTP 200 with runs of ``/`` for about half an hour,
then HTTP 400 "model not supported", while ``DeepSeek-V4-Flash`` on the same endpoint answered normally. These tests
replay both failures offline (no network): the next model writes the prose, every draft names the model that wrote
it, a model that wrote garbage is not asked again during its cool-down, and the account's own limits (429, a bad
key) are never "fixed" by switching models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from wapsi.adapters.llm_failover import AllModelsFailed, FailoverLLM, account_level, build
from wapsi.adapters.llm_openai_compat import LLMError
from wapsi.intake.llm_chain import HealthRegistry

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # editions/ and ui/ are imported from the repository root
    sys.path.insert(0, str(ROOT))

TF = "https://developer.amd.com.cn/radeon/api/v1"
GEMMA, DEEPSEEK, QWEN = "Gemma-4-31B-IT", "DeepSeek-V4-Flash", "Qwen3.8-27B"
GARBAGE = '{"' + "/" * 60
MESSAGES = [{"role": "system", "content": "You write one paragraph."}, {"role": "user", "content": "Write it."}]


class Model:
    """A model on the endpoint: answers with ``text``, or raises ``error``."""

    def __init__(self, model: str, text: str = '{"text": "The amount traced to the account is held."}',
                 error: Exception | None = None) -> None:
        self.model, self.base_url, self.text, self.error = model, TF, text, error
        self.name = f"{model} on AMD @ developer.amd.com.cn"
        self.calls = 0

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, json_schema=None) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def chain(*models: Model, clock: Clock | None = None, **kw) -> FailoverLLM:
    return FailoverLLM(list(models), registry=HealthRegistry(clock or Clock()), **kw)


def test_garbage_from_the_first_model_is_answered_by_the_next_one_and_the_first_rests():
    gemma, deepseek = Model(GEMMA, GARBAGE), Model(DEEPSEEK)
    clock = Clock()
    llm = chain(gemma, deepseek, clock=clock, cooldown_s=600)
    assert llm.name == gemma.name and llm.model == GEMMA  # before any answer: the configured model
    assert llm.complete(MESSAGES) == deepseek.text
    assert llm.name == deepseek.name and llm.last_model == DEEPSEEK  # the draft will name DeepSeek
    llm.complete(MESSAGES)
    assert gemma.calls == 1 and deepseek.calls == 2  # garbage once: not asked again while it rests
    clock.now = 601
    gemma.text = deepseek.text  # the deployment recovered
    llm.complete(MESSAGES)
    assert gemma.calls == 2 and llm.name == gemma.name
    rep = llm.report()
    assert rep["answers_by_model"] == {DEEPSEEK: 2, GEMMA: 1} and rep["rejected_by_model"] == {GEMMA: 1}
    assert rep["skipped_by_model"] == {GEMMA: 1} and rep["failovers"] == 1 and rep["model_id"] == GEMMA
    assert rep["failover_events"][0]["outcome"] == "garbage"


def test_a_withdrawn_model_fails_over_and_comes_back_sooner_than_a_garbage_one():
    refused = LLMError("HTTP 400: Requested model Gemma-4-31B-IT not supported", status_code=400)
    gemma, deepseek = Model(GEMMA, error=refused), Model(DEEPSEEK)
    clock = Clock()
    llm = chain(gemma, deepseek, clock=clock, cooldown_s=600, error_cooldown_s=60)
    assert llm.complete(MESSAGES) == deepseek.text and llm.report()["errors_by_model"] == {GEMMA: 1}
    clock.now = 61
    gemma.error = None
    llm.complete(MESSAGES)
    assert gemma.calls == 2 and llm.name == gemma.name


def test_the_accept_check_rejects_an_answer_the_detector_alone_would_pass():
    gemma, deepseek = Model(GEMMA, '{"text": "{not a paragraph"}'), Model(DEEPSEEK)
    llm = chain(gemma, deepseek, accept=lambda messages, text: "starts with JSON" if "{not" in text else None)
    assert llm.complete(MESSAGES) == deepseek.text and llm.report()["rejected_by_model"] == {GEMMA: 1}


@pytest.mark.parametrize("error", [
    LLMError("HTTP 429: Rate limit exceeded: maximum 20 requests per minute", status_code=429),
    LLMError("HTTP 401: Invalid API key", status_code=401),
])
def test_the_account_limits_and_the_key_are_not_fixed_by_switching_models(error):
    gemma, deepseek = Model(GEMMA, error=error), Model(DEEPSEEK)
    llm = chain(gemma, deepseek)
    assert account_level(error)
    with pytest.raises(LLMError):
        llm.complete(MESSAGES)
    assert deepseek.calls == 0


def test_when_every_model_fails_the_caller_gets_one_error_naming_each():
    llm = chain(Model(GEMMA, GARBAGE), Model(DEEPSEEK, error=TimeoutError("read timeout")))
    with pytest.raises(AllModelsFailed) as info:
        llm.complete(MESSAGES)
    assert GEMMA in str(info.value) and DEEPSEEK in str(info.value)
    # every model is resting now: the one due back soonest still gets one honest attempt
    retry = Model(DEEPSEEK)
    llm.llms[1] = retry
    assert llm.complete(MESSAGES) == retry.text


def test_one_model_is_returned_unwrapped():
    only = Model(GEMMA)
    assert build([only]) is only
    assert isinstance(build([only, Model(DEEPSEEK)]), FailoverLLM)
    with pytest.raises(ValueError):
        FailoverLLM([])


# --------------------------------------------------------------------------- the drafts


def test_drafts_written_while_gemma_answers_in_garbage_are_written_by_the_next_model_and_verified():
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.adapters.llm_replay import prose_answer_problem
    from wapsi.draft.drafter import draft_case
    from wapsi.fixtures import showcase as sc
    from wapsi.fixtures.bundles import showcase_bundle
    from wapsi.verify import verify_draft

    gemma = Model(GEMMA, GARBAGE)
    deepseek = FakeLLM(name=f"{DEEPSEEK} on AMD @ developer.amd.com.cn")
    llm = chain(gemma, deepseek, accept=prose_answer_problem)
    ledger, bundle = sc.showcase_ledger(), showcase_bundle(1)
    bundle.drafts = draft_case(bundle, ledger, llm)
    written = [d for d in bundle.drafts if d.llm_model]
    assert written and gemma.calls == 1  # one garbage answer, then Gemma rests
    for d in written:
        assert d.llm_model == deepseek.name and not d.structured.get("llm_fallback"), d.kind
        assert "////" not in d.body
    for d in bundle.drafts:
        assert verify_draft(d, bundle, ledger).passed, d.kind
    rep = llm.report()
    assert rep["rejected_by_model"] == {GEMMA: 1} and sum(rep["answers_by_model"].values()) >= len(written)
    assert GEMMA not in rep["answers_by_model"]


# --------------------------------------------------------------------------- built from the environment


def _transport(answers: dict[str, str], seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": answers[model]},
                                                      "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 1e-6}})

    return httpx.MockTransport(handler)


def test_resilient_endpoint_llm_fails_over_along_the_list_with_one_rate_limiter(tmp_path):
    from wapsi.adapters.llm_env import (
        chain_models,
        endpoint_config,
        make_endpoint_llm,
        resilient_endpoint_llm,
    )
    from wapsi.adapters.llm_resilience import ResilientLLM

    env = {"WAPSI_LLM_BASE_URL": TF, "WAPSI_LLM_API_KEY": "test-key", "WAPSI_LLM_MODEL": GEMMA,
           "WAPSI_LLM_MODELS": f"{GEMMA}, {DEEPSEEK}, {QWEN}"}
    cfg = endpoint_config(env)
    assert chain_models(cfg, env) == [GEMMA, DEEPSEEK, QWEN]
    assert chain_models(cfg, {**env, "WAPSI_LLM_MODELS": ""}) == [GEMMA]  # no list: no substitute
    assert chain_models(cfg, {"WAPSI_LLM_FALLBACK_MODELS": QWEN}) == [GEMMA, QWEN]  # the kickoff's spelling
    seen: list[str] = []
    transport = _transport({GEMMA: GARBAGE, DEEPSEEK: '{"text": "Held."}', QWEN: '{"text": "Held too."}'}, seen)

    def make(c):
        return make_endpoint_llm(c, env=env, transport=transport)

    single = resilient_endpoint_llm(cfg, env=env, inner=make(cfg), cache_dir=tmp_path / "c1", rpm=0)
    assert isinstance(single, ResilientLLM)  # failover is asked for, never assumed
    llm = resilient_endpoint_llm(cfg, env=env, inner=make(cfg), make_inner=make, cache_dir=tmp_path / "c2", rpm=0,
                                 failover=True)
    assert isinstance(llm, FailoverLLM) and llm.models == [GEMMA, DEEPSEEK, QWEN]
    assert len({id(m.limiter) for m in llm.llms}) == 1  # the endpoint's limit is per account
    assert llm.complete(MESSAGES) == '{"text": "Held."}' and seen == [GEMMA, DEEPSEEK]
    assert llm.name == f"{DEEPSEEK} on AMD @ developer.amd.com.cn"
    rep = llm.report()
    assert rep["unusable_answers"] == 0 and rep["answers_by_model"] == {DEEPSEEK: 1}
    assert rep["rejected_by_model"] == {GEMMA: 1} and "test-key" not in json.dumps(rep)
    assert llm.label.startswith(f"{GEMMA} on AMD @ developer.amd.com.cn; failover: {DEEPSEEK}, {QWEN}")


def test_the_amd_app_builds_one_client_per_listed_model():
    from editions.amd.app.services import build_services
    from editions.amd.app.settings import load_settings

    env = {"WAPSI_LLM_BASE_URL": TF, "WAPSI_LLM_MODEL": GEMMA, "WAPSI_LLM_MODELS": f"{GEMMA},{DEEPSEEK}"}
    settings = load_settings(env)
    assert settings.llm_fallback_models == (DEEPSEEK,) and settings.describe()["llm_fallback_models"] == [DEEPSEEK]
    svc = build_services(settings, search=False)
    assert isinstance(svc.llm, FailoverLLM) and svc.llm.models == [GEMMA, DEEPSEEK]
    assert svc.llm.name == f"{GEMMA} on AMD @ developer.amd.com.cn"
    assert load_settings({**env, "WAPSI_LLM_MODELS": ""}).llm_fallback_models == ()
    # voice intake gets the first drafting client and builds its own chain along the same list
    from editions.amd.app.services import build_voice

    voice = build_voice(svc, {**env, "WAPSI_VOICE_BACKEND": "open", "WAPSI_ASR_BACKEND": "fake"})
    chain = voice.extractor.chain
    assert chain.models == [GEMMA, DEEPSEEK] and chain.llms[0] is svc.llm.primary


def test_the_ui_live_endpoint_fails_over_when_a_list_is_set(monkeypatch, tmp_path):
    from ui import data_source

    for name, value in {"WAPSI_LLM_BASE_URL": TF, "WAPSI_LLM_API_KEY": "test-key", "WAPSI_LLM_MODEL": GEMMA,
                        "WAPSI_LLM_MODELS": f"{GEMMA},{DEEPSEEK}", data_source.LLM_CACHE_ENV: str(tmp_path)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(data_source, "_LLM_STATE", {})
    cached, fresh = data_source._live_llm(False), data_source._live_llm(True)
    assert isinstance(cached, FailoverLLM) and cached.models == [GEMMA, DEEPSEEK]
    assert cached.accept is not None and all(m.limiter is cached.llms[0].limiter for m in [*cached.llms, *fresh.llms])
    assert all(m.read_cache is False for m in fresh.llms)
