from __future__ import annotations

import json

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from wapsi.adapters.gemini_llm import (
    GeminiLLM,
    PrivateGemmaLLM,
    gemma_private_llm,
    parse_json_text,
    response_text,
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


def api_error(code: int) -> genai_errors.APIError:
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "boom", "status": "X"}})


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
    assert isinstance(llm, LLM) and llm.name == "vertex:gemini-3.5-flash"
    out = llm.complete(MESSAGES, temperature=0.2, max_tokens=300, json_schema=SCHEMA)
    assert json.loads(out) == {"text": "Hold ⟦hold_amount⟧."}
    call = client.models.calls[0]
    assert call["model"] == "gemini-3.5-flash"
    assert [c["role"] for c in call["contents"]] == ["user", "model", "user"]
    assert [p["text"] for p in call["contents"][2]["parts"]] == ["second", "third"]
    cfg = call["config"]
    assert cfg["system_instruction"] == "You write bank prose."
    assert cfg["response_mime_type"] == "application/json" and cfg["response_json_schema"] == SCHEMA
    assert cfg["temperature"] == 0.2 and cfg["max_output_tokens"] == 300
    types.GenerateContentConfig(**cfg)  # the dict is a valid SDK config


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


def test_client_is_lazy_and_targets_vertex(monkeypatch):
    from google import genai

    seen = {}

    class _Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(genai, "Client", _Client)
    llm = GeminiLLM("gemini-3.5-flash", project="wapsi-test", location="asia-south1", timeout_s=30)
    assert not seen
    assert isinstance(llm.client, _Client)
    assert seen["vertexai"] is True and seen["project"] == "wapsi-test" and seen["location"] == "asia-south1"
    assert seen["http_options"].timeout == 30000


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
