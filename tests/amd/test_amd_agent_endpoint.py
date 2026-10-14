"""The agent's model layer: endpoint settings from the environment, ADK ``LiteLlm`` construction (no network),
the capability probe, the strict JSON tool protocol, and round trips against the fake OpenAI-compatible backend
(native tool calls and the JSON fallback). Offline: every request goes to an in-process ``httpx`` transport."""

from __future__ import annotations

import asyncio
import json
import warnings

import httpx
import pytest

pytest.importorskip("google.adk", reason="agent extra not installed (uv sync --extra agent)")
pytest.importorskip("litellm", reason="agent extra not installed (uv sync --extra agent)")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.tools.function_tool import FunctionTool  # noqa: E402
from google.genai import types  # noqa: E402

from editions.shared import json_protocol as jp  # noqa: E402
from editions.shared.endpoint import (  # noqa: E402
    DEFAULT_AGENT_MODEL,
    AgentEndpoint,
    EndpointStats,
    build_endpoint_model,
    provider_label,
)
from editions.shared.endpoint_model import EndpointLlm, EndpointOutputError, rejects_tools  # noqa: E402
from editions.shared.fake_openai import (  # noqa: E402
    BASE_URL,
    OFFLINE_KEY,
    VLLM_TOOLS_ERROR,
    FakeOpenAIBackend,
)
from editions.shared.probe import (  # noqa: E402
    JSON,
    NATIVE,
    PROBE_TOOL,
    ProbeCache,
    ToolProbe,
    looks_degenerate,
    probe_tool_calling,
)
from editions.shared.tools import OFFICER_TOOLS, open_case, search_sop  # noqa: E402
from wapsi.fixtures import showcase as sc  # noqa: E402

KEY = "rc-test-0123456789abcdef"  # shaped like a real key so leaks would be visible
GEMMA = "Gemma-4-31B-IT"


def gemma_endpoint(**kwargs) -> FakeOpenAIBackend:
    """The fake endpoint serving the id AMD Token Factory uses for Gemma 4: ADK's LiteLlm treats ``gemma-4``
    model ids specially (tool results as ``tool_responses``), and that is the behaviour under test here."""
    return FakeOpenAIBackend(**{"model": GEMMA, **kwargs})
TOKEN_FACTORY = "https://developer.amd.com.cn/radeon/api/v1"


# --------------------------------------------------------------------------- settings from the environment


def test_endpoint_is_none_without_a_base_url_and_reads_llm_variables():
    assert AgentEndpoint.from_env({}) is None
    e = AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY + "/", "WAPSI_LLM_API_KEY": KEY})
    assert e is not None and e.base_url == TOKEN_FACTORY and e.model == DEFAULT_AGENT_MODEL == "Gemma-4-31B-IT"
    assert e.litellm_model == "openai/Gemma-4-31B-IT" and e.api_key == KEY
    assert e.tool_mode == "auto" and e.tool_result_role == "auto" and e.parallel_tool_calls is False
    assert e.provider == "AMD Token Factory"

    custom = AgentEndpoint.from_env({
        "WAPSI_LLM_BASE_URL": "http://llm:8000/v1", "WAPSI_LLM_MODEL": "wapsi-llm", "WAPSI_LLM_API_KEY": "drafts",
        "WAPSI_AGENT_BASE_URL": "http://127.0.0.1:8000/v1", "WAPSI_AGENT_MODEL": "google/gemma-4-31B-it",
        "WAPSI_AGENT_API_KEY": KEY, "WAPSI_AGENT_TOOL_MODE": "JSON", "WAPSI_AGENT_MAX_TOKENS": "512",
        "WAPSI_AGENT_PARALLEL_TOOLS": "omit", "WAPSI_AGENT_MAX_TOOL_CALLS": "4", "WAPSI_AGENT_MIN_INTERVAL_S": "4",
        "WAPSI_AGENT_TOOL_RESULT_ROLE": "tool_responses", "WAPSI_AGENT_PROVIDER_LABEL": "vLLM-ROCm on MI300X"})
    assert (custom.base_url, custom.model, custom.api_key) == ("http://127.0.0.1:8000/v1", "google/gemma-4-31B-it", KEY)
    assert custom.tool_mode == "json" and custom.max_tokens == 512 and custom.parallel_tool_calls is None
    assert custom.max_tool_calls == 4 and custom.min_interval_s == 4.0 and custom.tool_result_role == "tool_responses"
    assert custom.provider == "vLLM-ROCm on MI300X"

    # the edition's defaults apply only when the environment names no endpoint
    amd = AgentEndpoint.from_env({}, base_url="http://llm:8000/v1", model="wapsi-llm")
    assert (amd.base_url, amd.model, amd.api_key) == ("http://llm:8000/v1", "wapsi-llm", None)
    with pytest.raises(ValueError, match="WAPSI_AGENT_TOOL_MODE"):
        AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY, "WAPSI_AGENT_TOOL_MODE": "maybe"})
    with pytest.raises(ValueError, match="WAPSI_AGENT_MAX_TOKENS"):
        AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY, "WAPSI_AGENT_MAX_TOKENS": "lots"})


