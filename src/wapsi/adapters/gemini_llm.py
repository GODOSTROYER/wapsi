"""Gemini via the ``google-genai`` client (Gemini API key or Agent Platform), plus the private Gemma tier.

Two backends, chosen by the ``backend`` argument or ``WAPSI_GEMINI_BACKEND``:

- ``aistudio`` (default): the Gemini Developer API with an API key from Google AI Studio, read from
  ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``). This is the Google Cloud free-trial path: the Gemini API free tier
  is rate limited but costs nothing, and the free-trial credit could not pay for it anyway. The key is never
  logged, printed or put in ``repr``; error text is scrubbed of it.
- ``vertex``: Gemini on Agent Platform (Vertex AI) with Application Default Credentials, billed per token.

``GeminiLLM`` implements ``wapsi.ports.LLM`` (plus ``extract_media`` for voice intake: audio bytes inline and a
response schema, with the same retries):
- system messages become ``system_instruction``; ``assistant`` turns become ``model`` turns;
- ``json_schema`` switches on JSON mode (``response_mime_type="application/json"`` +
  ``response_json_schema``); the reply is parsed (code fences tolerated) and returned as compact JSON text;
- 408/429/5xx, timeouts, connection errors and unparseable JSON are retried with exponential backoff. A 429
  honours the server's ``RetryInfo`` delay; a 429 for a *daily* free-tier quota is not retried (it only resets at
  midnight Pacific) and raises a clear ``LLMError``; other errors raise ``LLMError`` at once (a safety block is
  reported with its reason). Request labels are sent only to Agent Platform (the Gemini API rejects them).

``gemma_private_llm`` returns an ``OpenAICompatLLM`` for Gemma 4 served by vLLM on Cloud Run GPU (the
"victim data stays in-region" tier, an optional paid add-on). When the service requires IAM, pass
``id_token_audience`` and a Google ID token is attached and refreshed before it expires.

Nothing here touches the network at import or construction time.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wapsi.adapters.llm_openai_compat import RETRY_STATUS, LLMError, OpenAICompatLLM

# Newest stable Flash model in the Gemini API docs (checked 2026-09-23); free of charge on the free tier.
DEFAULT_MODEL = "gemini-3.8-flash"
BACKENDS = ("aistudio", "vertex")
DEFAULT_BACKEND = "aistudio"
BACKEND_ENV = "WAPSI_GEMINI_BACKEND"
API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
RATE_LIMITS_URL = "https://aistudio.google.com/rate-limit"
MISSING_KEY_MESSAGE = (
    "Gemini backend 'aistudio' needs an API key: create one at https://aistudio.google.com/api-keys and set "
    "GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment; on Cloud Run, mount it from Secret Manager "
    "(see editions/gcp/README.md). To use Agent Platform with billing instead, set WAPSI_GEMINI_BACKEND=vertex."
)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_ROLE = {"user": "user", "assistant": "model", "model": "model"}


def parse_json_text(text: str) -> Any:
    """Parse model JSON output, tolerating a Markdown code fence around it."""
    m = _FENCE.match(text or "")
    return json.loads(m.group(1) if m else text)


def resolve_backend(backend: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """``backend`` if given, else ``WAPSI_GEMINI_BACKEND``, else ``aistudio``."""
    e = os.environ if env is None else env
    value = (backend or e.get(BACKEND_ENV) or DEFAULT_BACKEND).strip().lower()
    if value not in BACKENDS:
        raise ValueError(f"{BACKEND_ENV} must be one of {', '.join(BACKENDS)} (got {value!r})")
    return value


def gemini_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The Gemini API key from ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` (never log the return value)."""
    e = os.environ if env is None else env
    for name in API_KEY_ENVS:
        value = (e.get(name) or "").strip()
        if value:
            return value
    return None


