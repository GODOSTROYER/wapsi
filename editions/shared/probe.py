"""Capability probe: can this OpenAI-compatible endpoint drive a tool-using agent with native tool calls?

Two small requests with synthetic text (three when the first tool-result role is not understood):

1. ``tools`` with one function and ``tool_choice: "auto"``: "call it with value 7, then tell me the code".
   - ``choices[0].message.tool_calls`` names the function -> go on to step 2;
   - HTTP 200 without a tool call, or a 4xx that rejects ``tools`` / ``tool_choice`` (vLLM without the tool flags
     answers 400 ``"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set``)
     -> ``json``: the agent uses the strict JSON tool protocol (``json_protocol``), which needs plain chat
     completions only;
   - unusable output (leaked control tokens such as ``<|tool_call>``, runaway repetition) -> not usable right now
     (``ok=False``): nothing is cached and the probe is repeated later;
   - a 4xx that says the *model* is not served (shared catalogs change: AMD Token Factory answered 400
     ``Requested model Gemma-4-31B-IT not supported`` on 2026-10-14) -> ``ok=False`` with ``model_served=False``
     and the ids ``GET /models`` lists, so the caller can move to a configured fallback model.
2. The tool result is sent back and the model must quote the code it carries. OpenAI's ``role: "tool"`` is tried
   first, then ``role: "tool_responses"`` (the role Google ADK's ``LiteLlm`` sends for model ids matching
   ``gemma-4`` / ``gemma4``, which a strict OpenAI gateway may not accept). The role that works is reported as
   ``result_role`` and ``EndpointLlm`` sends tool results with it; if neither works -> ``json``.

Connection errors, 401 / 403, 429 and 5xx mean "not probed" (``ok=False``): the caller keeps its default and
probes again later. The probe is synchronous (``httpx``) and never logs or returns the API key. ``ProbeCache``
keeps successful results on disk (endpoint + model -> mode, role) so a short-lived process such as the CLI does not
spend requests on every start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("wapsi.agent.probe")

NATIVE, JSON = "native", "json"
ROLE_TOOL, ROLE_TOOL_RESPONSES = "tool", "tool_responses"
RESULT_ROLES = (ROLE_TOOL, ROLE_TOOL_RESPONSES)
PROBE_TOOL = "wapsi_probe"
PROBE_VALUE = 7
PROBE_CODE = "WAPSI-7421"
REJECTS_TOOLS = frozenset({400, 404, 405, 415, 422, 501})  # the request shape is not supported: use the fallback
CACHE_TTL_S = 24 * 3600.0
CACHE_VERSION = 2

_PROBE_TOOLS = [{
    "type": "function",
    "function": {
        "name": PROBE_TOOL,
        "description": "Capability check. Call this function with the value the user gives; it returns a code.",
        "parameters": {"type": "object", "properties": {"value": {"type": "integer", "description": "the value"}},
                       "required": ["value"]},
    },
}]
_CONTROL_TOKEN = re.compile(r"<\|[A-Za-z_/]{2,40}\|?>|<[A-Za-z_/]{2,40}\|>|<(?:start|end)_of_turn>")


def looks_degenerate(text: str) -> str | None:
    """Why ``text`` is not usable model output (leaked control tokens, runaway repetition), or ``None``.

    Conservative: ordinary prose, Markdown tables and JSON pass. Seen live on a shared endpoint: a tool turn that
    came back as ``<|tool_call>//1//////...`` with ``finish_reason: length``.
    """
    body = (text or "").strip()
    if not body:
        return None
    m = _CONTROL_TOKEN.search(body)
    if m:
        return f"control token {m.group(0)[:24]!r} in the text"
    dense = re.sub(r"\s+", "", body)
    if len(dense) >= 48:
        char, n = Counter(dense).most_common(1)[0]
        if n / len(dense) >= 0.6:
            return f"runaway repetition of {char!r}"
    words = body.split()
    if len(words) >= 24 and len(set(words)) / len(words) <= 0.12:
        return "runaway repetition"
    return None


@dataclass(frozen=True)
class ToolProbe:
    """Result of one probe. ``ok`` is False when the endpoint could not be assessed (``mode`` is then a default)."""

    mode: str  # native | json
    ok: bool
    reason: str
    base_url: str = ""
    model: str = ""
    result_role: str = ROLE_TOOL  # native mode: the message role tool results are sent with
    status_code: int | None = None
    latency_ms: float | None = None  # the first request (the tool-call turn)
    requests: int = 0
    probed_at: str = ""
    cached: bool = False
    model_served: bool | None = None  # False: the endpoint says it does not serve this model (None: not asked)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a function-calling assistant. Use the provided function."},
        {"role": "user", "content": (f"Call the {PROBE_TOOL} function with value {PROBE_VALUE}, then tell me the "
                                     "code it returned.")},
    ]


def probe_body(model: str, messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # 512 tokens: a model that thinks before it answers spends part of the budget on reasoning
    return {"model": model, "messages": messages or probe_messages(), "tools": _PROBE_TOOLS, "tool_choice": "auto",
            "temperature": 0.0, "max_tokens": 512}


def _detail(resp: httpx.Response) -> str:
    """The server's error message, in either AMD Token Factory shape (``{"error": {...}}`` or
    ``{"detail": {"error": {...}}}``), trimmed."""
    try:
        data: Any = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:200].strip()
    for _ in range(3):
        if isinstance(data, dict) and isinstance(data.get("detail"), dict | str):
            data = data["detail"]
        elif isinstance(data, dict) and isinstance(data.get("error"), dict | str):
            data = data["error"]
        else:
            break
    if isinstance(data, dict):
        data = data.get("message") or data.get("status") or data
    return str(data)[:200]


_ABOUT_TOOLS = re.compile(r"tool|function", re.IGNORECASE)
_ABOUT_MODEL = re.compile(r"model\b.{0,80}(not supported|not found|does not exist|unknown|not served|unavailable)"
                          r"|(unknown|invalid|unsupported) model", re.IGNORECASE | re.DOTALL)


def served_models(base_url: str, api_key: str | None = None, *, timeout_s: float = 15.0,
                  transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None) -> list[str] | None:
    """Model ids ``GET {base_url}/models`` lists right now, or ``None`` when the endpoint does not say."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    own = client is None
    http = client or httpx.Client(timeout=timeout_s, transport=transport)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/models", headers=headers)
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            http.close()
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")]