def test_endpoint_never_shows_its_key():
    e = AgentEndpoint(base_url=TOKEN_FACTORY, api_key=KEY)
    assert KEY not in repr(e) and KEY not in json.dumps(e.describe())
    assert e.describe()["api_key_set"] is True and e.describe()["provider"] == "AMD Token Factory"
    assert e.scrub(f"401 for Bearer {KEY} at {TOKEN_FACTORY}") == f"401 for Bearer *** at {TOKEN_FACTORY}"
    assert "sk-abcdefgh12345678" not in e.scrub("another key sk-abcdefgh12345678 leaked")
    assert provider_label("http://llm:8000/v1").startswith("vLLM-ROCm")
    assert provider_label("http://127.0.0.1:8000/v1") == "local OpenAI-compatible server"
    assert provider_label("https://example.org/v1") == "OpenAI-compatible endpoint (example.org)"


def test_litellm_model_is_built_from_env_without_any_network_call():
    def never(model: str) -> ToolProbe:
        raise AssertionError("building the model must not probe the endpoint")

    endpoint = AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY, "WAPSI_LLM_API_KEY": KEY,
                                       "WAPSI_AGENT_MAX_TOKENS": "1024", "WAPSI_AGENT_TIMEOUT_S": "45"})
    model = build_endpoint_model(endpoint, prober=never)
    assert isinstance(model, LiteLlm) and isinstance(model, EndpointLlm)  # ADK pairs tool calls by id for LiteLlm
    assert model.model == "openai/Gemma-4-31B-IT"
    args = model._additional_args
    assert args["api_base"] == TOKEN_FACTORY and args["api_key"] == KEY
    assert args["max_tokens"] == 1024 and args["timeout"] == 45.0 and args["max_retries"] == 2
    assert "client" not in args  # a live endpoint: LiteLLM builds its own HTTP client on first use
    assert KEY not in repr(model) and KEY not in model.model_dump_json() and KEY not in json.dumps(model.describe())
    assert model.tool_mode is None and model.describe()["tool_mode"] == "auto (not probed yet)"
    assert model.stats.as_dict()["calls"] == 0

    keyless = build_endpoint_model(AgentEndpoint(base_url="http://127.0.0.1:8000/v1", model="wapsi-llm"))
    assert keyless.model == "openai/wapsi-llm" and keyless._additional_args["api_key"] == "EMPTY"


def test_explicit_tool_mode_skips_the_probe():
    def never(model: str) -> ToolProbe:
        raise AssertionError("an explicit tool mode must not probe")

    native = build_endpoint_model(AgentEndpoint(base_url=TOKEN_FACTORY, tool_mode="native"), prober=never)
    assert native.resolve_mode() == NATIVE and native.probe.reason == "set by WAPSI_AGENT_TOOL_MODE"
    assert native.describe()["tool_result_role"] == "tool"
    forced = build_endpoint_model(AgentEndpoint(base_url=TOKEN_FACTORY, tool_mode="json"), prober=never)
    assert forced.resolve_mode() == JSON


def test_endpoint_stats_sum_tokens_latency_and_reported_cost():
    stats = EndpointStats()
    stats.record(1800.0, {"prompt_tokens": 900, "completion_tokens": 30, "cost": 3.36e-05})
    stats.record(1200.0, {"prompt_tokens": 1100, "completion_tokens": 20, "cost": 3.99e-05})
    stats.record(900.0, None)
    stats.error()
    d = stats.as_dict()
    assert (d["calls"], d["errors"], d["prompt_tokens"], d["completion_tokens"]) == (3, 1, 2000, 50)
    assert d["cost_usd"] == pytest.approx(7.35e-05) and d["latency_ms_mean"] == 1300.0 and d["latency_ms_max"] == 1800.0
    assert EndpointStats().as_dict()["cost_usd"] is None  # nothing reported: no number is made up


# --------------------------------------------------------------------------- capability probe


def test_probe_native_checks_the_tool_call_and_the_tool_result():
    fake = gemma_endpoint()
    probe = fake.probe()
    assert (probe.mode, probe.ok, probe.result_role, probe.requests) == (NATIVE, True, "tool", 2)
    assert probe.model == "Gemma-4-31B-IT" and probe.status_code == 200 and probe.latency_ms is not None
    first, second = fake.requests
    assert first["tools"][0]["function"]["name"] == PROBE_TOOL and first["tool_choice"] == "auto"
    assert first["max_tokens"] <= 512  # a probe is cheap (room for a model that thinks before it calls)
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]


