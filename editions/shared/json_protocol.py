"""Strict JSON tool protocol: tool use over plain chat completions, for endpoints without native tool calls.

A safety net, chosen by the capability probe (``probe``): AMD Token Factory and vLLM with a tool-call parser return
native OpenAI tool calls and never need it. When an endpoint (or a fine-tuned checkpoint) does not, the agent
still works, because this protocol needs nothing but ``POST /chat/completions``:

- the tool declarations go into the system message as JSON Schemas, under the marker ``WAPSI_TOOL_PROTOCOL v1``;
- the model answers with exactly one JSON object per turn: ``{"tool": "<name>", "args": {...}}`` to call a tool,
  or ``{"final": "<reply to the officer>"}`` to finish (``response_format: json_object`` when the endpoint has it);
- earlier calls are replayed as the model's own JSON and tool results as ``TOOL_RESULT <name>: <json>`` user
  messages, so the conversation is ordinary chat text.

Strict: the tool must be a declared one, the arguments must satisfy its schema (required keys, types, no extra
keys), one call per turn, and a bounded number of calls per officer message. An answer that breaks the protocol
gets one repair request that names the problem; after that the turn ends with a plain notice. Everything here is
pure Python on google-genai types (no HTTP); ``endpoint.EndpointLlm`` sends the rewritten request through LiteLLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

PROTOCOL_MARKER = "WAPSI_TOOL_PROTOCOL v1"
RESULT_PREFIX = "TOOL_RESULT"
ERROR_PREFIX = "PROTOCOL_ERROR"
MAX_REPAIRS = 1
MAX_RESULT_CHARS = 24000
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "array": (list,),
    "object": (dict,), "null": (type(None),),
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema of the arguments object


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Final:
    text: str


@dataclass(frozen=True)
class ProtocolError:
    message: str


Action = ToolCall | Final | ProtocolError


# --------------------------------------------------------------------------- tool declarations


def _lower_types(node: Any) -> Any:
    """google-genai ``Schema`` dumps use upper-case type names (``OBJECT``); JSON Schema uses lower case."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            elif k == "any_of":
                out["anyOf"] = _lower_types(v)
            else:
                out[k] = _lower_types(v)
        return out
    if isinstance(node, list):
        return [_lower_types(x) for x in node]
    return node