def _message(data: Any) -> dict[str, Any]:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {}
    return message if isinstance(message, dict) else {}


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return [c for c in calls if isinstance(c, dict) and isinstance(c.get("function"), dict)] \
        if isinstance(calls, list) else []


def probe_tool_calling(base_url: str, model: str, api_key: str | None = None, *, timeout_s: float = 30.0,
                       default: str = NATIVE, transport: httpx.BaseTransport | None = None,
                       client: httpx.Client | None = None, pause_s: float = 0.0,
                       roles: tuple[str, ...] = RESULT_ROLES) -> ToolProbe:
    """Probe the endpoint. ``default`` is the mode reported when it cannot be assessed; ``pause_s`` spaces the
    requests (shared rate limits); ``roles`` are the tool-result roles to try, in order."""
    base = base_url.rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    sent = 0
    first_ms: float | None = None

    def result(mode: str, ok: bool, reason: str, status: int | None = None, role: str = ROLE_TOOL,
               served: bool | None = None) -> ToolProbe:
        return ToolProbe(mode=mode, ok=ok, reason=reason[:300], base_url=base, model=model, result_role=role,
                         status_code=status, latency_ms=first_ms, requests=sent, probed_at=stamp,
                         model_served=served if served is not None else (True if ok else None))

    own = client is None
    http = client or httpx.Client(timeout=timeout_s, transport=transport)

    def post(body: dict[str, Any]) -> httpx.Response | str:
        nonlocal sent, first_ms
        if sent and pause_s > 0:
            time.sleep(pause_s)
        sent += 1
        t0 = time.perf_counter()
        try:
            resp = http.post(f"{base}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            return f"the endpoint could not be reached ({type(exc).__name__})"
        if first_ms is None:
            first_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return resp

    try:
        # ---- step 1: does the model return a tool call?
        resp = post(probe_body(model))
        if isinstance(resp, str):
            return result(default, False, resp)
        code = resp.status_code
        if code in (401, 403):
            return result(default, False, f"HTTP {code}: authentication failed (check WAPSI_LLM_API_KEY)", code)
        if code in REJECTS_TOOLS:
            detail = _detail(resp)
            if _ABOUT_MODEL.search(detail) and not _ABOUT_TOOLS.search(detail):
                ids = served_models(base, api_key, client=http)
                listed = f"; served now: {', '.join(ids[:12])}" if ids else ""
                return result(default, False, f"HTTP {code}: the endpoint does not serve model {model!r} "
                                              f"({detail}){listed}", code, served=False)
            if _ABOUT_TOOLS.search(detail) or code in (405, 415, 501):
                return result(JSON, True, f"HTTP {code}: the endpoint rejected the tools request ({detail})", code)
            return result(default, False, f"HTTP {code}: {detail}", code)
        if code >= 400:
            return result(default, False, f"HTTP {code}: {_detail(resp)}", code)
        try:
            message = _message(resp.json())
        except ValueError:
            return result(default, False, "the endpoint returned a non-JSON body", code)
        calls = _tool_calls(message)
        names = [str(c["function"].get("name")) for c in calls]
        if PROBE_TOOL not in names:
            bad = looks_degenerate(_text(message))
            if bad:
                return result(default, False, f"the model returned unusable output ({bad})", code)
            if names:
                return result(JSON, True, f"the model called an unknown function ({names[0][:40]})", code)
            return result(JSON, True, "the endpoint accepted tools but the model answered in text", code)

        # ---- step 2: does the model understand the tool result, and under which message role?
        call = next(c for c in calls if c["function"].get("name") == PROBE_TOOL)
        call_id = str(call.get("id") or "call_probe")
        assistant = {"role": "assistant", "content": message.get("content"),
                     "tool_calls": [{"id": call_id, "type": "function",
                                     "function": {"name": PROBE_TOOL,
                                                  "arguments": str(call["function"].get("arguments") or "{}")}}]}
        notes = []
        for role in roles:
            follow = [*probe_messages(), assistant,
                      {"role": role, "tool_call_id": call_id,
                       "content": json.dumps({"status": "ok", "code": PROBE_CODE})}]
            resp = post(probe_body(model, follow))
            if isinstance(resp, str):
                return result(default, False, resp)
            code = resp.status_code
            if code in (401, 403, 429) or code >= 500:
                return result(default, False, f"HTTP {code}: {_detail(resp)}", code)
            if code >= 400:
                notes.append(f"role {role!r}: HTTP {code}")
                continue
            try:
                text = _text(_message(resp.json()))
            except ValueError:
                return result(default, False, "the endpoint returned a non-JSON body", code)
            if PROBE_CODE.split("-")[1] in text:
                return result(NATIVE, True, f"tool call returned and its result understood (role {role!r})",
                              code, role)
            bad = looks_degenerate(text)
            if bad:
                return result(default, False, f"the model returned unusable output ({bad})", code)
            notes.append(f"role {role!r}: the result was not used")
        return result(JSON, True, "the model returned a tool call but did not use its result ("
                      + "; ".join(notes) + ")", code)
    finally:
        if own:
            http.close()


class ProbeCache:
    """Successful probe results on disk: ``{sha256(base_url|model): ToolProbe}`` in one small JSON file."""

    def __init__(self, path: str | Path, *, ttl_s: float = CACHE_TTL_S) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s

    @staticmethod
    def key(base_url: str, model: str) -> str:
        return hashlib.sha256(f"v{CACHE_VERSION}|{base_url.rstrip('/')}|{model}".encode()).hexdigest()[:32]

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, base_url: str, model: str) -> ToolProbe | None:
        row = self._load().get(self.key(base_url, model))
        if (not isinstance(row, dict) or row.get("mode") not in (NATIVE, JSON) or not row.get("ok")
                or row.get("result_role") not in RESULT_ROLES):
            return None
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(str(row.get("probed_at")))).total_seconds()
        except ValueError:
            return None
        if age < 0 or age > self.ttl_s:
            return None
        fields = {k: row.get(k) for k in ("mode", "ok", "reason", "base_url", "model", "result_role", "status_code",
                                         "latency_ms", "requests", "probed_at", "model_served")}
        return ToolProbe(**{**fields, "cached": True})

    def put(self, probe: ToolProbe) -> None:
        if not probe.ok:
            return
        data = self._load()
        data[self.key(probe.base_url, probe.model)] = {**probe.as_dict(), "cached": False}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
            tmp.replace(self.path)
        except OSError:
            log.warning("could not write the tool-probe cache %s", self.path)

    def forget(self, base_url: str, model: str) -> None:
        data = self._load()
        if data.pop(self.key(base_url, model), None) is not None:
            try:
                self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
            except OSError:
                pass