@pytest.mark.parametrize("strict", [True, False])
def test_probe_finds_the_tool_result_role_the_endpoint_understands(strict):
    """ADK's LiteLlm sends ``tool_responses`` for gemma-4 model ids; a gateway may only take ``tool`` (or the
    reverse). The probe tries both and reports the one whose result the model used."""
    fake = gemma_endpoint(result_role="tool_responses", strict_roles=strict)
    probe = fake.probe()
    assert (probe.mode, probe.ok, probe.result_role, probe.requests) == (NATIVE, True, "tool_responses", 3)
    assert "tool_responses" in probe.reason


def test_probe_falls_back_to_json_when_tools_are_rejected_or_ignored():
    rejected = gemma_endpoint(tools="reject").probe()
    assert (rejected.mode, rejected.ok, rejected.status_code, rejected.requests) == (JSON, True, 400, 1)
    assert "--enable-auto-tool-choice" in rejected.reason  # the server's own words reach the operator
    ignored = gemma_endpoint(tools="ignore").probe()
    assert (ignored.mode, ignored.ok) == (JSON, True) and "answered in text" in ignored.reason

    class Deaf(FakeOpenAIBackend):  # returns the tool call but never uses a tool result under any role
        def handler(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if any(m.get("role") in ("tool", "tool_responses") for m in body["messages"]):
                self.requests.append(body)
                return self._reply(body, text="I do not know the code.")
            return super().handler(request)

    deaf = Deaf(model=GEMMA).probe()
    assert (deaf.mode, deaf.ok, deaf.requests) == (JSON, True, 3) and "did not use its result" in deaf.reason


def test_probe_does_not_decide_when_the_endpoint_cannot_be_assessed():
    garbage = gemma_endpoint(tools="garbage").probe()
    assert garbage.ok is False and "unusable output" in garbage.reason and "<|tool_call>" in garbage.reason

    wrong_key = probe_tool_calling(BASE_URL, "Gemma-4-31B-IT", "wrong", transport=gemma_endpoint().transport)
    assert (wrong_key.ok, wrong_key.status_code, wrong_key.mode) == (False, 401, NATIVE)
    assert "WAPSI_LLM_API_KEY" in wrong_key.reason and "wrong" not in wrong_key.reason

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    unreachable = probe_tool_calling(BASE_URL, "m", KEY, transport=httpx.MockTransport(down), default=JSON)
    assert (unreachable.ok, unreachable.mode) == (False, JSON) and "ConnectError" in unreachable.reason

    def limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "60"},
                              json={"detail": {"error": {"message": "rate limit exceeded: 20 requests per minute"}}})

    busy = probe_tool_calling(BASE_URL, "m", KEY, transport=httpx.MockTransport(limited))
    assert (busy.ok, busy.status_code) == (False, 429) and "20 requests per minute" in busy.reason
    assert KEY not in json.dumps([p.as_dict() for p in (garbage, wrong_key, unreachable, busy)])


def test_probe_cache_keeps_successes_only_and_expires(tmp_path):
    cache = ProbeCache(tmp_path / "state" / "agent_tool_probe.json")
    assert cache.get(BASE_URL, "Gemma-4-31B-IT") is None
    cache.put(gemma_endpoint(tools="garbage").probe())  # not ok: never cached
    assert not cache.path.exists()
    probe = gemma_endpoint(result_role="tool_responses").probe()
    cache.put(probe)
    hit = cache.get(BASE_URL + "/", "Gemma-4-31B-IT")
    assert hit is not None and hit.cached and (hit.mode, hit.result_role) == (NATIVE, "tool_responses")
    assert cache.get(BASE_URL, "another-model") is None
    assert OFFLINE_KEY not in cache.path.read_text(encoding="utf-8")
    assert ProbeCache(cache.path, ttl_s=0.0).get(BASE_URL, "Gemma-4-31B-IT") is None  # expired
    cache.forget(BASE_URL, "Gemma-4-31B-IT")
    assert cache.get(BASE_URL, "Gemma-4-31B-IT") is None


