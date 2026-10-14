"""A deterministic fake OpenAI-compatible backend, in process: the offline stand-in for the model endpoint.

``FakeOpenAIBackend`` answers ``POST /chat/completions`` and ``GET /models`` through an ``httpx`` transport, so
the agent's real client stack (Google ADK -> LiteLLM -> the OpenAI SDK -> httpx) runs unchanged and only the
server is replaced. No socket is opened and no key is needed: the offline demo, the CLI and the tests use it.

What it plays, per request:

- the **capability probe** (``probe``): a tool call, then the code from its result, when ``tools="native"`` (tool
  results are understood under ``result_role`` only: ``tool`` like an OpenAI gateway, or ``tool_responses``);
  the HTTP 400 a vLLM server without ``--enable-auto-tool-choice`` returns when ``tools="reject"``; a text answer
  when ``tools="ignore"``; leaked control tokens and repetition when ``tools="garbage"`` (a degraded deployment);
- the **officer agent with native tool calls**: OpenAI ``tool_calls`` planned by ``scripted.next_officer_action``
  (open_case -> trace_money -> hold_plan -> verification_report -> draft_documents for a new notice, ...), then a
  reply that quotes only values the tools returned;
- the **officer agent on the JSON tool protocol** (``json_protocol``): the same plan as ``{"tool": ...}`` /
  ``{"final": ...}`` objects. ``faults`` makes it break the protocol first (tests of validation and repair);
- anything else (the pipeline's draft prose): ``prose_llm.complete`` (``FakeLLM`` by default).

It is a scripted planner, not a language model: it understands the requests the demo script uses. Its token
counts are character-based stand-ins, labelled as an offline run wherever they are shown, and the model id it
serves by default is ``wapsi-offline-planner`` (tests name it ``Gemma-4-31B-IT`` to exercise what ADK does for
Gemma 4 model ids).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from . import json_protocol as jp
from .endpoint import AgentEndpoint
from .probe import PROBE_TOOL, PROBE_VALUE, RESULT_ROLES, ROLE_TOOL, ToolProbe, probe_tool_calling
from .scripted import case_from_results, next_officer_action

BASE_URL = "http://wapsi-fake-endpoint.offline/v1"
OFFLINE_MODEL = "wapsi-offline-planner"  # the id the fake serves by default: never a real model's name, so an
#                                          offline run can not be mistaken for one in a trace or an audit log
OFFLINE_KEY = "offline-fake-key"  # not a secret: the fake endpoint only checks that the client sent what it was given
TOOL_MODES = ("native", "reject", "ignore", "garbage")
GARBAGE = "<|tool_call>//1" + "/" * 120  # the shape a degraded shared deployment returned on 2026-10-14
FAULTS = ("bad_json", "unknown_tool", "bad_args", "two_calls", "extra_keys")
VLLM_TOOLS_ERROR = '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'

Result = tuple[str, dict[str, Any]]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def _json_obj(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _is_protocol_feedback(text: str) -> bool:
    return text.startswith(jp.RESULT_PREFIX) or text.startswith(jp.ERROR_PREFIX)


class FakeOpenAIBackend:
    def __init__(self, *, model: str = OFFLINE_MODEL, tools: str = "native", result_role: str = ROLE_TOOL,
                 strict_roles: bool = True, require_key: bool = True, models: tuple[str, ...] | None = None,
                 garbage_models: tuple[str, ...] = (),
                 json_object: bool = True, faults: list[str] | None = None, prose_llm: Any | None = None,
                 planner: Callable[[str, list[Result], str | None], tuple[str, dict[str, Any]] | str] | None = None,
                 ) -> None:
        if tools not in TOOL_MODES:
            raise ValueError(f"tools must be one of {', '.join(TOOL_MODES)}")
        unknown = [f for f in (faults or []) if f not in FAULTS]
        if unknown:
            raise ValueError(f"unknown fault(s): {', '.join(unknown)}")
        if result_role not in RESULT_ROLES:
            raise ValueError(f"result_role must be one of {', '.join(RESULT_ROLES)}")
        self.model = model  # the id clients are configured with
        self.models = tuple(models) if models is not None else (model,)  # the ids this server serves right now
        self.garbage_models = tuple(garbage_models)  # served, but answering with unusable output
        self.tools = tools
        self.result_role = result_role  # the only message role this server takes tool results under
        self.strict_roles = strict_roles  # True: HTTP 400 for the other role; False: the message is ignored
        self.require_key = require_key
        self.json_object = json_object  # False: answer 400 to response_format json_object
        self.faults = list(faults or [])
        self.prose_llm = prose_llm
        self.planner = planner or next_officer_action
        self.requests: list[dict[str, Any]] = []  # chat-completions bodies, in order (tests)
        self.paths: list[str] = []
        self._n = 0

    # ------------------------------------------------------------------ wiring
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=30.0)

    def async_client(self) -> Any:
        """An ``openai.AsyncOpenAI`` whose HTTP goes to this backend (what LiteLLM is given as ``client``)."""
        from openai import AsyncOpenAI

        return AsyncOpenAI(base_url=BASE_URL, api_key=OFFLINE_KEY, max_retries=0,
                           http_client=httpx.AsyncClient(transport=self.transport, timeout=30.0))

    def endpoint(self, **overrides: Any) -> AgentEndpoint:
        return AgentEndpoint(**{"base_url": BASE_URL, "model": self.model, "api_key": OFFLINE_KEY,
                                "label": "offline fake endpoint", "max_retries": 0, **overrides})

    def probe(self, model: str | None = None) -> ToolProbe:
        return probe_tool_calling(BASE_URL, model or self.model, OFFLINE_KEY, transport=self.transport)

    def build_model(self, **overrides: Any) -> Any:
        """An ``EndpointLlm`` (ADK ``LiteLlm``) wired to this backend, probe included."""
        from .endpoint import build_endpoint_model

        return build_endpoint_model(self.endpoint(**overrides), client=self.async_client(), prober=self.probe)

    # ------------------------------------------------------------------ HTTP
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(f"{request.method} {path}")
        if self.require_key and request.headers.get("authorization") != f"Bearer {OFFLINE_KEY}":
            return self._error(401, "invalid API key")
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "wapsi-fake-endpoint"} for m in self.models]})
        if request.method != "POST" or not path.endswith("/chat/completions"):
            return self._error(404, f"{request.method} {path} is not served")
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._error(400, "request body is not JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return self._error(400, "messages is required")
        self.requests.append(body)
        if body.get("model") not in self.models:  # the wording AMD Token Factory uses for a withdrawn model
            return self._error(400, f"Requested model {str(body.get('model'))[:60]} not supported")
        fmt = body.get("response_format") or {}
        if fmt.get("type") == "json_object" and not self.json_object:
            return self._error(400, "response_format json_object is not supported by this server")
        messages = [m for m in body["messages"] if isinstance(m, dict)]
        if self.tools == "garbage" or body.get("model") in self.garbage_models:
            return self._reply(body, text=GARBAGE, finish="length")
        other = [m for m in messages if m.get("role") in RESULT_ROLES and m.get("role") != self.result_role]
        if other and self.strict_roles:
            return self._error(400, f"unexpected message role {other[0].get('role')!r}")
        messages = [m for m in messages if m not in other]
        tools = body.get("tools") or []
        if tools:
            if self.tools == "reject":
                return self._error(400, VLLM_TOOLS_ERROR)
            names = [t.get("function", {}).get("name") for t in tools if isinstance(t, dict)]
            if self.tools == "ignore":
                return self._reply(body, text="I can only answer in text; I cannot call functions.")
            if PROBE_TOOL in names:
                done = [_json_obj(_content_text(m.get("content"))) for m in messages
                        if m.get("role") == self.result_role]
                if done:
                    return self._reply(body, text=f"The function returned the code {done[-1].get('code')}.")
                if any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages):
                    return self._reply(body, text="I called the function but I do not see its result.")
                return self._reply(body, call=(PROBE_TOOL, {"value": PROBE_VALUE}))
            return self._reply(body, **self._native(messages))
        system = _content_text(messages[0].get("content")) if messages and messages[0].get("role") == "system" else ""
        if jp.PROTOCOL_MARKER in system:
            return self._reply(body, text=self._json_protocol(messages))
        return self._reply(body, text=self._prose(body, messages))

    @staticmethod
    def _error(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error",
                                                      "code": status}})

    def _reply(self, body: dict[str, Any], *, text: str | None = None,
               call: tuple[str, dict[str, Any]] | None = None, finish: str = "stop") -> httpx.Response:
        self._n += 1
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if call is not None:
            name, args = call
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_fake_{self._n:04d}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            finish = "tool_calls"
        sent = sum(len(_content_text(m.get("content"))) for m in body["messages"] if isinstance(m, dict))
        out = len(text or "") if call is None else len(json.dumps(call[1]))
        usage = {"prompt_tokens": sent // 4 + 1, "completion_tokens": out // 4 + 1}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return httpx.Response(200, json={
            "id": f"chatcmpl-fake-{self._n:04d}", "object": "chat.completion", "created": 1_760_000_000 + self._n,
            "model": body.get("model"), "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage})

    # ------------------------------------------------------------------ the officer agent, native tool calls
    def _native(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        names: dict[str, str] = {}
        results: list[tuple[int, Result]] = []
        last_user = -1
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "user":
                last_user = i
            elif role == "assistant":
                for c in m.get("tool_calls") or []:
                    if isinstance(c, dict) and isinstance(c.get("function"), dict):
                        names[str(c.get("id"))] = str(c["function"].get("name"))
            elif role == self.result_role:
                name = names.get(str(m.get("tool_call_id")), str(m.get("name") or ""))
                results.append((i, (name, _json_obj(_content_text(m.get("content"))))))
        text = _content_text(messages[last_user].get("content")) if last_user >= 0 else ""
        return self._act(text, [r for i, r in results if i > last_user], [r for _, r in results])

    def _act(self, text: str, done: list[Result], every: list[Result]) -> dict[str, Any]:
        action = self.planner(text, done, case_from_results(every, text))
        return {"text": action} if isinstance(action, str) else {"call": action}

    # ------------------------------------------------------------------ the officer agent, JSON tool protocol
    def _json_protocol(self, messages: list[dict[str, Any]]) -> str:
        results: list[tuple[int, Result]] = []
        last_user = -1
        for i, m in enumerate(messages):
            text = _content_text(m.get("content"))
            if m.get("role") != "user":
                continue
            if text.startswith(jp.RESULT_PREFIX):
                head, _, rest = text[len(jp.RESULT_PREFIX):].partition(":")
                try:
                    value, _ = json.JSONDecoder().raw_decode(rest.strip())
                except ValueError:
                    value = {}
                results.append((i, (head.strip(), value if isinstance(value, dict) else {})))
            elif not _is_protocol_feedback(text):
                last_user = i
        text = _content_text(messages[last_user].get("content")) if last_user >= 0 else ""
        act = self._act(text, [r for i, r in results if i > last_user], [r for _, r in results])
        repairing = _content_text(messages[-1].get("content")).startswith(jp.ERROR_PREFIX)
        if self.faults and not repairing:
            return self._faulty(self.faults.pop(0), act)
        if "call" in act:
            name, args = act["call"]
            return json.dumps({"tool": name, "args": args}, ensure_ascii=False)
        return json.dumps({"final": act["text"]}, ensure_ascii=False)

    @staticmethod
    def _faulty(fault: str, act: dict[str, Any]) -> str:
        name, args = act.get("call") or ("list_cases", {})
        if fault == "bad_json":
            return '{"tool": "' + name + '", "args": {'
        if fault == "unknown_tool":
            return json.dumps({"tool": "release_all_holds", "args": {}})
        if fault == "bad_args":
            return json.dumps({"tool": name, "args": {**args, "amount_to_release": 100000}})
        if fault == "two_calls":
            return json.dumps([{"tool": name, "args": args}, {"tool": "approve_draft", "args": {}}])
        return json.dumps({"tool": name, "args": args, "then": "approve_draft"})

    # ------------------------------------------------------------------ everything else
    def _prose(self, body: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        if self.prose_llm is None:
            from wapsi.adapters.llm_fake import FakeLLM

            self.prose_llm = FakeLLM(name="fake-llm (offline endpoint)")
        schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
        plain = [{"role": str(m.get("role")), "content": _content_text(m.get("content"))} for m in messages]
        return self.prose_llm.complete(plain, json_schema=schema)
