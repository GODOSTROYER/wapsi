"""Gemini on Vertex AI (Agent Platform) via the ``google-genai`` client, plus the private Gemma tier.

``GeminiLLM`` implements ``wapsi.ports.LLM``:
- system messages become ``system_instruction``; ``assistant`` turns become ``model`` turns;
- ``json_schema`` switches on JSON mode (``response_mime_type="application/json"`` +
  ``response_json_schema``); the reply is parsed (code fences tolerated) and returned as compact JSON text;
- 408/429/5xx, timeouts, connection errors and unparseable JSON are retried with exponential backoff;
  other errors raise ``LLMError`` at once (a safety block is reported with its reason).

``gemma_private_llm`` returns an ``OpenAICompatLLM`` for Gemma 4 served by vLLM on Cloud Run GPU (the
"victim data stays in-region" tier). When the service requires IAM, pass ``id_token_audience`` and a Google ID
token is attached and refreshed before it expires.

Nothing here touches the network at import or construction time.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from wapsi.adapters.llm_openai_compat import RETRY_STATUS, LLMError, OpenAICompatLLM

DEFAULT_MODEL = "gemini-3.5-flash"
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_ROLE = {"user": "user", "assistant": "model", "model": "model"}


def parse_json_text(text: str) -> Any:
    """Parse model JSON output, tolerating a Markdown code fence around it."""
    m = _FENCE.match(text or "")
    return json.loads(m.group(1) if m else text)


def _status(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def _retryable(exc: BaseException) -> bool:
    code = _status(exc)
    if code is not None:
        return code in RETRY_STATUS
    name = type(exc).__name__
    return isinstance(exc, (TimeoutError, ConnectionError)) or name in {
        "ServerError", "ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "TimeoutException",
    }


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def response_text(resp: Any) -> str:
    """Concatenate text parts of the first candidate (ignores thought parts)."""
    candidates = _field(resp, "candidates") or []
    if candidates:
        content = _field(candidates[0], "content")
        parts = (_field(content, "parts") or []) if content is not None else []
        texts = [str(_field(p, "text")) for p in parts if _field(p, "text") and not _field(p, "thought")]
        if texts:
            return "".join(texts)
    try:
        text = _field(resp, "text")
    except Exception:  # noqa: BLE001 - the SDK property may raise on odd responses
        text = None
    return text or ""


def _block_reason(resp: Any) -> str:
    fb = _field(resp, "prompt_feedback")
    reason = _field(fb, "block_reason") if fb is not None else None
    if reason:
        return f"prompt blocked: {getattr(reason, 'name', reason)}"
    candidates = _field(resp, "candidates") or []
    if candidates:
        fr = _field(candidates[0], "finish_reason")
        if fr:
            return f"finish_reason={getattr(fr, 'name', fr)}"
    return "no candidates"


class GeminiLLM:
    """Implements ``wapsi.ports.LLM`` with Gemini on Vertex AI."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        project: str | None = None,
        location: str = "global",
        client: Any | None = None,
        *,
        name: str | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        max_backoff_s: float = 20.0,
        timeout_s: float = 60.0,
        thinking_budget: int | None = None,
        labels: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        self.model = model
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("WAPSI_PROJECT")
        self.location = location or "global"
        self.name = name or f"vertex:{model}"
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.timeout_s = timeout_s
        self.thinking_budget = thinking_budget
        self.labels = dict(labels or {"app": "wapsi"})
        self._sleep = sleep
        self._client = client
        self.last_usage: Any = None

    def __repr__(self) -> str:
        return f"GeminiLLM(model={self.model!r}, project={self.project!r}, location={self.location!r})"

    @property
    def client(self) -> Any:
        if self._client is None:
            from google import genai  # lazy
            from google.genai import types

            self._client = genai.Client(
                vertexai=True,
                project=self.project,
                location=self.location,
                http_options=types.HttpOptions(timeout=int(self.timeout_s * 1000)),
            )
        return self._client

    # ------------------------------------------------------------------ request building
    def build_request(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = _ROLE.get(m.get("role", ""))
            if role is None:
                continue
            if contents and contents[-1]["role"] == role:  # Gemini wants alternating turns
                contents[-1]["parts"].append({"text": m["content"]})
            else:
                contents.append({"role": role, "parts": [{"text": m["content"]}]})
        if not contents:
            contents.append({"role": "user", "parts": [{"text": "Follow the system instruction."}]})
        config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "candidate_count": 1,
        }
        if system:
            config["system_instruction"] = system
        if json_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = json_schema
        if self.thinking_budget is not None:
            config["thinking_config"] = {"thinking_budget": self.thinking_budget}
        if self.labels:
            config["labels"] = dict(self.labels)
        return contents, config

    # ------------------------------------------------------------------ LLM protocol
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        contents, config = self.build_request(
            messages, temperature=temperature, max_tokens=max_tokens, json_schema=json_schema
        )
        attempts = self.max_retries + 1
        last: LLMError | None = None
        for attempt in range(attempts):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001 - classify below
                err = LLMError(f"Gemini {self.model} call failed: {type(exc).__name__}: {exc}"[:500],
                               status_code=_status(exc))
                if not _retryable(exc):
                    raise err from exc
                last = err
            else:
                self.last_usage = _field(resp, "usage_metadata")
                text = response_text(resp)
                if not text:
                    raise LLMError(f"Gemini {self.model} returned no text ({_block_reason(resp)})")
                if json_schema is None:
                    return text
                try:
                    return json.dumps(parse_json_text(text), ensure_ascii=False, separators=(",", ":"))
                except ValueError:
                    last = LLMError(f"Gemini {self.model} returned invalid JSON", body=text[:2000])
            if attempt < attempts - 1:
                self._sleep(min(self.backoff_s * (2**attempt), self.max_backoff_s))
        assert last is not None
        raise LLMError(f"giving up after {attempts} attempt(s): {last}", status_code=last.status_code,
                       body=last.body)


