"""OpenAI-compatible chat-completions client (implements ``wapsi.ports.LLM``).

Works with any server exposing ``POST {base_url}/chat/completions``: vLLM, the AMD Token Factory
(``https://developer.amd.com.cn/radeon/api/v1``), Gemini's OpenAI-compatible endpoint
(``https://generativelanguage.googleapis.com/v1beta/openai``), Ollama, OpenAI, and so on.

The API key comes from the ``api_key`` argument or the ``WAPSI_LLM_API_KEY`` environment variable (optional for
local servers). 429 and 5xx responses and connection errors are retried with exponential backoff (``Retry-After``
honoured); other errors raise ``LLMError`` at once.

Extra request fields: ``extra_body`` (constructor, every request) and ``complete(..., extra_body=...)`` (one request,
merged over the constructor's) add server-specific fields to the JSON body, e.g. vLLM's
``{"chat_template_kwargs": {"enable_thinking": False}}`` to switch off Qwen's thinking, or ``top_p`` / ``seed``. The
fields the client controls (``model``, ``messages``, ``temperature``, ``max_tokens`` and, with a schema,
``response_format``) always win; ``model``, ``messages`` and ``stream`` cannot be set as extras.

Token usage: ``complete_with_usage`` returns the text and that request's ``TokenUsage`` (``None`` when the server
reports none); ``last_usage`` holds the latest one and ``usage`` the running total over the client's life
(thread-safe; ``reset_usage()`` returns and clears it). A server that prices its answers (``usage.cost``, e.g. the
AMD Token Factory) has that figure in ``TokenUsage.cost_usd``. ``last_rate_limit`` holds the latest response's
``x-ratelimit-*`` headers; an ``LLMError`` for a 429 / 503 carries the server's ``Retry-After`` as
``retry_after_s``. ``host`` is the endpoint's host name (never the key).
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

API_KEY_ENV = "WAPSI_LLM_API_KEY"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RESERVED_EXTRA_FIELDS = frozenset({"model", "messages", "stream"})
# how a JSON answer is asked for: a strict schema, a plain JSON object, or "none" (no response_format field at
# all: the prompt asks for JSON and the caller parses it; for servers whose structured-output modes misbehave)
JSON_MODES = ("json_schema", "json_object", "none")


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.retry_after_s = retry_after_s  # the server's Retry-After, when it sent one


@dataclass(frozen=True)
class TokenUsage:
    """Token counts of one response (``calls`` = 1) or a sum of responses (``a + b``)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0  # responses that reported usage
    cost_usd: float = 0.0  # the server's own ``usage.cost`` (0.0 when it reports none)

    @classmethod
    def from_response(cls, data: Mapping[str, Any]) -> TokenUsage | None:
        """The OpenAI ``usage`` object of a chat-completions response, or ``None`` if there is none."""
        usage = data.get("usage")
        if not isinstance(usage, Mapping):
            return None

        def count(key: str) -> int:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                return 0
            return max(0, int(value))

        prompt, completion = count("prompt_tokens"), count("completion_tokens")
        cost = usage.get("cost")
        priced = isinstance(cost, int | float) and not isinstance(cost, bool) and cost > 0
        return cls(prompt, completion, count("total_tokens") or prompt + completion, 1,
                   float(cost) if priced else 0.0)

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
            self.calls + other.calls,
            self.cost_usd + other.cost_usd,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


def _check_extra(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    fields = dict(extra or {})
    reserved = sorted(RESERVED_EXTRA_FIELDS & set(fields))
    if reserved:
        raise ValueError(f"extra_body cannot set {', '.join(reserved)}")
    return fields


def _error_detail(resp: httpx.Response) -> str:
    try:
        data: Any = resp.json()
    except ValueError:
        return resp.text[:300].strip() or resp.reason_phrase
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        detail = data.get("detail")  # FastAPI-style gateways wrap the error: {"detail": {"error": {...}}}
        if "error" not in data and detail is not None:
            data = detail if isinstance(detail, dict) else {"error": detail}
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or err)[:300]
        return str(err)[:300]
    return str(data)[:300]