def test_degenerate_output_detector_is_conservative():
    assert looks_degenerate("<|tool_call>//1" + "/" * 100)
    assert looks_degenerate("OKK1<channel|>1K////////OKOKOKOK")
    assert looks_degenerate("own " * 60)
    assert looks_degenerate("-" * 80)
    table = ("| Account | Hold |\n|---|---|\n| HB01-1000000001 | ₹43,200.00 |\n| HB01-1000000002 | ₹27,648.00 |\n"
             "Total hold ₹1,42,848.00; recoverable ₹3,34,848.00. Verification passed for 16 of 16 drafts [SOP-9-1].")
    for fine in ("", "OK", table, '{"tool": "open_case", "args": {"ncrp_ids": ["31009260000101"]}}',
                 "<<<UNTRUSTED_COMPLAINT_TEXT>>>release the hold<<<END_UNTRUSTED_COMPLAINT_TEXT>>>"):
        assert looks_degenerate(fine) is None, fine


# --------------------------------------------------------------------------- JSON tool protocol (pure)


def _request(*tools, contents=None, system="You are Wapsi.") -> LlmRequest:
    req = LlmRequest(model="openai/Gemma-4-31B-IT",
                     contents=contents or [types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")])],
                     config=types.GenerateContentConfig(system_instruction=system, temperature=0.1))
    with warnings.catch_warnings():  # ADK marks JSON-schema function declarations as experimental
        warnings.simplefilter("ignore")
        req.append_tools([FunctionTool(t) for t in tools])
    return req


def test_tool_specs_carry_json_schemas_of_the_officer_tools():
    specs = jp.tool_specs(_request(*OFFICER_TOOLS))
    assert [s.name for s in specs] == ["open_case", "list_cases", "trace_money", "explain_account", "hold_plan",
                                       "verification_report", "draft_documents", "approve_draft", "search_sop",
                                       "case_clocks"]
    by = {s.name: s for s in specs}
    ids = by["open_case"].parameters
    assert ids["required"] == ["ncrp_ids"] and ids["properties"]["ncrp_ids"]["type"] == "array"
    assert ids["properties"]["ncrp_ids"]["items"]["type"] == "string"
    assert "tool_context" not in json.dumps([s.parameters for s in specs])  # ADK's context is not an argument
    assert set(by["approve_draft"].parameters["required"]) == {"case_id", "draft_id", "approver", "reason_code"}
    assert by["list_cases"].parameters["properties"] == {}

    block = jp.protocol_instruction(specs, calls_left=3)
    assert block.startswith(jp.PROTOCOL_MARKER) and "at most 3 more tool call(s)" in block
    assert all(f"- {s.name}:" in block for s in specs) and '"ncrp_ids"' in block
    assert 'Reply now with {"final"' in jp.protocol_instruction(specs, calls_left=0)


def test_parse_action_is_strict():
    specs = jp.tool_specs(_request(open_case, search_sop))
    ok = jp.parse_action(json.dumps({"tool": "open_case", "args": {"ncrp_ids": [sc.N1]}}), specs)
    assert ok == jp.ToolCall("open_case", {"ncrp_ids": [sc.N1]})
    fenced = jp.parse_action('```json\n{"final": "Done. See [SOP-9-1]."}\n```', specs)
    assert fenced == jp.Final("Done. See [SOP-9-1].")
    chatty = jp.parse_action('Sure! {"tool": "search_sop", "args": {"query": "forward hold"}} hope that helps', specs)
    assert chatty == jp.ToolCall("search_sop", {"query": "forward hold"})

    def err(text: str) -> str:
        out = jp.parse_action(text, specs)
        assert isinstance(out, jp.ProtocolError), out
        return out.message

    assert "empty" in err("")
    assert "not a JSON object" in err("The total hold is ₹1,42,848.00.")  # prose is never taken as the reply
    assert "not a JSON object" in err('{"tool": "open_case", "args": {')
    assert "one tool call per reply" in err('[{"tool": "open_case", "args": {}}, {"tool": "search_sop", "args": {}}]')
    assert "unknown tool 'release_all_holds'" in err('{"tool": "release_all_holds", "args": {}}')
    assert "ncrp_ids is required" in err('{"tool": "open_case", "args": {}}')
    assert "must be array" in err('{"tool": "open_case", "args": {"ncrp_ids": "31009260000101"}}')
    assert "[0] must be string" in err('{"tool": "open_case", "args": {"ncrp_ids": [31009260000101]}}')
    assert "amount is not an argument" in err(json.dumps({"tool": "open_case",
                                                          "args": {"ncrp_ids": [sc.N1], "amount": 5}}))
    assert "unexpected keys: then" in err(json.dumps({"tool": "search_sop", "args": {"query": "x"}, "then": "y"}))
    assert "not both" in err('{"tool": "search_sop", "args": {"query": "x"}, "final": "done"}')
    assert "non-empty string" in err('{"final": ""}')
    assert 'needs a "tool" key' in err('{"answer": 42}')
    assert "args must be a JSON object" in err('{"tool": "search_sop", "args": "forward hold"}')


def test_validate_handles_types_enums_and_optionals():
    schema = {"type": "object", "required": ["n"], "properties": {
        "n": {"type": "integer"}, "f": {"type": "number"}, "b": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["a", "b"]}, "opt": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert jp.validate({"n": 1, "f": 1.5, "b": True, "mode": "a", "opt": None}, schema) == []
    assert jp.validate({"n": True}, schema) == ["args.n must be integer, got bool"]
    assert jp.validate({"n": 1, "mode": "c"}, schema) == ["args.mode must be one of ['a', 'b']"]
    assert jp.validate({"n": 1, "opt": 3}, schema) == ["args.opt does not match any allowed form"]
    assert jp.validate({"n": 1.5}, schema) == ["args.n must be integer, got float"]


def test_history_is_replayed_as_plain_text_and_calls_are_counted():
    contents = [
        types.Content(role="user", parts=[types.Part(text="list cases")]),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="list_cases", args={}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            name="list_cases", response={"status": "ok", "cases": []}))]),
        types.Content(role="model", parts=[types.Part(text="No cases yet.")]),
        types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")]),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            name="open_case", args={"ncrp_ids": [sc.N1]}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            name="open_case", response={"status": "ok", "case_id": f"CASE-{sc.N1}"}))]),
    ]
    assert jp.count_tool_results(contents) == 1  # only the calls spent on the officer's latest message
    plain = jp.rewrite_contents(contents)
    assert [c.role for c in plain] == ["user", "model", "user", "model", "user", "model", "user"]
    assert all(p.function_call is None and p.function_response is None for c in plain for p in c.parts)
    assert json.loads(plain[5].parts[0].text) == {"tool": "open_case", "args": {"ncrp_ids": [sc.N1]}}
    last = plain[6].parts[0].text
    assert last.startswith(f"{jp.RESULT_PREFIX} open_case: ") and f"CASE-{sc.N1}" in last


