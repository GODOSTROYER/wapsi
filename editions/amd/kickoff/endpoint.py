"""Metered client for a shared, rate-limited OpenAI-compatible endpoint: AMD Token Factory, or anyone's vLLM.

Endpoint mode of the kickoff pipeline (``run_all.sh --mode endpoint``) has no GPU box: everything LLM-side runs
against ``WAPSI_LLM_BASE_URL`` with ``WAPSI_LLM_API_KEY`` and ``WAPSI_LLM_MODEL``. The GPU is on the provider's side
and is not visible to the client, so the evidence records the endpoint host and the model id, never a GPU name.

    uv run python -m editions.amd.kickoff.endpoint check            # preflight rows (TSV): config, /models, /usage
    uv run python -m editions.amd.kickoff.endpoint wait-model       # poll GET /models until the model is served
    uv run python -m editions.amd.kickoff.endpoint probe --out editions/amd/evidence/endpoint/<date>/endpoint.json

What the client does that the plain ``OpenAICompatLLM`` does not (all of it needed on a trial endpoint that allows
20 requests a minute and 8 at once for the whole team):

- **Rate limit.** ``RateLimiter``: at most ``WAPSI_LLM_RPM`` request starts in any 60 s and ``WAPSI_LLM_MAX_CONCURRENCY``
  in flight, shared by every client of the same host in this process. An HTTP 429 pauses every thread for its
  ``Retry-After`` and is retried (it is the provider's queue, not a model error); 502 / 503 are retried with
  exponential backoff and jitter; any other 4xx raises at once. Errors arrive as ``{"error": {...}}`` or
  ``{"detail": {"error": {...}}}``: both are read.
- **JSON without a strict schema.** ``json_mode="json_object"`` (the default here): the endpoint accepts
  ``response_format: {"type": "json_object"}`` but not a strict JSON schema, so the schema is enforced by Wapsi's own
  parser and checks, as everywhere else.
- **One request shape.** System text is merged into a single first message (some served models reject anything else).
- **Disk cache.** Temperature-0 requests are cached under ``WAPSI_LLM_CACHE_DIR`` (default
  ``data/generated/llm-cache``, gitignored) by a hash of the endpoint, model and request, so a re-run costs nothing
  and returns the same text (the endpoint ignores ``seed``). The hash leaves out one field of a drafting request:
  the case's ``opened_at``, the wall-clock moment this run opened the case, which is the only thing that differs
  between two runs of the same case (``CACHE_KEY_MASK``). Benchmarks switch the cache off.
- **Metering.** Every call records latency, tokens and the ``usage.cost`` the API reports. ``report()`` is the
  ``endpoint`` block of the result files; ``WAPSI_LLM_METER_OUT=<file>`` also writes it at exit.

The key is read from ``WAPSI_LLM_API_KEY`` (else ``RADEON_API_KEY``, else ``VLLM_API_KEY``). It is sent only as the
``Authorization`` header and never appears in a report, a cache file, a log line or ``repr()``.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from editions.amd.bench.common import dist  # noqa: E402  (standard library only)

SCHEMA = "wapsi-amd-endpoint/1"
KEY_ENVS = ("WAPSI_LLM_API_KEY", "RADEON_API_KEY", "VLLM_API_KEY")
DEFAULT_MODEL = "Gemma-4-31B-IT"
DEFAULT_CACHE_DIR = ROOT / "data" / "generated" / "llm-cache"
WINDOW_S = 60.0
RETRY_STATUS = frozenset({429, 502, 503})
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
# shared-trial defaults: the account allows 20 requests/min and 8 in flight for the whole team
TOKEN_FACTORY_RPM = 16
TOKEN_FACTORY_CONCURRENCY = 4
AMD_HARDWARE_RE = re.compile(r"\b(AMD|Instinct|Radeon|MI3\d\d[A-Z]?|ROCm)\b", re.I)
# the one run-dependent field of a drafting request (wapsi.draft.factsheet: when this run opened the case); it is
# masked in the cache key only, never in what is sent
CACHE_KEY_MASK = re.compile(r'"opened_at": "[^"]*"')


class EndpointError(RuntimeError):
    """A request the endpoint refused or that kept failing after the retries."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# --------------------------------------------------------------------------- configuration


def api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The bearer token: ``WAPSI_LLM_API_KEY``, else ``RADEON_API_KEY`` (Token Factory), else ``VLLM_API_KEY``."""
    env = os.environ if env is None else env
    for name in KEY_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def host_of(base_url: str) -> str:
    """``https://developer.amd.com.cn/radeon/api/v1`` -> ``developer.amd.com.cn`` (with ``:port`` when not default)."""
    parts = urlsplit(base_url if "://" in base_url else f"//{base_url}")
    host = (parts.hostname or "").lower()
    return f"{host}:{parts.port}" if parts.port and parts.port not in (80, 443) else host


def is_local(base_url: str) -> bool:
    return host_of(base_url).split(":")[0] in LOOPBACK


