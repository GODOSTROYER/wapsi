from __future__ import annotations

import json

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from wapsi.adapters.gemini_llm import (
    DEFAULT_MODEL,
    MISSING_KEY_MESSAGE,
    GeminiLLM,
    PrivateGemmaLLM,
    gemini_api_key,
    gemma_private_llm,
    is_daily_quota,
    parse_json_text,
    quota_hint,
    resolve_backend,
    response_text,
    retry_delay_s,
)
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.llm_openai_compat import LLMError
from wapsi.fixtures import showcase as sc
from wapsi.pipeline.run import process_case
from wapsi.ports import LLM

SCHEMA = {"title": "prose", "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
          "additionalProperties": False}


def reply(text: str | None, finish: str = "STOP") -> types.GenerateContentResponse:
    parts = [types.Part(text=text)] if text is not None else []
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=parts), finish_reason=finish)])


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeClient:
    def __init__(self, *outcomes):
        self.models = FakeModels(outcomes)


def api_error(code: int, message: str = "boom", details: list | None = None) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    body = {"code": code, "message": message, "status": "RESOURCE_EXHAUSTED" if code == 429 else "X"}
    if details is not None:
        body["details"] = details
    return cls(code, {"error": body})


def free_tier_429(per_day: bool = False, retry: str | None = "7s") -> genai_errors.APIError:
    """Shaped like the Gemini API's free-tier 429: QuotaFailure + RetryInfo details."""
    quota_id = ("GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day
                else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
    details: list = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
         "quotaId": quota_id}]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return api_error(429, "You exceeded your current quota, please check your plan and billing details.", details)


@pytest.fixture(autouse=True)
def _no_ambient_gemini_env(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "WAPSI_GEMINI_BACKEND"):
        monkeypatch.delenv(name, raising=False)


MESSAGES = [
    {"role": "system", "content": "You write bank prose."},
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "ok"},
    {"role": "user", "content": "second"},
    {"role": "user", "content": "third"},
]


def test_request_mapping_and_json_mode():
    client = FakeClient(reply('```json\n{"text": "Hold ⟦hold_amount⟧."}\n```'))
    llm = GeminiLLM(client=client, project="p", sleep=lambda s: None)
    assert isinstance(llm, LLM) and llm.backend == "aistudio" and llm.name == f"gemini-api:{DEFAULT_MODEL}"
    out = llm.complete(MESSAGES, temperature=0.2, max_tokens=300, json_schema=SCHEMA)
    assert json.loads(out) == {"text": "Hold ⟦hold_amount⟧."}
    call = client.models.calls[0]
    assert call["model"] == DEFAULT_MODEL == "gemini-3.8-flash"
    assert [c["role"] for c in call["contents"]] == ["user", "model", "user"]
    assert [p["text"] for p in call["contents"][2]["parts"]] == ["second", "third"]
    cfg = call["config"]
    assert cfg["system_instruction"] == "You write bank prose."
    assert cfg["response_mime_type"] == "application/json" and cfg["response_json_schema"] == SCHEMA
    assert cfg["temperature"] == 0.2 and cfg["max_output_tokens"] == 300
    assert "labels" not in cfg  # the Gemini API rejects request labels
    types.GenerateContentConfig(**cfg)  # the dict is a valid SDK config

    vertex = FakeClient(reply('{"text": "ok"}'))
    GeminiLLM(client=vertex, backend="vertex").complete(MESSAGES, json_schema=SCHEMA)
    assert vertex.models.calls[0]["config"]["labels"] == {"app": "wapsi"}


def test_plain_text_mode_has_no_json_config():
    client = FakeClient(reply("Plain prose."))
    assert GeminiLLM(client=client).complete([{"role": "user", "content": "hi"}]) == "Plain prose."
    assert "response_mime_type" not in client.models.calls[0]["config"]


def test_retries_on_429_and_5xx_then_succeeds():
    sleeps = []
    client = FakeClient(api_error(429), api_error(503), reply('{"text": "ok"}'))
    llm = GeminiLLM(client=client, sleep=sleeps.append, backoff_s=0.5)
    assert json.loads(llm.complete(MESSAGES, json_schema=SCHEMA)) == {"text": "ok"}
    assert sleeps == [0.5, 1.0] and len(client.models.calls) == 3


def test_non_retryable_error_raises_immediately():
    client = FakeClient(api_error(400), reply("never"))
    with pytest.raises(LLMError) as exc:
        GeminiLLM(client=client, sleep=lambda s: None).complete(MESSAGES)
    assert exc.value.status_code == 400 and len(client.models.calls) == 1


def test_gives_up_after_max_retries():
    client = FakeClient(*[api_error(500)] * 3)
    with pytest.raises(LLMError, match="giving up after 3"):
        GeminiLLM(client=client, max_retries=2, sleep=lambda s: None).complete(MESSAGES)