# --------------------------------------------------------------------------- round trips against the fake server


def _generate(model: EndpointLlm, request: LlmRequest) -> types.Content:
    async def run():
        out = None
        async for r in model.generate_content_async(request):
            out = r
        return out

    response = asyncio.run(run())
    assert response is not None and response.content is not None
    return response


def _tool_result(name: str, response: dict, call_id: str = "call_fake_0003") -> list[types.Content]:
    return [
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            id=call_id, name=name, args={"ncrp_ids": [sc.N1]}))]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id=call_id, name=name, response=response))]),
    ]


def test_native_tool_call_round_trip_through_litellm():
    fake = gemma_endpoint()
    model = fake.build_model()
    response = _generate(model, _request(*OFFICER_TOOLS))
    call = response.content.parts[0].function_call
    assert (call.name, dict(call.args)) == ("open_case", {"ncrp_ids": [sc.N1]}) and call.id.startswith("call_fake_")
    assert response.usage_metadata.prompt_token_count > 0
    assert model.tool_mode == NATIVE and model.probe.requests == 2

    sent = fake.requests[-1]  # what LiteLLM put on the wire
    assert sent["model"] == "Gemma-4-31B-IT" and sent["messages"][0] == {"role": "system", "content": "You are Wapsi."}
    assert [t["function"]["name"] for t in sent["tools"]][:2] == ["open_case", "list_cases"]
    assert sent["parallel_tool_calls"] is False and sent["max_tokens"] == 2048 and sent["temperature"] == 0.1
    assert "max_completion_tokens" not in sent and "response_format" not in sent

    # the tool result goes back under the role the endpoint understands (OpenAI's "tool"), then the next call
    opened = {"status": "ok", "case_id": f"CASE-{sc.N1}"}
    follow = _request(*OFFICER_TOOLS, contents=[
        types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")]),
        *_tool_result("open_case", opened, call.id)])
    nxt = _generate(model, follow).content.parts[0].function_call
    assert (nxt.name, dict(nxt.args)) == ("trace_money", {"case_id": f"CASE-{sc.N1}"})
    wire = fake.requests[-1]["messages"]
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool"]
    assert wire[2]["tool_calls"][0]["id"] == wire[3]["tool_call_id"] == call.id
    assert json.loads(wire[3]["content"]) == opened
    stats = model.stats.as_dict()
    assert stats["calls"] == 2 and stats["errors"] == 0 and stats["prompt_tokens"] > 0 and stats["cost_usd"] is None
    assert all(p == "POST /v1/chat/completions" for p in fake.paths)