def endpoint_host(base_url: str) -> str:
    """``https://developer.amd.com.cn/radeon/api/v1`` -> ``developer.amd.com.cn``; ``http://127.0.0.1:8000/v1`` ->
    ``127.0.0.1:8000``. Never contains credentials."""
    parts = urlsplit(base_url if "//" in base_url else f"//{base_url}")
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if host and port else host


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 60,
        extra_headers: Mapping[str, str] | None = None,
        *,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        json_mode: str = "json_schema",
        name: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not model:
            raise ValueError("model is required")
        if json_mode not in JSON_MODES:
            raise ValueError("json_mode must be 'json_schema', 'json_object' or 'none'")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = name or model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.json_mode = json_mode
        self.extra_body: dict[str, Any] = _check_extra(extra_body)
        self.last_usage: TokenUsage | None = None
        self.last_rate_limit: dict[str, str] = {}
        self._usage = TokenUsage()
        self._usage_lock = threading.Lock()
        self._sleep = sleep
        key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._has_key = bool(key)
        headers.update(dict(extra_headers or {}))
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @property
    def url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def host(self) -> str:
        """Host name of the endpoint (with the port when the URL names one), e.g. ``developer.amd.com.cn``."""
        return endpoint_host(self.base_url)

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatLLM(base_url={self.base_url!r}, model={self.model!r}, api_key={'***' if self._has_key else None})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatLLM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token usage
    @property
    def usage(self) -> TokenUsage:
        """Running total over every response that reported usage."""
        with self._usage_lock:
            return self._usage

    def reset_usage(self) -> TokenUsage:
        """Return the running total and start again from zero."""
        with self._usage_lock:
            total, self._usage = self._usage, TokenUsage()
            return total

    # -- LLM protocol
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> str:
        return self.complete_with_usage(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_schema=json_schema,
            extra_body=extra_body,
        )[0]

    def complete_with_usage(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> tuple[str, TokenUsage | None]:
        """Like ``complete``; also returns this request's token usage (``None`` if the server reported none)."""
        payload: dict[str, Any] = {**self.extra_body, **_check_extra(extra_body)}
        payload.update(
            model=self.model,
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_schema is not None and self.json_mode != "none":
            payload["response_format"] = self._response_format(json_schema)
        data = self._post(payload)
        usage = TokenUsage.from_response(data)
        with self._usage_lock:
            self.last_usage = usage
            if usage is not None:
                self._usage = self._usage + usage
        return self._content(data), usage

    # -- internals
    def _response_format(self, schema: dict) -> dict[str, Any]:
        if self.json_mode == "json_object":
            return {"type": "json_object"}
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(schema.get("title") or "response"))[:64]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "schema": schema,
                "strict": schema.get("additionalProperties") is False,
            },
        }

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_backoff_s)
        return min(self.backoff_s * (2**attempt), self.max_backoff_s)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last: LLMError | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            retry_after = None
            try:
                resp = self._client.post(self.url, json=payload)
            except httpx.TimeoutException as exc:
                last = LLMError(f"timed out after {self.timeout}s calling {self.url}: {type(exc).__name__}")
            except httpx.TransportError as exc:
                last = LLMError(f"connection error calling {self.url}: {exc}")
            else:
                code = resp.status_code
                self.last_rate_limit = {k.lower(): v for k, v in resp.headers.items()
                                        if k.lower().startswith("x-ratelimit-")}
                if code < 400:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(
                            f"{self.url} returned a non-JSON body", status_code=code, body=resp.text[:2000]
                        ) from exc
                    if not isinstance(data, dict):
                        raise LLMError(
                            f"{self.url} returned unexpected JSON", status_code=code, body=resp.text[:2000]
                        )
                    return data
                detail = _error_detail(resp)
                hint = f" (check {API_KEY_ENV})" if code in (401, 403) else ""
                retry_after = _retry_after(resp)
                err = LLMError(
                    f"{self.url} returned HTTP {code}: {detail}{hint}",
                    status_code=code,
                    body=resp.text[:2000],
                    retry_after_s=retry_after,
                )
                if code not in RETRY_STATUS:
                    raise err
                last = err
            if attempt < attempts - 1:
                self._sleep(self._delay(attempt, retry_after))
        assert last is not None
        raise LLMError(
            f"giving up after {attempts} attempt(s): {last}", status_code=last.status_code, body=last.body,
            retry_after_s=last.retry_after_s,
        )

    @staticmethod
    def _content(data: dict[str, Any]) -> str:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("malformed response: no choices[0].message") from exc
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            refusal = message.get("refusal") if isinstance(message, dict) else None
            extra = f"; refusal: {refusal}" if refusal else ""
            raise LLMError(f"model returned no content (finish_reason={choice.get('finish_reason')}{extra})")
        return str(content)