def _slim(node: Any) -> Any:
    """Drop schema noise the model does not need (pydantic titles)."""
    if isinstance(node, dict):
        return {k: _slim(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_slim(x) for x in node]
    return node


def schema_of(declaration: Any) -> dict[str, Any]:
    """The JSON Schema of a ``FunctionDeclaration``'s arguments (an object schema, possibly without properties)."""
    raw = getattr(declaration, "parameters_json_schema", None)
    if not isinstance(raw, dict):
        params = getattr(declaration, "parameters", None)
        raw = _lower_types(params.model_dump(mode="json", exclude_none=True)) if params is not None else {}
    schema = _slim(raw)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def tool_specs(llm_request: Any) -> list[ToolSpec]:
    """The function tools declared on an ADK ``LlmRequest``."""
    out: list[ToolSpec] = []
    config = getattr(llm_request, "config", None)
    for tool in (getattr(config, "tools", None) or []):
        for decl in (getattr(tool, "function_declarations", None) or []):
            if decl.name:
                out.append(ToolSpec(name=decl.name, description=(decl.description or "").strip(),
                                    parameters=schema_of(decl)))
    return out


# --------------------------------------------------------------------------- validation


def _type_ok(value: Any, name: str) -> bool:
    kinds = _JSON_TYPES.get(name)
    if kinds is None:
        return True  # unknown keyword: do not reject what the schema does not describe
    if isinstance(value, bool) and name in ("integer", "number"):
        return False
    return isinstance(value, kinds)


def validate(value: Any, schema: dict[str, Any], path: str = "args") -> list[str]:
    """Problems with ``value`` against the JSON Schema subset tool declarations use (``type``, ``properties``,
    ``required``, ``items``, ``enum``, ``anyOf``). Unknown object keys are errors."""
    if not isinstance(schema, dict) or not schema:
        return []
    options = schema.get("anyOf")
    if isinstance(options, list) and options:
        if any(not validate(value, o, path) for o in options if isinstance(o, dict)):
            return []
        return [f"{path} does not match any allowed form"]
    errors: list[str] = []
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else [k for k in kind if isinstance(k, str)] if isinstance(kind, list) else []
    if kinds and not any(_type_ok(value, k) for k in kinds):
        return [f"{path} must be {' or '.join(kinds)}, got {type(value).__name__}"]
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path} must be one of {enum[:8]}")
    if isinstance(value, dict) and ("object" in kinds or "properties" in schema):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        for key, item in value.items():
            if key not in props:
                errors.append(f"{path}.{key} is not an argument of this tool")
            else:
                errors.extend(validate(item, props[key], f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value[:50]):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors[:6]


# --------------------------------------------------------------------------- parsing the model's answer


def _first_object(text: str) -> Any:
    """The first JSON value in ``text`` (whole text, a fenced block, or the first ``{...}`` inside prose)."""
    body = text.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group("body").strip()
    try:
        return json.loads(body)
    except ValueError:
        pass
    start = body.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    value, _ = json.JSONDecoder().raw_decode(body[start:])
    return value


def parse_action(text: str, specs: list[ToolSpec]) -> Action:
    """Read one model answer. Strict: anything that is not one JSON object of the protocol is an error (prose
    is never taken as the reply: a model that has lost the protocol must not reach the officer unchecked)."""
    text = (text or "").strip()
    if not text:
        return ProtocolError("the reply was empty")
    try:
        obj = _first_object(text)
    except ValueError:
        return ProtocolError("the reply is not a JSON object")
    if isinstance(obj, list):
        return ProtocolError("one tool call per reply: send a single JSON object, not a list")
    if not isinstance(obj, dict):
        return ProtocolError("the reply must be a JSON object")
    has_tool, has_final = "tool" in obj, "final" in obj
    if has_tool and has_final:
        return ProtocolError('send either {"tool": ..., "args": ...} or {"final": ...}, not both')
    if has_final:
        extra = sorted(set(obj) - {"final"})
        if extra:
            return ProtocolError(f"unexpected keys next to final: {', '.join(extra[:4])}")
        if not isinstance(obj["final"], str) or not obj["final"].strip():
            return ProtocolError("final must be a non-empty string")
        return Final(obj["final"].strip())
    if not has_tool:
        return ProtocolError('the object needs a "tool" key (with "args") or a "final" key')
    extra = sorted(set(obj) - {"tool", "args"})
    if extra:
        return ProtocolError(f"unexpected keys: {', '.join(extra[:4])} (one tool call per reply)")
    name = obj["tool"]
    spec = next((s for s in specs if s.name == name), None) if isinstance(name, str) else None
    if spec is None:
        known = ", ".join(s.name for s in specs)
        return ProtocolError(f"unknown tool {str(name)[:60]!r}; the tools are: {known}")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        return ProtocolError("args must be a JSON object")
    problems = validate(args, spec.parameters)
    if problems:
        return ProtocolError(f"invalid arguments for {spec.name}: " + "; ".join(problems))
    return ToolCall(spec.name, args)


# --------------------------------------------------------------------------- building the plain request


def protocol_instruction(specs: list[ToolSpec], *, calls_left: int) -> str:
    """The system-message block that teaches the protocol and lists the tools."""
    lines = [
        PROTOCOL_MARKER,
        "This endpoint has no native function calling. To use a tool, reply with exactly one JSON object and "
        "nothing else:",
        '{"tool": "<tool name>", "args": {<arguments>}}',
        f"One tool call per reply. You then receive a user message '{RESULT_PREFIX} <tool name>: <JSON result>'.",
        "When you have what you need, reply with exactly one JSON object:",
        '{"final": "<your answer to the officer; Markdown and line breaks allowed>"}',
        "Use only the tools and argument names below (JSON Schema of args). Never invent a tool or an argument.",
        "",
        "Tools:",
    ]
    for s in specs:
        lines.append(f"- {s.name}: {s.description}")
        lines.append(f"  args schema: {json.dumps(s.parameters, ensure_ascii=False, sort_keys=True)}")
    if calls_left <= 0:
        lines += ["", 'You have used every tool call allowed for this request. Reply now with {"final": "..."}.']
    else:
        lines += ["", f"You may make at most {calls_left} more tool call(s) for this request."]
    return "\n".join(lines)


def _is_officer_text(content: types.Content) -> bool:
    parts = content.parts or []
    return (content.role == "user" and any(p.text for p in parts)
            and not any(p.function_response for p in parts))


def count_tool_results(contents: list[types.Content] | None) -> int:
    """Tool results since the officer's latest message (the calls already spent on this request)."""
    contents = list(contents or [])
    last = max((i for i, c in enumerate(contents) if _is_officer_text(c)), default=-1)
    return sum(1 for c in contents[last + 1:] for p in (c.parts or []) if p.function_response)


def _dump(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …[truncated]"


def rewrite_contents(contents: list[types.Content] | None) -> list[types.Content]:
    """Replay the conversation as plain text: calls as the model's JSON, results as ``TOOL_RESULT`` messages."""
    out: list[types.Content] = []
    for content in contents or []:
        parts: list[types.Part] = []
        for p in content.parts or []:
            if getattr(p, "thought", False):
                continue
            if p.function_call is not None:
                parts.append(types.Part(text=json.dumps(
                    {"tool": p.function_call.name, "args": dict(p.function_call.args or {})}, ensure_ascii=False)))
            elif p.function_response is not None:
                parts.append(types.Part(text=(
                    f"{RESULT_PREFIX} {p.function_response.name}: {_dump(p.function_response.response)}\n"
                    "Reply with the next JSON object.")))
            elif p.text:
                parts.append(types.Part(text=p.text))
        if parts:
            role = "user" if any(p.function_response for p in (content.parts or [])) else (content.role or "user")
            out.append(types.Content(role=role, parts=parts))
    return out


def system_text(config: Any) -> str:
    """The system instruction of a ``GenerateContentConfig`` as text."""
    raw = getattr(config, "system_instruction", None)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    parts = getattr(raw, "parts", None)
    if parts is not None:
        return "\n".join(p.text for p in parts if getattr(p, "text", None))
    if isinstance(raw, list):
        return "\n".join(x if isinstance(x, str) else str(getattr(x, "text", "") or "") for x in raw)
    return str(getattr(raw, "text", "") or raw)


def repair_message(problem: str) -> str:
    return (f"{ERROR_PREFIX}: {problem}. Reply again with exactly one JSON object: "
            '{"tool": "<tool name>", "args": {...}} or {"final": "..."}.')


def call_limit_notice(limit: int) -> str:
    return (f"[Wapsi] The agent stopped after {limit} tool calls for one request (the step limit). The results "
            "so far are in the agent trace; ask a narrower question to continue.")


def invalid_notice(problem: str) -> str:
    return ("[Wapsi] The model did not produce a valid tool call or reply for this endpoint's JSON tool protocol "
            f"({problem[:200]}). Nothing was executed for that turn; please ask again.")