def test_invalid_json_is_retried_then_reported():
    client = FakeClient(reply("not json"), reply('{"text": "fixed"}'))
    assert json.loads(GeminiLLM(client=client, sleep=lambda s: None).complete(MESSAGES, json_schema=SCHEMA)) == {
        "text": "fixed"}
    bad = FakeClient(reply("nope"), reply("still nope"))
    with pytest.raises(LLMError, match="invalid JSON"):
        GeminiLLM(client=bad, max_retries=1, sleep=lambda s: None).complete(MESSAGES, json_schema=SCHEMA)


def test_blocked_or_empty_response_is_an_error():
    blocked = types.GenerateContentResponse(
        candidates=[], prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"))
    with pytest.raises(LLMError, match="SAFETY"):
        GeminiLLM(client=FakeClient(blocked)).complete(MESSAGES)
    with pytest.raises(LLMError, match="finish_reason"):
        GeminiLLM(client=FakeClient(reply(None, finish="MAX_TOKENS"))).complete(MESSAGES)


def test_helpers():
    assert parse_json_text('```\n{"a": 1}\n```') == {"a": 1}
    thought = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
        role="model", parts=[types.Part(text="thinking...", thought=True), types.Part(text="answer")]))])
    assert response_text(thought) == "answer"


class GeminiBackedByFake:
    """A fake google-genai client that answers like Gemini would, using FakeLLM's deterministic prose."""

    def __init__(self):
        self.fake = FakeLLM()
        self.models = self
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        messages = [{"role": "system", "content": config.get("system_instruction", "")}]
        for c in contents:
            messages.append({"role": "user" if c["role"] == "user" else "assistant",
                             "content": "\n".join(p["text"] for p in c["parts"])})
        schema = config.get("response_json_schema")
        return reply(self.fake.complete(messages, json_schema=schema))


def test_pipeline_drafts_verify_with_gemini_adapter():
    client = GeminiBackedByFake()
    llm = GeminiLLM(client=client)
    b = process_case(sc.showcase_ledger(), sc.SHOWCASE_1["ncrp_ids"], llm=llm)
    assert client.calls > 0
    assert all(r.passed for r in b.verifications.values())
    assert any(d.llm_model == llm.name for d in b.drafts)


class _RecordingClient:
    seen: list[dict] = []

    def __init__(self, **kwargs):
        _RecordingClient.seen.append(kwargs)


@pytest.fixture()
def recording_genai(monkeypatch):
    from google import genai

    _RecordingClient.seen = []
    monkeypatch.setattr(genai, "Client", _RecordingClient)
    return _RecordingClient.seen