def provider_of(base_url: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Who serves this endpoint and what may be said about its hardware.

    - ``amd-token-factory``: a host under ``amd.com`` / ``amd.com.cn``. AMD documents Token Factory as the model API
      of AMD Radeon Cloud, running on AMD GPUs; the GPU model is not visible to a client.
    - ``self-hosted``: a loopback address. The hardware is this machine's (see the amd-smi identity of the run).
    - ``other``: any other host. AMD-served only if the operator declares it (``WAPSI_ENDPOINT_HARDWARE``, recorded
      as declared, never as measured).
    """
    env = os.environ if env is None else env
    host = host_of(base_url)
    name = host.split(":")[0]
    declared = (env.get("WAPSI_ENDPOINT_HARDWARE") or "").strip() or None
    if name in LOOPBACK:
        return {"kind": "self-hosted", "name": "self-hosted OpenAI-compatible server on this machine",
                "gpu_side": "local", "amd_served": None, "hardware_declared": declared,
                "basis": "the GPU identity is this machine's (amd-smi / rocm-smi / torch)"}
    if name in ("amd.com", "amd.com.cn") or name.endswith((".amd.com", ".amd.com.cn")):
        return {"kind": "amd-token-factory", "name": "AMD Token Factory (AMD Radeon Cloud model API)",
                "gpu_side": "provider", "amd_served": True, "hardware_declared": declared,
                "basis": "AMD documents Token Factory as model endpoints running on AMD GPUs; the GPU model is not "
                         "visible to an API client, so none is recorded"}
    return {"kind": "other", "name": f"OpenAI-compatible endpoint {host}", "gpu_side": "provider",
            "amd_served": bool(declared and AMD_HARDWARE_RE.search(declared)), "hardware_declared": declared,
            "basis": ("declared by the operator in WAPSI_ENDPOINT_HARDWARE, not measured" if declared
                      else "hardware unknown: set WAPSI_ENDPOINT_HARDWARE to what serves this endpoint")}


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def default_limits(base_url: str, env: Mapping[str, str] | None = None) -> tuple[float, int, float]:
    """(requests per minute, concurrent requests, minimum seconds between starts) for this endpoint.

    ``WAPSI_LLM_RPM`` / ``WAPSI_LLM_MAX_CONCURRENCY`` / ``WAPSI_LLM_MIN_INTERVAL_S`` win. Otherwise a Token Factory
    host gets the shared-trial defaults (16 a minute, 4 at once: below the account's 20 and 8) and any other
    endpoint (your own vLLM) is not limited (0 = no limit)."""
    env = os.environ if env is None else env
    shared = provider_of(base_url, env)["kind"] == "amd-token-factory"
    rpm = _env_float(env, "WAPSI_LLM_RPM", TOKEN_FACTORY_RPM if shared else 0.0)
    conc = int(_env_float(env, "WAPSI_LLM_MAX_CONCURRENCY", TOKEN_FACTORY_CONCURRENCY if shared else 64))
    return max(0.0, rpm), max(1, conc), max(0.0, _env_float(env, "WAPSI_LLM_MIN_INTERVAL_S", 0.0))


def default_json_mode(base_url: str, env: Mapping[str, str] | None = None) -> str:
    """``WAPSI_LLM_JSON_MODE``, else ``json_schema`` for your own server (vLLM guided decoding) and ``json_object``
    for a provider's endpoint (Token Factory serves no strict JSON schema)."""
    env = os.environ if env is None else env
    mode = (env.get("WAPSI_LLM_JSON_MODE") or "").strip()
    return mode or ("json_schema" if is_local(base_url) else "json_object")


def cache_dir_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    raw = env.get("WAPSI_LLM_CACHE_DIR")
    if raw is None:
        return DEFAULT_CACHE_DIR
    raw = raw.strip()
    return None if raw.lower() in ("", "0", "off", "none", "false") else Path(raw)


# --------------------------------------------------------------------------- rate limiter


class RateLimiter:
    """At most ``rpm`` request starts in any 60 s window and ``max_concurrency`` requests in flight (thread-safe).

    ``rpm = 0`` switches the window off. ``penalize(seconds)`` holds every new start (an HTTP 429's ``Retry-After``).
    ``room_wait(n)`` is how long until ``n`` requests may start at once (the benchmark waits that long before a
    concurrency level, so the level runs as one burst instead of being paced)."""

    def __init__(self, rpm: float = 0.0, max_concurrency: int = 64, min_interval_s: float = 0.0, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.rpm = max(0, int(rpm))
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock, self._sleep = clock, sleep
        self._starts: deque[float] = deque()
        self._not_before = 0.0
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        self.waits = 0
        self.waited_s = 0.0
        self.started = 0

    def _trim(self, now: float) -> None:
        while self._starts and now - self._starts[0] >= WINDOW_S:
            self._starts.popleft()

    def _wait(self, now: float, n: int = 1) -> float:
        self._trim(now)
        wait = self._not_before - now
        if self.rpm:
            over = len(self._starts) + min(n, self.rpm) - self.rpm
            if over > 0:  # the over-th oldest start has to leave the window first
                wait = max(wait, self._starts[over - 1] + WINDOW_S - now)
        if self.min_interval_s and self._starts:
            wait = max(wait, self._starts[-1] + self.min_interval_s - now)
        return max(0.0, wait)

    def room_wait(self, n: int = 1) -> float:
        """Seconds until ``n`` requests can start together (0 when they can now). Reserves nothing."""
        with self._lock:
            return self._wait(self._clock(), n)

    def try_start(self) -> float:
        """Register a start if one is allowed now and return 0.0, else the seconds to wait (async callers)."""
        with self._lock:
            now = self._clock()
            wait = self._wait(now)
            if wait <= 0:
                self._starts.append(now)
                self.started += 1
                return 0.0
            self.waits += 1
            self.waited_s += wait
            return wait

    def acquire(self) -> None:
        self._sem.acquire()
        try:
            while True:
                wait = self.try_start()
                if wait <= 0:
                    return
                self._sleep(wait)
        except BaseException:
            self._sem.release()
            raise

    def release(self) -> None:
        self._sem.release()

    @contextmanager
    def slot(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._not_before = max(self._not_before, self._clock() + max(0.0, seconds))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"rpm": self.rpm or None, "max_concurrency": self.max_concurrency,
                    "min_interval_s": self.min_interval_s or None, "starts": self.started, "waits": self.waits,
                    "waited_s": round(self.waited_s, 3)}


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def shared_limiter(base_url: str, env: Mapping[str, str] | None = None) -> RateLimiter:
    """One limiter per endpoint host in this process (the drafting model and the judge share the account's limit)."""
    host = host_of(base_url)
    with _LIMITERS_LOCK:
        if host not in _LIMITERS:
            rpm, conc, gap = default_limits(base_url, env)
            _LIMITERS[host] = RateLimiter(rpm, conc, gap)
        return _LIMITERS[host]


# --------------------------------------------------------------------------- responses


def error_detail(resp: httpx.Response) -> str:
    """The message of an error body: ``{"error": {...}}``, ``{"detail": {"error": {...}}}`` or plain text."""
    try:
        data: Any = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:300].strip()
    for _ in range(3):  # unwrap {"detail": ...} / {"error": ...} layers
        if isinstance(data, dict) and isinstance(data.get("detail"), dict | str):
            data = data["detail"]
        elif isinstance(data, dict) and "error" in data:
            data = data["error"]
        else:
            break
    if isinstance(data, dict):
        return str(data.get("message") or data.get("code") or data)[:300]
    return str(data)[:300]


def retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def ratelimit_headers(resp: httpx.Response) -> dict[str, float]:
    """``x-ratelimit-*`` numbers of a response (limit / remaining requests a minute, daily relative cost)."""
    names = {"x-ratelimit-limit-user-rpm": "limit_rpm", "x-ratelimit-remaining-user-rpm": "remaining_rpm",
             "x-ratelimit-limit-user-daily-usd": "limit_daily_usd",
             "x-ratelimit-remaining-user-daily-usd": "remaining_daily_usd",
             "x-ratelimit-used-user-daily-usd": "used_daily_usd"}
    out: dict[str, float] = {}
    for header, key in names.items():
        try:
            out[key] = float(resp.headers[header])
        except (KeyError, ValueError):
            continue
    return out


def normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One system message, first (served chat templates reject a later or a second one); text content only."""
    system = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
    rest = [{"role": str(m["role"]), "content": str(m.get("content") or "")} for m in messages
            if m.get("role") != "system"]
    return ([{"role": "system", "content": "\n\n".join(s for s in system if s)}] if any(system) else []) + rest


def content_of(data: Mapping[str, Any]) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EndpointError("malformed response: no choices[0].message") from exc
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content:
        finish = data["choices"][0].get("finish_reason") if isinstance(data["choices"][0], dict) else None
        raise EndpointError(f"model returned no content (finish_reason={finish})")
    return str(content)


def usage_of(data: Mapping[str, Any]) -> dict[str, Any]:
    """Tokens and the API-reported cost of one response (``cost`` is ``None`` when the server reports none)."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def count(key: str) -> int:
        value = usage.get(key)
        return max(0, int(value)) if isinstance(value, int | float) and not isinstance(value, bool) else 0

    cost = usage.get("cost")
    return {"prompt_tokens": count("prompt_tokens"), "completion_tokens": count("completion_tokens"),
            "cost": float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None}


# --------------------------------------------------------------------------- the client


class MeteredLLM:
    """``wapsi.ports.LLM`` over an OpenAI-compatible endpoint with a rate limit, retries, a cache and a meter."""

    def __init__(self, base_url: str, model: str, *, api_key: str | None = None, json_mode: str = "json_object",
                 timeout: float = 180.0, max_retries: int = 6, limiter: RateLimiter | None = None,
                 cache_dir: Path | str | None = None, name: str | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 jitter: float = 0.25, max_backoff_s: float = 65.0, extra_body: Mapping[str, Any] | None = None,
                 max_tokens_headroom: int = 0, env: Mapping[str, str] | None = None) -> None:
        if not base_url:
            raise ValueError("base_url is required (WAPSI_LLM_BASE_URL)")
        if not model:
            raise ValueError("model is required (WAPSI_LLM_MODEL)")
        if json_mode not in ("json_object", "json_schema"):
            raise ValueError("json_mode must be 'json_object' or 'json_schema'")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.host = host_of(self.base_url)
        self.name = name or f"{model}@{self.host}"
        self.json_mode = json_mode
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.max_backoff_s = max_backoff_s
        self.jitter = max(0.0, jitter)
        self.limiter = limiter or RateLimiter()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.extra_body = {k: v for k, v in dict(extra_body or {}).items() if k not in ("model", "messages", "stream")}
        self.max_tokens_headroom = max(0, int(max_tokens_headroom))  # for a model whose thinking counts as output
        self.provider = provider_of(self.base_url, env)
        self._sleep = sleep
        self._rng = random.Random(0)
        self._lock = threading.Lock()
        self._calls: list[dict[str, Any]] = []
        self._errors = 0
        self._rate_limited = 0
        self._retries = 0
        self._ratelimit: dict[str, float] = {}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        self._has_key = bool(api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    @classmethod
    def from_env(cls, *, base_url: str | None = None, model: str | None = None, cache: bool = True,
                 env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None,
                 limiter: RateLimiter | None = None, name: str | None = None, **kwargs: Any) -> MeteredLLM:
        """The client the environment describes (``WAPSI_LLM_BASE_URL`` / ``_API_KEY`` / ``_MODEL`` / ``_JSON_MODE``
        / ``_RPM`` / ``_MAX_CONCURRENCY`` / ``_TIMEOUT_S`` / ``_CACHE_DIR``). ``cache=False`` for benchmarks.
        ``WAPSI_LLM_EXTRA_BODY`` (a JSON object) adds request fields a model needs, e.g.
        ``{"reasoning_effort": "low"}`` for a fallback model that thinks by default (Gemma 4 does not)."""
        from editions.amd.kickoff.model_plan import request_defaults  # noqa: PLC0415  (standard library only)

        env = os.environ if env is None else env
        chosen = model or (env.get("WAPSI_LLM_MODEL") or "").strip() or DEFAULT_MODEL
        # what the served model needs (a fallback model that thinks by default: reasoning_effort low + headroom),
        # only on a provider's endpoint; the environment overrides it
        body, headroom = ({}, 0) if is_local(base_url or env.get("WAPSI_LLM_BASE_URL") or "") else request_defaults(chosen)
        if "extra_body" not in kwargs and (env.get("WAPSI_LLM_EXTRA_BODY") or "").strip():
            try:
                kwargs["extra_body"] = json.loads(env["WAPSI_LLM_EXTRA_BODY"])
            except ValueError as exc:
                raise SystemExit(f"WAPSI_LLM_EXTRA_BODY must be a JSON object: {exc}") from exc
            if not isinstance(kwargs["extra_body"], dict):
                raise SystemExit("WAPSI_LLM_EXTRA_BODY must be a JSON object")
        kwargs.setdefault("extra_body", body)
        kwargs.setdefault("max_tokens_headroom", int(_env_float(env, "WAPSI_LLM_MAX_TOKENS_HEADROOM", headroom)))
        url = base_url or (env.get("WAPSI_LLM_BASE_URL") or "").strip()
        if not url:
            raise SystemExit("WAPSI_LLM_BASE_URL is not set (endpoint mode needs the endpoint's base URL, e.g. "
                             "https://developer.amd.com.cn/radeon/api/v1)")
        json_mode = default_json_mode(url, env)
        llm = cls(url, chosen, api_key=api_key(env),
                  json_mode=json_mode, timeout=_env_float(env, "WAPSI_LLM_TIMEOUT_S", 180.0),
                  limiter=limiter or (shared_limiter(url, env) if transport is None else None),
                  cache_dir=cache_dir_from_env(env) if cache else None, name=name, transport=transport, env=env,
                  **kwargs)
        meter_out = (env.get("WAPSI_LLM_METER_OUT") or "").strip()
        if meter_out:
            atexit.register(llm.write_report, meter_out)
        return llm

    def __repr__(self) -> str:  # never show the key
        return f"MeteredLLM(base_url={self.base_url!r}, model={self.model!r}, api_key={'***' if self._has_key else None})"

    def close(self) -> None:
        self._client.close()

    # -- LLM protocol
    def complete(self, messages: Sequence[Mapping[str, Any]], *, temperature: float = 0.0, max_tokens: int = 1024,
                 json_schema: dict | None = None, extra_body: Mapping[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {**self.extra_body, **dict(extra_body or {})}
        payload.update(model=self.model, messages=normalize_messages(messages), temperature=temperature,
                       max_tokens=max_tokens + self.max_tokens_headroom)
        if json_schema is not None:
            payload["response_format"] = self._response_format(json_schema)
        key = self._cache_key(payload) if (self.cache_dir is not None and temperature == 0) else None
        if key:
            hit = self._cache_get(key)
            if hit is not None:
                with self._lock:
                    self._calls.append({"cached": True, "prompt_tokens": hit.get("prompt_tokens", 0),
                                        "completion_tokens": hit.get("completion_tokens", 0)})
                return str(hit["content"])
        t_total = time.perf_counter()
        try:
            with self.limiter.slot():
                data, latency, retries = self._post(payload)
            text = content_of(data)
        except Exception:
            with self._lock:
                self._errors += 1
            raise
        usage = usage_of(data)
        record = {"cached": False, "latency_s": round(latency, 4), "total_s": round(time.perf_counter() - t_total, 4),
                  "retries": retries, **usage}
        with self._lock:
            self._calls.append(record)
        if key:
            self._cache_put(key, {"content": text, **usage, "latency_s": record["latency_s"]})
        return text

    # -- internals
    def _response_format(self, schema: dict) -> dict[str, Any]:
        if self.json_mode == "json_object":
            return {"type": "json_object"}
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(schema.get("title") or "response"))[:64]
        return {"type": "json_schema", "json_schema": {"name": name, "schema": schema,
                                                       "strict": schema.get("additionalProperties") is False}}

    def _backoff(self, attempt: int, hinted: float | None) -> float:
        base = hinted if hinted is not None else min(2.0 ** attempt, self.max_backoff_s)
        return min(base, self.max_backoff_s) + self._rng.uniform(0.0, self.jitter)

    def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float, int]:
        """(response JSON, seconds of the attempt that succeeded, retries). 429 / 502 / 503 and connection errors
        are retried; a read timeout once; anything else raises."""
        url = f"{self.base_url}/chat/completions"
        last: EndpointError | None = None
        timeouts = 0
        for attempt in range(self.max_retries + 1):
            hinted = None
            t = time.perf_counter()
            try:
                resp = self._client.post(url, json=payload)
            except httpx.TimeoutException as exc:
                timeouts += 1
                last = EndpointError(f"timed out after {self.timeout}s calling {self.host}: {type(exc).__name__}")
                if timeouts > 1:
                    break
            except httpx.TransportError as exc:
                last = EndpointError(f"connection error calling {self.host}: {type(exc).__name__}: {exc}"[:300])
            else:
                latency = time.perf_counter() - t
                code = resp.status_code
                seen = ratelimit_headers(resp)
                if seen:
                    with self._lock:
                        self._ratelimit = seen
                if code < 400:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise EndpointError(f"{self.host} returned a non-JSON body", status_code=code,
                                            body=resp.text[:1000]) from exc
                    if not isinstance(data, dict):
                        raise EndpointError(f"{self.host} returned unexpected JSON", status_code=code)
                    if seen.get("remaining_rpm") == 0:  # the team's minute is used up: let it refill
                        self.limiter.penalize(5.0)
                    return data, latency, attempt
                hint = " (check WAPSI_LLM_API_KEY)" if code in (401, 403) else ""
                err = EndpointError(f"{self.host} returned HTTP {code}: {error_detail(resp)}{hint}", status_code=code,
                                    body=resp.text[:1000])
                if code not in RETRY_STATUS:
                    raise err
                last = err
                hinted = retry_after(resp)
                if code == 429:
                    with self._lock:
                        self._rate_limited += 1
                    self.limiter.penalize(hinted if hinted is not None else 5.0)
            if attempt < self.max_retries:
                with self._lock:
                    self._retries += 1
                self._sleep(self._backoff(attempt, hinted))
        assert last is not None
        raise EndpointError(f"giving up after {self.max_retries + 1} attempt(s): {last}",
                            status_code=last.status_code, body=last.body)

    def _cache_key(self, payload: dict[str, Any]) -> str:
        stable = {**payload, "messages": [{**m, "content": CACHE_KEY_MASK.sub('"opened_at": "(run time)"', m["content"])}
                                          for m in payload["messages"]]}
        blob = json.dumps({"base_url": self.base_url, "payload": stable}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        assert self.cache_dir is not None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{self.host}-{self.model}").strip("-")
        return self.cache_dir / slug / key[:2] / f"{key}.json"

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._cache_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("content"), str) and data["content"] else None

    def _cache_put(self, key: str, entry: dict[str, Any]) -> None:
        path = self._cache_path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({**entry, "model": self.model, "cached_at": _iso_now()}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # the cache is a convenience: never fail a request over it

    # -- meter
    def mark(self) -> int:
        """Position in the call log (``report(since=mark)`` meters one phase of a run)."""
        with self._lock:
            return len(self._calls)

    def report(self, since: int = 0) -> dict[str, Any]:
        """The ``endpoint`` block of a result file: who served the calls, how many, tokens, cost, latency."""
        with self._lock:
            calls = list(self._calls[since:])
            errors, limited, retries, headers = self._errors, self._rate_limited, self._retries, dict(self._ratelimit)
        live = [c for c in calls if not c.get("cached")]
        costs = [c["cost"] for c in live if c.get("cost") is not None]
        rates = [c["completion_tokens"] / c["latency_s"] for c in live
                 if c.get("completion_tokens") and c.get("latency_s")]
        return {
            "endpoint_host": self.host,
            "base_url": self.base_url,
            "model": self.model,
            "provider": self.provider,
            "json_mode": self.json_mode,
            "extra_body": self.extra_body or None,
            "max_tokens_headroom": self.max_tokens_headroom or None,
            "calls": len(live),
            "cache_hits": len(calls) - len(live),
            "errors": errors,
            "rate_limited_429": limited,
            "retries": retries,
            "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in live),
            "completion_tokens": sum(c.get("completion_tokens") or 0 for c in live),
            "cost_usd": round(sum(costs), 8) if costs else None,
            "cost_reported_calls": len(costs),
            "cost_note": "usage.cost as reported by the API (relative usage cost; the trial is not billed)",
            "latency_s": dist(c["latency_s"] for c in live),
            "completion_tokens_per_s": dist(rates, digits=2),
            "limiter": self.limiter.stats(),
            "ratelimit_last_seen": headers or None,
        }

    def write_report(self, path: str | Path) -> None:
        try:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"schema": SCHEMA, "generated_at": _iso_now(), **self.report()}, indent=2)
                           + "\n", encoding="utf-8", newline="\n")
        except OSError:
            pass


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def measured_label(base_url: str, model: str, date: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """How a result measured through this endpoint is labelled, e.g.
    ``AMD Token Factory, Gemma-4-31B-IT, measured 2026-10-14 (provider-side AMD GPUs; not an MI300X droplet
    benchmark)``."""
    date = date or datetime.now(UTC).strftime("%Y-%m-%d")
    prov = provider_of(base_url, env)
    if prov["kind"] == "amd-token-factory":
        return (f"AMD Token Factory, {model}, measured {date} (provider-side AMD GPUs through the shared trial API; "
                "not an MI300X droplet benchmark)")
    if prov["kind"] == "self-hosted":
        return f"self-hosted endpoint {host_of(base_url)}, {model}, measured {date}"
    hw = f"; hardware as declared by the operator: {prov['hardware_declared']}" if prov["hardware_declared"] else \
        "; hardware not verified"
    return f"endpoint {host_of(base_url)}, {model}, measured {date}{hw}"


# --------------------------------------------------------------------------- probe and preflight rows


def _get_json(client: httpx.Client, url: str) -> tuple[int | None, Any]:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def list_models(client: httpx.Client, base_url: str) -> tuple[int | None, list[dict[str, Any]], str | None]:
    """(HTTP status, model cards, error text) of ``GET {base}/models``."""
    code, body = _get_json(client, f"{base_url.rstrip('/')}/models")
    if code != 200:
        return code, [], str(body.get("error", body) if isinstance(body, dict) else body)[:200]
    cards = body.get("data") if isinstance(body, dict) else body
    return code, [c for c in cards or [] if isinstance(c, dict) and c.get("id")], None


def card_summary(card: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The fields of a model card the evidence keeps (Token Factory and vLLM name them differently)."""
    if not card:
        return None
    pricing = card.get("pricing") if isinstance(card.get("pricing"), dict) else {}

    def price(key: str) -> float | None:
        try:
            return float(pricing[key])
        except (KeyError, TypeError, ValueError):
            return None

    prov = (card.get("providers") or [{}])[0] if isinstance(card.get("providers"), list) else {}
    return {"id": card.get("id"), "name": card.get("name"), "root": card.get("root"),
            "context_length": card.get("context_length") or card.get("max_model_len"),
            "price_prompt_usd_per_token": price("prompt"), "price_completion_usd_per_token": price("completion"),
            "json_output": card.get("json_output"), "structured_outputs": card.get("structured_outputs"),
            "tools": prov.get("tools") if isinstance(prov, dict) else None,
            "input_modalities": (card.get("architecture") or {}).get("input_modalities")
            if isinstance(card.get("architecture"), dict) else None}


def usage_limits(client: httpx.Client, base_url: str) -> dict[str, Any] | None:
    """Limits and today's spend from ``GET {base}/usage`` (Token Factory); ``None`` when the endpoint has none.
    Account identifiers in the reply are not kept."""
    code, body = _get_json(client, f"{base_url.rstrip('/')}/usage")
    if code != 200 or not isinstance(body, dict):
        return None
    keep = ("rpm_limit", "daily_cost_limit_usd", "daily_cost_used_usd", "daily_cost_remaining_usd", "daily_reset_at")
    out = {k: body.get(k) for k in keep if k in body}
    return out or None


def _auth_client(key: str | None, transport: httpx.BaseTransport | None, timeout: float = 20.0) -> httpx.Client:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.Client(timeout=timeout, headers=headers, transport=transport)


def probe(*, base_url: str | None = None, model: str | None = None, env: Mapping[str, str] | None = None,
          transport: httpx.BaseTransport | None = None, chat: bool = True) -> dict[str, Any]:
    """Identify the endpoint for the evidence bundle: host, provider, the model's card, the limits, and one short
    ``json_object`` completion (latency, tokens, API-reported cost). Never raises: a failure is recorded."""
    from editions.amd.kickoff.model_plan import plan_endpoint  # noqa: PLC0415  (standard library only)

    env = dict(os.environ if env is None else env)
    if model:
        env["WAPSI_LLM_MODEL"] = model
    url = (base_url or env.get("WAPSI_LLM_BASE_URL") or "").strip().rstrip("/")
    requested = (env.get("WAPSI_LLM_MODEL") or "").strip()
    wanted = requested or DEFAULT_MODEL
    out: dict[str, Any] = {"schema": SCHEMA, "probed_at": _iso_now(), "base_url": url or None,
                           "endpoint_host": host_of(url) if url else None, "model": wanted,
                           "model_requested": requested or None,
                           "api_key_set": bool(api_key(env)),
                           "json_mode": default_json_mode(url, env) if url else None}
    if not url:
        out.update(ok=False, error="WAPSI_LLM_BASE_URL is not set")
        return out
    out["provider"] = provider_of(url, env)
    out["gpu"] = {"side": out["provider"]["gpu_side"],
                  "note": "provider-side: the client sees the endpoint host and the model id, not the GPU"
                  if out["provider"]["gpu_side"] == "provider" else "this machine's GPU (see hardware)"}
    with _auth_client(api_key(env), transport) as client:
        code, cards, err = list_models(client, url)
        out["models_status"] = code
        out["models"] = [str(c["id"]) for c in cards]
        out["limits"] = usage_limits(client, url)
    if err:
        out.update(ok=False, error=f"GET /models: HTTP {code}: {err}" if code else f"GET /models: {err}")
        return out
    # The model plan decides: the requested (or default) model when it is served, a fallback only when the operator
    # allows one or named none. A model that is not listed is never called. A model that is listed but refuses the
    # call (HTTP 400 "Unsupported model": the catalogue was changing, seen live on 2026-10-14) counts as not served,
    # and the plan is asked again without it.
    refused: list[str] = []
    out["models_refused"] = refused
    for _ in range(4):
        plan = plan_endpoint([m for m in out["models"] if m not in refused], env)
        chosen = plan["drafting"]["id"]
        out["model_fallback"] = bool(plan.get("fallback_in_use")) or bool(chosen and refused)
        card = next((c for c in cards if c.get("id") == chosen), None)
        out["model_served"] = bool(chosen) and card is not None
        out["model_card"] = card_summary(card)
        if not chosen:
            why = "; ".join(plan["warnings"][:1]) or f"model {wanted!r} is not served"
            if refused:
                why = (f"{', '.join(refused)} is listed by GET /models but refuses calls right now (HTTP 400): the "
                       f"catalogue is changing. {why}")
            out.update(ok=False, error=f"{why}; available: {', '.join(out['models'][:20])}")
            return out
        out["model"] = chosen
        if not chat:
            out["ok"] = True
            return out
        answer = _probe_call(url, chosen, env, transport)
        out["probe"] = answer
        if answer["ok"]:
            out["ok"] = True
            out.pop("error", None)
            return out
        out.update(ok=False, error=answer["error"])
        if not answer.get("model_refused"):
            return out
        refused.append(chosen)
    return out


MODEL_REFUSED_RE = re.compile(r"unsupported model|model\b.{0,60}\bnot (supported|found|available)|unknown model", re.I)


def _probe_call(url: str, model: str, env: Mapping[str, str],
                transport: httpx.BaseTransport | None) -> dict[str, Any]:
    """One short ``json_object`` completion: latency, tokens, API-reported cost; or why it failed."""
    llm = MeteredLLM.from_env(base_url=url, model=model, cache=False, env=env, transport=transport, max_retries=3)
    try:
        text = llm.complete(
            [{"role": "system", "content": "You answer with JSON only."},
             {"role": "user", "content": 'Reply with exactly this JSON: {"ok": true}'}],
            max_tokens=32, json_schema={"type": "object"})
        try:
            parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except ValueError:
            parsed = None
        rep = llm.report()
        return {"ok": True, "model": model, "json_object_ok": isinstance(parsed, dict),
                "latency_s": rep["latency_s"]["mean"], "prompt_tokens": rep["prompt_tokens"],
                "completion_tokens": rep["completion_tokens"], "cost_usd": rep["cost_usd"],
                "rate_limited_429": rep["rate_limited_429"], "ratelimit_last_seen": rep["ratelimit_last_seen"]}
    except Exception as exc:  # noqa: BLE001 - the probe records the failure instead of raising
        refused = isinstance(exc, EndpointError) and exc.status_code in (400, 404) and bool(MODEL_REFUSED_RE.search(str(exc)))
        return {"ok": False, "model": model, "model_refused": refused, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        llm.close()


def _say(text: str) -> None:
    print(text, flush=True)


def wait_model(*, model: str | None = None, timeout_s: float = 1800.0, interval_s: float = 30.0, call: bool = False,
               env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               say: Callable[[str], None] = _say) -> bool:
    """Poll ``GET /models`` until ``model`` (default ``WAPSI_LLM_MODEL``, else Gemma-4-31B-IT) is served. The
    catalogue of a shared endpoint changes while you work; polling makes no completion and costs nothing.
    ``call=True`` also asks the model for one short answer once it is listed (a listed model may still refuse
    calls for a while), so "served" means it answers."""
    env = os.environ if env is None else env
    url = (env.get("WAPSI_LLM_BASE_URL") or "").strip().rstrip("/")
    wanted = (model or env.get("WAPSI_LLM_MODEL") or "").strip() or DEFAULT_MODEL
    if not url:
        say("WAPSI_LLM_BASE_URL is not set")
        return False
    deadline = clock() + timeout_s
    with _auth_client(api_key(env), transport) as client:
        while True:
            code, cards, err = list_models(client, url)
            ids = [str(c["id"]) for c in cards]
            listed = next((i for i in ids if i.lower() == wanted.lower()), None)
            if listed and call:
                answer = _probe_call(url, listed, env, transport)
                if answer["ok"]:
                    say(f"{_iso_now()} {listed} is served by {host_of(url)} and answers ({answer['latency_s']} s)")
                    return True
                say(f"{_iso_now()} {listed} is listed but does not answer yet: {answer['error']}")
            elif listed:
                say(f"{_iso_now()} {listed} is served by {host_of(url)}")
                return True
            else:
                say(f"{_iso_now()} {wanted} is not served "
                    f"({'HTTP ' + str(code) + ': ' + str(err) if err else str(len(ids)) + ' other models'})")
            if clock() + interval_s > deadline:
                return False
            sleep(interval_s)


Row = tuple[str, str, str]


def check_rows(*, env: Mapping[str, str] | None = None, offline: bool = False,
               transport: httpx.BaseTransport | None = None) -> list[Row]:
    """Preflight rows (check, PASS | WARN | FAIL | SKIP, detail) for endpoint mode. Makes no completion: two GETs."""
    env = os.environ if env is None else env
    rows: list[Row] = []
    url = (env.get("WAPSI_LLM_BASE_URL") or "").strip().rstrip("/")
    wanted = (env.get("WAPSI_LLM_MODEL") or "").strip()
    if not url:
        rows.append(("endpoint base URL", "FAIL", "WAPSI_LLM_BASE_URL is not set (Token Factory: "
                                                  "https://developer.amd.com.cn/radeon/api/v1)"))
        return rows
    prov = provider_of(url, env)
    rows.append(("endpoint base URL", "PASS", f"{host_of(url)} ({prov['name']})"))
    if prov["kind"] == "other" and not prov["amd_served"]:
        rows.append(("endpoint hardware", "WARN", "not an AMD host: results are not AMD results unless "
                                                  "WAPSI_ENDPOINT_HARDWARE declares what serves it"))
    if api_key(env):
        rows.append(("endpoint API key", "PASS", "set in this shell (value not shown)"))
    elif prov["kind"] == "self-hosted":
        rows.append(("endpoint API key", "WARN", "not set (fine for a local server started without --api-key)"))
    else:
        rows.append(("endpoint API key", "FAIL", "not set: read -rs WAPSI_LLM_API_KEY && export WAPSI_LLM_API_KEY"))
    mode = default_json_mode(url, env)
    rows.append(("drafting model", "PASS" if wanted else "WARN",
                 wanted or f"WAPSI_LLM_MODEL unset: the model plan picks one (default {DEFAULT_MODEL})"))
    if offline:
        rows += [("endpoint /models", "SKIP", "--offline"), ("endpoint limits", "SKIP", "--offline")]
        return rows
    from editions.amd.kickoff.model_plan import plan_endpoint  # noqa: PLC0415  (standard library only)

    with _auth_client(api_key(env), transport) as client:
        code, cards, err = list_models(client, url)
        if err or not cards:
            hint = " (check the key)" if code in (401, 403) else ""
            rows.append(("endpoint /models", "FAIL", f"HTTP {code or 'none'}: {err or 'no models'}{hint}"))
            return rows
        ids = [str(c["id"]) for c in cards]
        rows.append(("endpoint /models", "PASS", f"{len(ids)} models served"))
        plan = plan_endpoint(ids, env)
        chosen = plan["drafting"]["id"]
        if chosen is None:  # the named model is not served and no fallback is allowed, or nothing at all is served
            rows.append(("model served", "FAIL", f"{'; '.join(plan['warnings'][:1])}; available: {', '.join(ids[:12])}"))
        else:
            asked = wanted or plan["drafting"]["candidates"][0]
            if not plan["fallback_in_use"]:
                rows.append(("model served", "PASS", chosen))
            else:
                rows.append(("model served", "WARN", f"{asked} is not served right now: the model plan uses {chosen}"
                             + ("" if plan["drafting"]["google_open_model"] else " (not a Gemma model: flagged in the "
                                "evidence; the Google partner prize needs Gemma or ADK in the workload)")))
            card = next((c for c in cards if c.get("id") == chosen), None) or {}
            if card.get("structured_outputs") is False and mode == "json_schema":
                rows.append(("JSON mode", "FAIL", "this model has no strict JSON schema output: "
                                                  "export WAPSI_LLM_JSON_MODE=json_object"))
            else:
                rows.append(("JSON mode", "PASS", mode))
        limits = usage_limits(client, url)
    if limits is None:
        rows.append(("endpoint limits", "SKIP", "no /usage on this endpoint"))
    else:
        rpm, left = limits.get("rpm_limit"), limits.get("daily_cost_remaining_usd")
        detail = f"{rpm} requests/min; daily cost {limits.get('daily_cost_used_usd')} of " \
                 f"{limits.get('daily_cost_limit_usd')} used"
        low = isinstance(left, int | float) and left < 0.2
        rows.append(("endpoint limits", "WARN" if low else "PASS",
                     detail + (" (little left today: the cap resets at " f"{limits.get('daily_reset_at')})" if low else "")))
    return rows


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="endpoint.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="preflight rows as TSV: check, status, detail")
    c.add_argument("--offline", action="store_true")
    p = sub.add_parser("probe", help="identify the endpoint (two GETs and one short completion)")
    p.add_argument("--out", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--no-chat", action="store_true", help="skip the completion (no model call)")
    w = sub.add_parser("wait-model", help="poll GET /models until the model is served (no completion, no cost)")
    w.add_argument("--model", default=None)
    w.add_argument("--timeout", type=float, default=1800.0)
    w.add_argument("--interval", type=float, default=30.0)
    w.add_argument("--call", action="store_true",
                   help="once listed, also ask the model for one short answer (a listed model may still refuse calls)")
    args = ap.parse_args(argv)
    if args.cmd == "wait-model":
        return 0 if wait_model(model=args.model, timeout_s=args.timeout, interval_s=max(5.0, args.interval),
                               call=args.call) else 1
    if args.cmd == "check":
        rows = check_rows(offline=args.offline)
        for check, status, detail in rows:
            print(f"{check}\t{status}\t{detail}")
        return 1 if any(s == "FAIL" for _, s, _ in rows) else 0
    info = probe(model=args.model, chat=not args.no_chat)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    pr = info.get("probe") or {}
    if info.get("model_fallback"):
        print(f"NOTE: {info.get('model_requested') or DEFAULT_MODEL} is not served right now: using {info.get('model')}")
    print(f"endpoint {info.get('endpoint_host')} | model {info.get('model')} | "
          f"{'ok' if info.get('ok') else 'FAILED: ' + str(info.get('error'))}"
          + (f" | {pr.get('latency_s')} s, cost {pr.get('cost_usd')}" if pr.get("ok") else ""))
    return 0 if info.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