def _status(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def _error_body(exc: BaseException) -> dict[str, Any]:
    details = getattr(exc, "details", None)  # google-genai APIError: the response JSON
    if isinstance(details, dict):
        inner = details.get("error")
        return inner if isinstance(inner, dict) else details
    return {}


def retry_delay_s(exc: BaseException) -> float | None:
    """The server's suggested wait from a ``google.rpc.RetryInfo`` detail (``"37s"``), if any."""
    for item in _error_body(exc).get("details") or []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", str(item.get("retryDelay") or ""))
            if m:
                return float(m.group(1))
    return None


def is_daily_quota(exc: BaseException) -> bool:
    """True when a 429 is for a per-day quota (retrying before the reset cannot succeed)."""
    body = _error_body(exc)
    if str(body.get("code", "")).lower() == "quota_exceeded":
        return True
    for item in body.get("details") or []:
        for v in (item.get("violations") or []) if isinstance(item, dict) else []:
            ident = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')}" if isinstance(v, dict) else ""
            if re.search(r"per_?day", ident, re.IGNORECASE):
                return True
    message = str(body.get("message") or "")
    return bool(re.search(r"per[ _-]?day|daily", message, re.IGNORECASE))


def quota_hint(exc: BaseException, model: str | None = None) -> str | None:
    """A plain-language explanation for a 429 (free-tier rate limit), or ``None`` for other errors."""
    if _status(exc) != 429:
        return None
    which = f" for {model}" if model else ""
    if is_daily_quota(exc):
        return (f"The Gemini API free-tier daily quota{which} is used up; it resets at midnight Pacific time. "
                f"Set WAPSI_MODEL to another free-tier model (for example gemini-3.5-flash-lite), try again "
                f"tomorrow, or switch to WAPSI_GEMINI_BACKEND=vertex with billing. Limits: {RATE_LIMITS_URL}")
    return (f"The Gemini API free tier is rate limited (429 RESOURCE_EXHAUSTED){which}: too many requests or "
            f"tokens this minute. Wait about a minute and try again. Limits: {RATE_LIMITS_URL}")


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret and len(secret) >= 8 else text


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
    """Implements ``wapsi.ports.LLM`` with Gemini: the Gemini API (API key) or Agent Platform (Vertex AI)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        project: str | None = None,
        location: str = "global",
        client: Any | None = None,
        *,
        backend: str | None = None,
        api_key: str | None = None,
        name: str | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        max_backoff_s: float = 20.0,
        max_retry_delay_s: float = 60.0,
        timeout_s: float = 60.0,
        thinking_budget: int | None = None,
        labels: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        self.model = model
        self.backend = resolve_backend(backend)
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("WAPSI_PROJECT")
        self.location = location or "global"
        self.name = name or f"{'gemini-api' if self.backend == 'aistudio' else 'vertex'}:{model}"
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_retry_delay_s = max_retry_delay_s
        self.timeout_s = timeout_s
        self.thinking_budget = thinking_budget
        self.labels = dict(labels or {"app": "wapsi"})
        self._api_key = api_key  # explicit key (tests, scripts); otherwise read from the env when first needed
        self._sleep = sleep
        self._client = client
        self.last_usage: Any = None

    def __repr__(self) -> str:  # never includes the API key
        if self.backend == "aistudio":
            return f"GeminiLLM(model={self.model!r}, backend='aistudio')"
        return (f"GeminiLLM(model={self.model!r}, backend='vertex', project={self.project!r}, "
                f"location={self.location!r})")

    def _key(self) -> str | None:
        return self._api_key or (gemini_api_key() if self.backend == "aistudio" else None)

    @property
    def client(self) -> Any:
        """The google-genai client, created on first use. ``aistudio`` without a key raises ``LLMError``."""
        if self._client is None:
            from google import genai  # lazy
            from google.genai import types

            http_options = types.HttpOptions(timeout=int(self.timeout_s * 1000))
            if self.backend == "aistudio":
                key = self._key()
                if not key:
                    raise LLMError(MISSING_KEY_MESSAGE)
                # explicit vertexai=False: GOOGLE_GENAI_USE_VERTEXAI / _ENTERPRISE in the env cannot flip it
                self._client = genai.Client(vertexai=False, api_key=key, http_options=http_options)
            else:
                self._client = genai.Client(vertexai=True, project=self.project, location=self.location,
                                            http_options=http_options)
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
        if self.labels and self.backend == "vertex":  # the Gemini API rejects request labels
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
        return self._generate(contents, config, json_schema)

    # ------------------------------------------------------------------ multimodal (voice intake)
    def build_media_request(
        self,
        data: bytes,
        mime_type: str,
        *,
        system: str,
        prompt: str,
        json_schema: dict | None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """One user turn: the media inline (bytes + MIME type) followed by the text prompt."""
        contents = [{"role": "user", "parts": [{"inline_data": {"mime_type": mime_type, "data": data}},
                                               {"text": prompt}]}]
        config: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_tokens, "candidate_count": 1}
        if system:
            config["system_instruction"] = system
        if json_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = json_schema
        if self.thinking_budget is not None:
            config["thinking_config"] = {"thinking_budget": self.thinking_budget}
        if self.labels and self.backend == "vertex":  # the Gemini API rejects request labels
            config["labels"] = {**self.labels, "step": "voice_intake"}
        return contents, config

    def extract_media(
        self,
        data: bytes,
        mime_type: str,
        *,
        system: str,
        prompt: str,
        json_schema: dict,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Audio / image / PDF bytes + prompt -> JSON text matching ``json_schema`` (``wapsi.intake.voice``).
        Same retries, quota handling and key redaction as ``complete``."""
        contents, config = self.build_media_request(data, mime_type, system=system, prompt=prompt,
                                                    json_schema=json_schema, temperature=temperature,
                                                    max_tokens=max_tokens)
        return self._generate(contents, config, json_schema)

    def _generate(self, contents: list[dict[str, Any]], config: dict[str, Any], json_schema: dict | None) -> str:
        client = self.client  # a missing API key fails here, once, with MISSING_KEY_MESSAGE
        secret = self._key()
        attempts = self.max_retries + 1
        last: LLMError | None = None
        last_exc: BaseException | None = None  # the SDK error behind ``last`` (keeps RetryInfo / quota details)
        hint: str | None = None
        for attempt in range(attempts):
            server_delay: float | None = None
            try:
                resp = client.models.generate_content(model=self.model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001 - classify below
                detail = _redact(f"{type(exc).__name__}: {exc}", secret)
                err = LLMError(f"Gemini {self.model} call failed: {detail}"[:500], status_code=_status(exc))
                hint = quota_hint(exc, self.model)
                if hint and is_daily_quota(exc):
                    raise LLMError(f"{hint} ({detail})"[:800], status_code=429) from exc
                if not _retryable(exc):
                    raise err from exc
                server_delay = retry_delay_s(exc)
                last, last_exc = err, exc
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
                    last, last_exc = LLMError(f"Gemini {self.model} returned invalid JSON", body=text[:2000]), None
            if attempt < attempts - 1:
                delay = min(self.backoff_s * (2**attempt), self.max_backoff_s)
                if server_delay is not None:  # the free tier says how long until the next minute window
                    delay = max(delay, min(server_delay, self.max_retry_delay_s))
                self._sleep(delay)
        assert last is not None
        prefix = f"{hint} " if hint and last.status_code == 429 else ""
        # chained to the SDK error so a caller that owns the retry policy (llm_resilience.ResilientLLM, which runs
        # this adapter with max_retries=0) still sees the server's RetryInfo delay; the message alone is truncated
        raise LLMError(f"{prefix}giving up after {attempts} attempt(s): {last}", status_code=last.status_code,
                       body=last.body) from last_exc


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
