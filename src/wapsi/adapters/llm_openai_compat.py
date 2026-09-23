"""OpenAI-compatible chat-completions client (implements ``wapsi.ports.LLM``).

Works with any server exposing ``POST {base_url}/chat/completions``: vLLM, Gemini's OpenAI-compatible endpoint
(``https://generativelanguage.googleapis.com/v1beta/openai``), Ollama, OpenAI, and so on.

The API key comes from the ``api_key`` argument or the ``WAPSI_LLM_API_KEY`` environment variable (optional for
local servers). 429 and 5xx responses and connection errors are retried with exponential backoff (``Retry-After``
honoured); other errors raise ``LLMError`` at once.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

API_KEY_ENV = "WAPSI_LLM_API_KEY"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _error_detail(resp: httpx.Response) -> str:
    try:
        data: Any = resp.json()
    except ValueError:
        return resp.text[:300].strip() or resp.reason_phrase
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or err)[:300]
        return str(err)[:300]
    return str(data)[:300]


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
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not model:
            raise ValueError("model is required")
        if json_mode not in ("json_schema", "json_object"):
            raise ValueError("json_mode must be 'json_schema' or 'json_object'")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = name or model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.json_mode = json_mode
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

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatLLM(base_url={self.base_url!r}, model={self.model!r}, api_key={'***' if self._has_key else None})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatLLM:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- LLM protocol
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = self._response_format(json_schema)
        return self._content(self._post(payload))

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
                err = LLMError(
                    f"{self.url} returned HTTP {code}: {detail}{hint}",
                    status_code=code,
                    body=resp.text[:2000],
                )
                if code not in RETRY_STATUS:
                    raise err
                last = err
                retry_after = _retry_after(resp)
            if attempt < attempts - 1:
                self._sleep(self._delay(attempt, retry_after))
        assert last is not None
        raise LLMError(
            f"giving up after {attempts} attempt(s): {last}", status_code=last.status_code, body=last.body
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