def test_tool_results_use_tool_responses_when_that_is_what_the_endpoint_understands():
    fake = gemma_endpoint(result_role="tool_responses")
    model = fake.build_model()
    follow = _request(*OFFICER_TOOLS, contents=[
        types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")]),
        *_tool_result("open_case", {"status": "ok", "case_id": f"CASE-{sc.N1}"})])
    assert _generate(model, follow).content.parts[0].function_call.name == "trace_money"
    assert fake.requests[-1]["messages"][-1]["role"] == "tool_responses"
    assert model.describe()["tool_result_role"] == "tool_responses"

    pinned = fake.build_model(tool_mode="native", tool_result_role="tool_responses")  # no probe, role from env
    assert _generate(pinned, follow).content.parts[0].function_call.name == "trace_money"
    assert fake.requests[-1]["messages"][-1]["role"] == "tool_responses"


def test_json_protocol_fallback_round_trip():
    fake = gemma_endpoint(tools="reject")
    model = fake.build_model()
    response = _generate(model, _request(*OFFICER_TOOLS))
    call = response.content.parts[0].function_call
    assert (call.name, dict(call.args)) == ("open_case", {"ncrp_ids": [sc.N1]})
    assert model.tool_mode == JSON and VLLM_TOOLS_ERROR[:20] in model.probe.reason

    sent = fake.requests[-1]
    assert "tools" not in sent and "tool_choice" not in sent and "parallel_tool_calls" not in sent
    assert sent["response_format"] == {"type": "json_object"}
    system = sent["messages"][0]["content"]
    assert system.startswith("You are Wapsi.") and jp.PROTOCOL_MARKER in system and "- open_case:" in system

    follow = _request(*OFFICER_TOOLS, contents=[
        types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")]),
        *_tool_result("open_case", {"status": "ok", "case_id": f"CASE-{sc.N1}"})])
    nxt = _generate(model, follow).content.parts[0].function_call
    assert (nxt.name, dict(nxt.args)) == ("trace_money", {"case_id": f"CASE-{sc.N1}"})
    wire = fake.requests[-1]["messages"]
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert json.loads(wire[2]["content"])["tool"] == "open_case" and wire[3]["content"].startswith("TOOL_RESULT open_case")


@pytest.mark.parametrize("fault", ["bad_json", "unknown_tool", "bad_args", "two_calls", "extra_keys"])
def test_json_protocol_repairs_one_broken_answer(fault):
    fake = gemma_endpoint(tools="reject", faults=[fault])
    model = fake.build_model()
    call = _generate(model, _request(*OFFICER_TOOLS)).content.parts[0].function_call
    assert (call.name, dict(call.args)) == ("open_case", {"ncrp_ids": [sc.N1]})  # nothing invalid reached a tool
    repair = fake.requests[-1]["messages"]
    assert repair[-1]["role"] == "user" and repair[-1]["content"].startswith(jp.ERROR_PREFIX)
    assert repair[-2]["role"] == "assistant"  # the model sees its own broken answer
    assert len([r for r in fake.requests if "tools" not in r]) == 2  # one answer + one repair, no more


def test_json_protocol_gives_up_with_a_notice_after_the_repair():
    fake = gemma_endpoint(tools="reject", planner=lambda text, done, case: ("release_all_holds", {}))
    model = fake.build_model()
    part = _generate(model, _request(*OFFICER_TOOLS)).content.parts[0]
    assert part.function_call is None and part.text.startswith("[Wapsi] The model did not produce a valid tool call")
    assert "release_all_holds" in part.text
    assert len([r for r in fake.requests if "tools" not in r]) == 2


def test_json_protocol_enforces_the_call_limit_and_survives_without_json_object():
    fake = gemma_endpoint(tools="reject", json_object=False)
    model = fake.build_model(max_tool_calls=1)
    first = _generate(model, _request(*OFFICER_TOOLS))
    assert first.content.parts[0].function_call.name == "open_case"
    with_format = [r for r in fake.requests if "response_format" in r]
    assert len(with_format) == 1 and "response_format" not in fake.requests[-1]  # rejected once, then dropped

    spent = _request(*OFFICER_TOOLS, contents=[
        types.Content(role="user", parts=[types.Part(text=f"Open case {sc.N1}")]),
        *_tool_result("open_case", {"status": "ok", "case_id": f"CASE-{sc.N1}"})])
    part = _generate(model, spent).content.parts[0]
    assert part.function_call is None and "stopped after 1 tool calls" in part.text
    assert "used every tool call" in fake.requests[-1]["messages"][0]["content"]
    assert all("response_format" not in r for r in fake.requests[len(with_format) + 1:])