# --------------------------------------------------------------------------- private Gemma tier


class _IdTokenAuth:
    """Fetches and caches a Google-signed ID token for a Cloud Run audience (refreshed every 45 minutes)."""

    def __init__(self, audience: str, fetch: Callable[[str], str] | None = None, ttl_s: float = 45 * 60) -> None:
        self.audience = audience
        self._fetch = fetch or self._default_fetch
        self.ttl_s = ttl_s
        self._token: str | None = None
        self._at = 0.0

    @staticmethod
    def _default_fetch(audience: str) -> str:
        import google.auth.transport.requests  # lazy
        import google.oauth2.id_token

        return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)

    def token(self) -> str:
        now = time.monotonic()
        if self._token is None or now - self._at > self.ttl_s:
            self._token = self._fetch(self.audience)
            self._at = now
        return self._token


class PrivateGemmaLLM(OpenAICompatLLM):
    """``OpenAICompatLLM`` that attaches a fresh Cloud Run ID token to every request."""

    def __init__(self, *args: Any, auth: _IdTokenAuth | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auth = auth

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._auth is not None:
            self._client.headers["Authorization"] = f"Bearer {self._auth.token()}"
        return super()._post(payload)


def gemma_private_llm(
    base_url: str,
    model: str = "google/gemma-4-E4B-it",
    *,
    api_key: str | None = None,
    id_token_audience: str | None = None,
    id_token_fetch: Callable[[str], str] | None = None,
    timeout: float = 120,
    **kwargs: Any,
) -> OpenAICompatLLM:
    """LLM for Gemma 4 served by vLLM on Cloud Run GPU (OpenAI-compatible ``/v1``).

    ``base_url`` may be the service URL with or without ``/v1``. ``id_token_audience`` (usually the service URL)
    enables IAM-authenticated calls from another Cloud Run service.
    """
    if not base_url:
        raise ValueError("base_url is required")
    root = base_url.rstrip("/")
    url = root if root.endswith("/v1") else f"{root}/v1"
    auth = _IdTokenAuth(id_token_audience, id_token_fetch) if id_token_audience else None
    return PrivateGemmaLLM(
        url, model, api_key=api_key if api_key is not None else "", timeout=timeout,
        name=kwargs.pop("name", f"gemma-private:{model}"), auth=auth, **kwargs,
    )
