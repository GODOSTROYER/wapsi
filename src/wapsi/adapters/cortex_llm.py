"""``CortexLLM``: ``wapsi.ports.LLM`` over Snowflake Cortex, called through a Snowpark session.

Two call shapes (both run where the data is; nothing leaves Snowflake):

- ``mode="ai_complete"`` (default): ``AI_COMPLETE(model => ..., prompt => ?, model_parameters => {...},
  response_format => {...}, show_details => TRUE)``. The chat turns are flattened into one prompt string.
- ``mode="complete"``: ``SNOWFLAKE.CORTEX.COMPLETE('<model>', PARSE_JSON(?), {...})`` with the chat history as
  an array of ``{role, content}`` objects (the options object carries temperature, max_tokens,
  response_format and guardrails).

The prompt (which carries untrusted complaint text inside the drafter's markers) is always a ``?`` bind. The
model name, the options and the JSON schema come from code, not from users; they are validated / escaped and
written as SQL constants because Cortex functions expect constant arguments there.

The LLM only ever writes prose with ``⟦slot⟧`` placeholders (``wapsi.draft.prose``); amounts, account numbers,
UTRs and dates stay in slots and the verifier recomputes them. ``calls`` keeps one usage record per request
(tokens when Cortex returns them) for cost-per-case reporting.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_MODEL = "claude-sonnet-5"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_$@-]{1,100}$")
_MODES = ("ai_complete", "complete")


class CortexError(RuntimeError):
    pass


def sql_string(value: str) -> str:
    """A Snowflake single-quoted string constant (backslashes and quotes escaped)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_constant(value: Any) -> str:
    """Python JSON-like value -> Snowflake OBJECT / ARRAY / scalar constant."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        if isinstance(value, float) and value != value:  # NaN
            raise ValueError("NaN is not a valid constant")
        return repr(value)
    if isinstance(value, str):
        return sql_string(value)
    if isinstance(value, Mapping):
        parts = []
        for k, v in value.items():
            if not isinstance(k, str) or not _KEY_RE.match(k):
                raise ValueError(f"unsupported object key {k!r}")
            parts.append(f"{sql_string(k)}: {sql_constant(v)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(sql_constant(v) for v in value) + "]"
    raise TypeError(f"unsupported constant type {type(value).__name__}")


def cortex_schema(json_schema: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of a JSON schema Cortex structured output takes (drops ``title`` / ``$schema``)."""
    return {k: v for k, v in json_schema.items() if k not in ("title", "$schema")}


def flatten_messages(messages: Sequence[Mapping[str, str]]) -> str:
    """Chat turns as one prompt string for single-string AI_COMPLETE (system first, labelled turns)."""
    parts: list[str] = []
    for m in messages:
        role = str(m.get("role", "user")).upper()
        parts.append(f"### {role}\n{m.get('content', '')}")
    parts.append("### ASSISTANT\n")
    return "\n\n".join(parts)


def parse_response(raw: Any) -> tuple[str, dict[str, Any]]:
    """(text, usage) from an AI_COMPLETE / COMPLETE result (string, JSON string or object)."""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw, {}
    if isinstance(data, Mapping) and isinstance(data.get("choices"), list):
        usage = dict(data.get("usage") or {})
        choice = data["choices"][0] if data["choices"] else {}
        msg = choice.get("messages", choice.get("message", "")) if isinstance(choice, Mapping) else choice
        if isinstance(msg, Mapping) and "content" in msg and len(msg) <= 2:
            msg = msg["content"]
        text = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
        if data.get("model"):
            usage.setdefault("model", data["model"])
        return text, usage
    if isinstance(data, Mapping | list):
        return json.dumps(data, ensure_ascii=False), {}
    return str(data), {}


class CortexLLM:
    """Implements ``wapsi.ports.LLM`` with Cortex AI_COMPLETE / COMPLETE through ``session.sql``."""

    def __init__(
        self,
        session: Any,
        model: str = DEFAULT_MODEL,
        *,
        mode: str = "ai_complete",
        guardrails: bool = False,
        name: str | None = None,
    ) -> None:
        if not _MODEL_RE.match(model or ""):
            raise ValueError(f"invalid Cortex model name: {model!r}")
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}")
        self.session = session
        self.model = model
        self.mode = mode
        self.guardrails = guardrails
        self.name = name or f"cortex:{model}"
        self.calls: list[dict[str, Any]] = []

    def build_sql(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: Mapping[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        params: dict[str, Any] = {"temperature": float(temperature), "max_tokens": int(max_tokens)}
        if self.guardrails:
            params["guardrails"] = True
        fmt = {"type": "json", "schema": cortex_schema(json_schema)} if json_schema else None
        if self.mode == "ai_complete":
            args = [
                f"model => {sql_string(self.model)}",
                "prompt => ?",
                f"model_parameters => {sql_constant(params)}",
            ]
            if fmt is not None:
                args.append(f"response_format => {sql_constant(fmt)}")
            args.append("show_details => TRUE")
            return f"SELECT AI_COMPLETE({', '.join(args)}) AS RESPONSE", [flatten_messages(messages)]
        options = dict(params)
        if fmt is not None:
            options["response_format"] = fmt
        history = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]
        sql = (
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE({sql_string(self.model)}, PARSE_JSON(?), "
            f"{sql_constant(options)}) AS RESPONSE"
        )
        return sql, [json.dumps(history, ensure_ascii=False)]

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        sql, binds = self.build_sql(messages, temperature=temperature, max_tokens=max_tokens, json_schema=json_schema)
        t0 = time.perf_counter()
        try:
            rows = self.session.sql(sql, params=binds).collect()
        except Exception as exc:
            self.calls.append({"model": self.model, "mode": self.mode, "ok": False, "error": type(exc).__name__,
                               "seconds": round(time.perf_counter() - t0, 3)})
            raise CortexError(f"Cortex {self.mode} failed: {exc}") from exc
        raw = _first_value(rows)
        text, usage = parse_response(raw)
        self.calls.append({"model": self.model, "mode": self.mode, "ok": True,
                           "seconds": round(time.perf_counter() - t0, 3), **usage})
        return text

    def usage_summary(self) -> dict[str, Any]:
        """Totals over ``calls`` (for AUDIT_LOG / eval cost-per-case)."""
        out: dict[str, Any] = {"model": self.model, "mode": self.mode, "calls": len(self.calls),
                               "failed": sum(1 for c in self.calls if not c.get("ok"))}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "guard_tokens"):
            vals = [c[key] for c in self.calls if isinstance(c.get(key), int | float)]
            if vals:
                out[key] = int(sum(vals))
        return out


def _first_value(rows: Sequence[Any]) -> Any:
    if not rows:
        raise CortexError("Cortex returned no rows")
    row = rows[0]
    if hasattr(row, "as_dict"):
        vals = list(row.as_dict().values())
    elif isinstance(row, Mapping):
        vals = list(row.values())
    else:
        vals = list(row)
    if not vals:
        raise CortexError("Cortex returned an empty row")
    return vals[0]