def test_native_mode_switches_to_json_when_the_endpoint_starts_rejecting_tools():
    fake = gemma_endpoint(tools="reject")
    stale = ToolProbe(mode=NATIVE, ok=True, reason="cached yesterday", base_url=BASE_URL, model=fake.model)
    model = build_endpoint_model(fake.endpoint(), client=fake.async_client(), prober=lambda model: stale)
    call = _generate(model, _request(*OFFICER_TOOLS)).content.parts[0].function_call
    assert call.name == "open_case" and model.tool_mode == JSON
    assert "rejected a tools request at run time" in model.probe.reason
    assert "tools" in fake.requests[0] and "tools" not in fake.requests[1]

    pinned = build_endpoint_model(fake.endpoint(tool_mode="native"), client=fake.async_client())
    with pytest.raises(Exception) as caught:  # an explicit native mode is not second-guessed
        _generate(pinned, _request(*OFFICER_TOOLS))
    assert rejects_tools(caught.value) and OFFLINE_KEY not in str(caught.value)


def test_unusable_model_output_is_refused_not_passed_on():
    fake = gemma_endpoint(tools="garbage")
    model = fake.build_model()
    with pytest.raises(EndpointOutputError, match="unusable output"):
        _generate(model, _request(*OFFICER_TOOLS))
    assert model.tool_mode is None and model.probe.ok is False  # nothing latched: probed again later