def test_client_is_lazy_and_targets_vertex(recording_genai, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-must-not-be-used-by-vertex")
    llm = GeminiLLM("gemini-3.5-flash", project="wapsi-test", location="asia-south1", timeout_s=30, backend="vertex")
    assert not recording_genai and llm.name == "vertex:gemini-3.5-flash"
    assert isinstance(llm.client, _RecordingClient)
    seen = recording_genai[0]
    assert seen["vertexai"] is True and seen["project"] == "wapsi-test" and seen["location"] == "asia-south1"
    assert "api_key" not in seen and seen["http_options"].timeout == 30000


def test_backend_selection(monkeypatch):
    assert resolve_backend() == "aistudio" and GeminiLLM().backend == "aistudio"  # the free-trial default
    monkeypatch.setenv("WAPSI_GEMINI_BACKEND", "Vertex")
    assert resolve_backend() == "vertex" and GeminiLLM().backend == "vertex"
    assert GeminiLLM(backend="aistudio").backend == "aistudio"  # the constructor argument wins
    assert resolve_backend(env={}) == "aistudio"
    assert resolve_backend(env={"WAPSI_GEMINI_BACKEND": "vertex"}) == "vertex"
    with pytest.raises(ValueError, match="WAPSI_GEMINI_BACKEND"):
        GeminiLLM(backend="openai")
    monkeypatch.setenv("WAPSI_GEMINI_BACKEND", "bogus")
    with pytest.raises(ValueError):
        GeminiLLM()


def test_aistudio_client_uses_the_api_key_and_never_shows_it(recording_genai, monkeypatch):
    key = "AIzaSy-test-key-0123456789"
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")  # must not flip the backend
    monkeypatch.setenv("GEMINI_API_KEY", key)
    llm = GeminiLLM(project="wapsi-test", timeout_s=20)
    assert llm.client is llm.client and len(recording_genai) == 1
    seen = recording_genai[0]
    assert seen["api_key"] == key and seen["vertexai"] is False
    assert "project" not in seen and "location" not in seen and seen["http_options"].timeout == 20000
    assert key not in repr(llm) and key not in llm.name

    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-google-fallback-99")
    assert gemini_api_key() == "AIzaSy-google-fallback-99"
    assert GeminiLLM().client is not None
    assert recording_genai[-1]["api_key"] == "AIzaSy-google-fallback-99"
    assert gemini_api_key({"GEMINI_API_KEY": " a ", "GOOGLE_API_KEY": "b"}) == "a"
    assert gemini_api_key({"GEMINI_API_KEY": "  "}) is None


def test_missing_api_key_is_a_clear_error_without_network(recording_genai):
    llm = GeminiLLM(sleep=lambda s: pytest.fail("must not retry"))
    with pytest.raises(LLMError) as exc:
        llm.complete(MESSAGES)
    assert str(exc.value) == MISSING_KEY_MESSAGE
    assert "GEMINI_API_KEY" in MISSING_KEY_MESSAGE and "aistudio.google.com" in MISSING_KEY_MESSAGE
    assert not recording_genai  # no client was built, so nothing could reach the network


def test_free_tier_429_honours_retry_info_then_succeeds():
    sleeps = []
    client = FakeClient(free_tier_429(retry="7s"), reply('{"text": "ok"}'))
    llm = GeminiLLM(client=client, sleep=sleeps.append, backoff_s=0.5)
    assert json.loads(llm.complete(MESSAGES, json_schema=SCHEMA)) == {"text": "ok"}
    assert sleeps == [7.0]  # the server's RetryInfo, not the 0.5 s backoff
    capped = FakeClient(free_tier_429(retry="300s"), reply("fine"))
    sleeps.clear()
    GeminiLLM(client=capped, sleep=sleeps.append, max_retry_delay_s=45).complete(MESSAGES)
    assert sleeps == [45]


def test_free_tier_daily_quota_is_not_retried():
    client = FakeClient(free_tier_429(per_day=True), reply("never"))
    with pytest.raises(LLMError) as exc:
        GeminiLLM(client=client, sleep=lambda s: pytest.fail("must not retry")).complete(MESSAGES)
    msg = str(exc.value)
    assert exc.value.status_code == 429 and len(client.models.calls) == 1
    assert "daily quota" in msg and "midnight Pacific" in msg and "aistudio.google.com/rate-limit" in msg


def test_persistent_rate_limit_explains_the_free_tier():
    client = FakeClient(*[free_tier_429(retry=None)] * 3)
    with pytest.raises(LLMError) as exc:
        GeminiLLM(client=client, max_retries=2, sleep=lambda s: None).complete(MESSAGES)
    msg = str(exc.value)
    assert "free tier is rate limited" in msg and "giving up after 3" in msg and exc.value.status_code == 429


def test_quota_helpers():
    minute, day = free_tier_429(), free_tier_429(per_day=True, retry=None)
    assert retry_delay_s(minute) == 7.0 and retry_delay_s(day) is None and retry_delay_s(ValueError()) is None
    assert not is_daily_quota(minute) and is_daily_quota(day)
    assert is_daily_quota(api_error(429, "Quota exceeded for requests per day"))
    assert quota_hint(api_error(500)) is None and quota_hint(ValueError("x")) is None
    assert "gemini-3.8-flash" in quota_hint(minute, "gemini-3.8-flash")


def test_api_key_is_redacted_from_errors(monkeypatch):
    key = "AIzaSy-secret-in-error-42"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    client = FakeClient(api_error(400, f"API key not valid: {key}"))
    with pytest.raises(LLMError) as exc:
        GeminiLLM(client=client).complete(MESSAGES)
    assert key not in str(exc.value) and "***" in str(exc.value)


def test_gemma_private_llm_with_id_token():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "google/gemma-4-E4B-it"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"text": "ok"}'}}]})

    tokens = iter(["tok-1", "tok-2"])
    llm = gemma_private_llm("https://gemma-abc-el.a.run.app/", id_token_audience="https://gemma-abc-el.a.run.app",
                            id_token_fetch=lambda aud: next(tokens), transport=httpx.MockTransport(handler))
    assert isinstance(llm, PrivateGemmaLLM) and isinstance(llm, LLM)
    assert llm.url == "https://gemma-abc-el.a.run.app/v1/chat/completions"
    assert llm.name == "gemma-private:google/gemma-4-E4B-it"
    assert json.loads(llm.complete(MESSAGES, json_schema=SCHEMA)) == {"text": "ok"}
    assert requests[0].headers["authorization"] == "Bearer tok-1"
    assert requests[0].url.path == "/v1/chat/completions"
    llm._auth.ttl_s = -1  # force a refresh
    llm.complete(MESSAGES)
    assert requests[1].headers["authorization"] == "Bearer tok-2"
    assert json.loads(requests[0].content)["response_format"]["type"] == "json_schema"

    plain = gemma_private_llm("http://localhost:8000/v1", "google/gemma-4-26B-A4B-it")
    assert plain.url == "http://localhost:8000/v1/chat/completions" and plain._auth is None
    with pytest.raises(ValueError):
        gemma_private_llm("")