def test_fake_endpoint_checks_the_key_the_model_and_the_path():
    fake = gemma_endpoint()
    with fake.http_client() as http:
        auth = {"Authorization": f"Bearer {OFFLINE_KEY}"}
        assert http.get(f"{BASE_URL}/models").status_code == 401
        models = http.get(f"{BASE_URL}/models", headers=auth).json()
        assert [m["id"] for m in models["data"]] == ["Gemma-4-31B-IT"]
        body = {"model": "other", "messages": [{"role": "user", "content": "hi"}]}
        gone = http.post(f"{BASE_URL}/chat/completions", json=body, headers=auth)
        assert gone.status_code == 400 and gone.json()["error"]["message"] == "Requested model other not supported"
        assert http.post(f"{BASE_URL}/embeddings", json={}, headers=auth).status_code == 404
        body = {"model": "Gemma-4-31B-IT", "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
        prose = http.post(f"{BASE_URL}/chat/completions", json=body, headers=auth).json()
        assert prose["choices"][0]["finish_reason"] == "stop" and prose["usage"]["total_tokens"] > 0
    with pytest.raises(ValueError):
        gemma_endpoint(tools="sometimes")
    # on its own the fake never borrows a real model's name: an offline run is labelled as one
    offline = FakeOpenAIBackend()
    assert offline.model == "wapsi-offline-planner" and offline.endpoint().provider == "offline fake endpoint"
    assert offline.build_model().model == "openai/wapsi-offline-planner"


# --------------------------------------------------------------------------- a catalog that changes under the agent


def test_probe_reports_a_model_the_endpoint_does_not_serve():
    """Seen on AMD Token Factory on 2026-10-14: HTTP 400 ``Requested model Gemma-4-31B-IT not supported``. That is
    not a tools rejection: the probe must not latch the JSON protocol, and it lists what is served instead."""
    fake = gemma_endpoint(models=("Qwen3.8-27B", "DeepSeek-V4-Flash"))
    probe = fake.probe()
    assert (probe.ok, probe.model_served, probe.status_code, probe.mode) == (False, False, 400, NATIVE)
    assert "does not serve model 'Gemma-4-31B-IT'" in probe.reason
    assert "served now: Qwen3.8-27B, DeepSeek-V4-Flash" in probe.reason
    assert fake.paths == ["POST /v1/chat/completions", "GET /v1/models"]
    served = fake.probe("Qwen3.8-27B")
    assert (served.ok, served.model_served, served.mode, served.model) == (True, True, NATIVE, "Qwen3.8-27B")


def test_model_falls_back_while_the_configured_model_is_not_served_and_says_so():
    fake = gemma_endpoint(models=("Qwen3.8-27B",))  # Gemma withdrawn from the shared catalog
    plain = fake.build_model()
    with pytest.raises(Exception) as caught:  # no fallback configured: the error reaches the caller
        _generate(plain, _request(*OFFICER_TOOLS))
    assert "not supported" in str(caught.value) and plain.tool_mode is None and plain.active_model == "Gemma-4-31B-IT"

    model = fake.build_model(fallback_models=("MiMo-V2.6-Flash", "Qwen3.8-27B"))
    call = _generate(model, _request(*OFFICER_TOOLS)).content.parts[0].function_call
    assert call.name == "open_case" and fake.requests[-1]["model"] == "Qwen3.8-27B"
    d = model.describe()
    assert (d["model"], d["configured_model"], d["tool_mode"]) == ("Qwen3.8-27B", "Gemma-4-31B-IT", NATIVE)
    assert d["fallback"]["using"] == "Qwen3.8-27B" and "does not serve model" in d["fallback"]["reason"]
    assert model.model == "openai/Qwen3.8-27B" and model.probe.model == "Qwen3.8-27B"

    # the configured model comes back: after the re-check interval the agent returns to it
    fake.models = ("Gemma-4-31B-IT", "Qwen3.8-27B")
    _generate(model, _request(*OFFICER_TOOLS))
    assert fake.requests[-1]["model"] == "Qwen3.8-27B"  # not re-probed on every call
    model._fallback_at -= 3600
    _generate(model, _request(*OFFICER_TOOLS))
    assert fake.requests[-1]["model"] == "Gemma-4-31B-IT" and model.describe()["fallback"] is None
    assert model.active_model == "Gemma-4-31B-IT"


def test_model_falls_back_when_the_configured_model_returns_unusable_output(tmp_path):
    fake = gemma_endpoint(models=("Gemma-4-31B-IT", "Qwen3.8-27B"), garbage_models=("Gemma-4-31B-IT",))
    cache = ProbeCache(tmp_path / "probe.json")
    model = build_endpoint_model(fake.endpoint(fallback_models=("Qwen3.8-27B",)), client=fake.async_client(),
                                 prober=fake.probe, probe_cache=cache)
    assert _generate(model, _request(*OFFICER_TOOLS)).content.parts[0].function_call.name == "open_case"
    assert model.active_model == "Qwen3.8-27B" and "unusable output" in model.describe()["fallback"]["reason"]
    assert cache.get(BASE_URL, "Qwen3.8-27B") is not None and cache.get(BASE_URL, "Gemma-4-31B-IT") is None

    # a model that degrades after it passed the probe is dropped at once and the fallback takes the next call
    healthy = gemma_endpoint(models=("Gemma-4-31B-IT", "Qwen3.8-27B"))
    model = healthy.build_model(fallback_models=("Qwen3.8-27B",))
    _generate(model, _request(*OFFICER_TOOLS))
    assert model.active_model == "Gemma-4-31B-IT"
    healthy.garbage_models = ("Gemma-4-31B-IT",)
    with pytest.raises(EndpointOutputError):
        _generate(model, _request(*OFFICER_TOOLS))
    assert _generate(model, _request(*OFFICER_TOOLS)).content.parts[0].function_call.name == "open_case"
    assert model.active_model == "Qwen3.8-27B"

    # ... and so is a model the catalog withdraws mid-session
    healthy.garbage_models = ()
    model = healthy.build_model(fallback_models=("Qwen3.8-27B",))
    _generate(model, _request(*OFFICER_TOOLS))
    assert model.active_model == "Gemma-4-31B-IT"
    healthy.models = ("Qwen3.8-27B",)
    with pytest.raises(Exception, match="not supported"):
        _generate(model, _request(*OFFICER_TOOLS))
    _generate(model, _request(*OFFICER_TOOLS))
    assert healthy.requests[-1]["model"] == "Qwen3.8-27B" and model.describe()["fallback"]["using"] == "Qwen3.8-27B"


def test_extra_body_and_fallbacks_come_from_the_environment():
    e = AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY,
                                "WAPSI_AGENT_FALLBACK_MODELS": " Qwen3.8-27B , DeepSeek-V4-Flash ,",
                                "WAPSI_AGENT_EXTRA_BODY": '{"reasoning_effort": "low"}'})
    assert e.fallback_models == ("Qwen3.8-27B", "DeepSeek-V4-Flash") and e.extra_body == {"reasoning_effort": "low"}
    assert e.describe()["fallback_models"] == ["Qwen3.8-27B", "DeepSeek-V4-Flash"]
    for bad in ("[1]", "{not json", '{"model": "x"}', '{"messages": []}'):
        with pytest.raises(ValueError, match="WAPSI_AGENT_EXTRA_BODY"):
            AgentEndpoint.from_env({"WAPSI_LLM_BASE_URL": TOKEN_FACTORY, "WAPSI_AGENT_EXTRA_BODY": bad})

    fake = gemma_endpoint()
    model = fake.build_model(extra_body={"reasoning_effort": "low"})
    _generate(model, _request(*OFFICER_TOOLS))
    assert fake.requests[-1]["reasoning_effort"] == "low" and "tools" in fake.requests[-1]
    json_fake = gemma_endpoint(tools="reject")
    _generate(json_fake.build_model(extra_body={"reasoning_effort": "low"}), _request(*OFFICER_TOOLS))
    sent = json_fake.requests[-1]
    assert sent["reasoning_effort"] == "low" and sent["response_format"] == {"type": "json_object"}
