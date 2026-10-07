"""Free-tier-friendly wrapper around any ``wapsi.ports.LLM``: rate limiting, retries, a response cache, accounting.

``ResilientLLM(inner)`` implements ``wapsi.ports.LLM`` and adds, in this order, for every ``complete`` call:

1. **Cache.** The request (model, messages, temperature, max_tokens, json_schema) is hashed (SHA-256 of canonical
   JSON). A hit returns the stored answer without calling the model, so re-running an evaluation is free and
   deterministic. Entries are plain JSON files under ``cache_dir`` (gitignored ``.cache/llm`` for evals). The cache
   exists only to replay evaluation runs; its contents are never training data (AGENTS.md).
2. **Rate limiter.** A sliding 60-second window keeps requests under ``rpm``; a per-day counter (Pacific time, when
   the Gemini API free tier resets) keeps them under ``rpd``. The day counter is persisted next to the cache, so a
   rerun later the same day knows what is left. When the day budget is spent, calls fail fast with
   ``QuotaExhausted`` (an ``LLMError`` with ``status_code=429``) instead of hammering the API; Wapsi's prose layer
   then falls back to its deterministic paragraph and the report shows it in ``llm_prose_fallback_rate``.
3. **Retries.** 408 / 429 / 5xx answers, timeouts and connection errors are retried with exponential backoff that
   honours the server's ``RetryInfo`` delay when the error carries one. A 429 for a *daily* quota is never retried
   (it only resets at midnight Pacific). Other errors propagate at once.
4. **Accounting.** API calls, cache hits, retries, 429s, time spent waiting, token usage (from ``inner.last_usage``:
   google-genai ``usage_metadata`` or an OpenAI-style ``TokenUsage``) and a cost estimate (``report()``).

Default limits are conservative free-tier figures (``DEFAULT_RPM`` / ``DEFAULT_RPD``); the real per-project limits are
shown at https://aistudio.google.com/rate-limit. Override with ``WAPSI_LLM_RPM`` / ``WAPSI_LLM_RPD`` (``0`` = no
limit). The wrapper never sees or stores an API key. Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from wapsi.adapters.llm_openai_compat import LLMError

DEFAULT_RPM = 10  # conservative Gemini API free-tier figures; the real limits are per project (AI Studio)
DEFAULT_RPD = 250
RPM_ENV = "WAPSI_LLM_RPM"
RPD_ENV = "WAPSI_LLM_RPD"
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
CACHE_VERSION = 1
CACHE_PURPOSE = "evaluation replay cache; never training data"
QUOTA_FILE = "_quota.json"

# Paid-tier list prices in USD per 1M tokens (input, output including thinking), used ONLY to estimate what a run
# would cost on a billed key or on Agent Platform. ASSUMED: the published Gemini 2.5 rates (2025) by model family;
# verify at https://ai.google.dev/gemini-api/docs/pricing and override with WAPSI_LLM_PRICE_IN / _OUT. The Gemini
# API free tier itself costs nothing (over the limit you get HTTP 429, never a bill).
PAID_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "flash-lite": (0.10, 0.40),
    "flash": (0.30, 2.50),
    "pro": (1.25, 10.00),
}
PRICE_BASIS = "assumed paid-tier list price per 1M tokens (Gemini 2.5 family rates, 2025); verify before quoting"

_TRANSIENT_NAMES = frozenset({
    "ServerError", "ConnectError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
    "RemoteProtocolError", "TimeoutException", "ReadError",
})
_DAILY = re.compile(r"per[ _-]?day|daily quota|requests ?per ?day", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)


class QuotaExhausted(LLMError):
    """The day's request budget is used up (client-side counter or the server's per-day 429)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=429)


def pacific_today(now: datetime | None = None) -> date:
    """Today in America/Los_Angeles: the Gemini API free tier's requests-per-day reset at midnight Pacific."""
    now = now or datetime.now(UTC)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/Los_Angeles")).date()
    except Exception:  # noqa: BLE001 - no tz database: UTC-8 is close enough for a counter
        return (now.astimezone(UTC) - timedelta(hours=8)).date()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up at call time (tests patch time.sleep)


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


# --------------------------------------------------------------------------- error classification


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in out and len(out) < 5:
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def is_daily_limit(exc: BaseException) -> bool:
    """A per-day quota 429 (retrying before the reset cannot succeed)."""
    if isinstance(exc, QuotaExhausted):
        return True
    try:
        from wapsi.adapters.gemini_llm import is_daily_quota
    except ImportError:  # pragma: no cover - the module is part of the package
        is_daily_quota = None  # type: ignore[assignment]
    for e in _chain(exc):
        if _status(e) == 429 or isinstance(e, LLMError):
            if is_daily_quota is not None and getattr(e, "details", None) is not None and is_daily_quota(e):
                return True
            if _status(e) == 429 and _DAILY.search(str(e)):
                return True
    return False


def server_retry_delay(exc: BaseException) -> float | None:
    """The server's suggested wait: ``RetryInfo`` on a google-genai error, else a ``retryDelay`` in the message,
    else a ``retry_after_s`` attribute."""
    try:
        from wapsi.adapters.gemini_llm import retry_delay_s
    except ImportError:  # pragma: no cover
        retry_delay_s = None  # type: ignore[assignment]
    for e in _chain(exc):
        if retry_delay_s is not None and getattr(e, "details", None) is not None:
            d = retry_delay_s(e)
            if d is not None:
                return d
        v = getattr(e, "retry_after_s", None)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
        m = _RETRY_DELAY.search(str(e))
        if m:
            return float(m.group(1))
    return None


def is_retryable(exc: BaseException) -> bool:
    code = _status(exc)
    if code is not None:
        return code in RETRY_STATUS
    for e in _chain(exc):
        c = _status(e)
        if c is not None:
            return c in RETRY_STATUS
        if isinstance(e, TimeoutError | ConnectionError) or type(e).__name__ in _TRANSIENT_NAMES:
            return True
    return False


# --------------------------------------------------------------------------- usage


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.total_tokens += other.total_tokens


def _get(obj: Any, *names: str) -> int:
    for n in names:
        v = obj.get(n) if isinstance(obj, Mapping) else getattr(obj, n, None)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return max(0, int(v))
    return 0


def usage_from(raw: Any) -> Usage | None:
    """Normalise google-genai ``usage_metadata``, an OpenAI ``TokenUsage`` or a dict of either."""
    if raw is None:
        return None
    prompt = _get(raw, "prompt_token_count", "prompt_tokens")
    output = _get(raw, "candidates_token_count", "completion_tokens", "output_tokens")
    thinking = _get(raw, "thoughts_token_count", "thinking_tokens")
    total = _get(raw, "total_token_count", "total_tokens") or prompt + output + thinking
    if not (prompt or output or thinking or total):
        return None
    return Usage(prompt, output, thinking, total)


def paid_price(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for ``model`` (env override first, then the model family)."""
    raw_in, raw_out = os.environ.get("WAPSI_LLM_PRICE_IN"), os.environ.get("WAPSI_LLM_PRICE_OUT")
    if raw_in and raw_out:
        try:
            return float(raw_in), float(raw_out)
        except ValueError:
            pass
    m = model.lower()
    for family in ("flash-lite", "flash", "pro"):  # most specific first
        if family in m:
            return PAID_PRICE_PER_M[family]
    return PAID_PRICE_PER_M["flash"]


# --------------------------------------------------------------------------- rate limiter


class RateLimiter:
    """Sliding-window requests-per-minute plus a requests-per-day budget (``0`` disables either)."""

    def __init__(
        self,
        rpm: int = DEFAULT_RPM,
        rpd: int = DEFAULT_RPD,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        today: Callable[[], date] = pacific_today,
        window_s: float = 60.0,
    ) -> None:
        self.rpm = max(0, int(rpm))
        self.rpd = max(0, int(rpd))
        self.state_path = state_path
        self._clock = clock
        self._sleep = sleep or _sleep
        self._today = today
        self.window_s = window_s
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._day, self._used = self._load()
        self.exhausted = False  # set when the server reports a per-day 429

    def _load(self) -> tuple[str, int]:
        day = self._today().isoformat()
        if self.state_path is None or not self.state_path.exists():
            return day, 0
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return day, 0
        if data.get("date") == day and isinstance(data.get("requests"), int):
            return day, max(0, data["requests"])
        return day, 0

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": self._day, "requests": self._used}), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass  # the counter is advisory

    @property
    def used_today(self) -> int:
        return self._used

    def remaining_today(self) -> int | None:
        return None if not self.rpd else max(0, self.rpd - self._used)

    def acquire(self) -> float:
        """Block until a request may be sent; returns the seconds waited. Raises ``QuotaExhausted``."""
        waited = 0.0
        with self._lock:
            day = self._today().isoformat()
            if day != self._day:  # a new Pacific day: the free tier's daily counter reset
                self._day, self._used, self.exhausted = day, 0, False
            if self.exhausted or (self.rpd and self._used >= self.rpd):
                raise QuotaExhausted(
                    f"daily request budget used up ({self._used}/{self.rpd or '?'} today, Pacific time); the Gemini "
                    f"API free tier resets at midnight Pacific. Raise {RPD_ENV} if your project allows more.")
            if self.rpm:
                while True:
                    now = self._clock()
                    while self._recent and now - self._recent[0] >= self.window_s:
                        self._recent.popleft()
                    if len(self._recent) < self.rpm:
                        break
                    delay = self.window_s - (now - self._recent[0]) + 0.05
                    self._sleep(delay)
                    waited += delay
            self._recent.append(self._clock())
            self._used += 1
            self._save()
        return waited


# --------------------------------------------------------------------------- cache


# The fact sheet carries the case's ``opened_at``: the wall-clock time the case was processed, different on every
# run. It is masked in the cache key only (prose never contains dates: those are slots), so reruns hit the cache.
_VOLATILE = re.compile(r'("case":\s*\{[^{}]*?"opened_at":\s*")[^"]*(")')


def _stable(content: str) -> str:
    return _VOLATILE.sub(r"\1~\2", content)


def request_key(model: str, messages: Sequence[Mapping[str, str]], *, temperature: float, max_tokens: int,
                json_schema: Mapping[str, Any] | None) -> str:
    payload = {
        "v": CACHE_VERSION,
        "model": model,
        "messages": [{"role": m.get("role", ""), "content": _stable(str(m.get("content", "")))} for m in messages],
        "temperature": round(float(temperature), 4),
        "max_tokens": int(max_tokens),
        "json_schema": json_schema,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    """One JSON file per request hash: ``<root>/<key[:2]>/<key>.json``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None

    def put(self, key: str, text: str, *, model: str, usage: Usage | None) -> None:
        p = self._path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": CACHE_VERSION, "purpose": CACHE_PURPOSE, "model": model, "text": text,
                                       "usage": asdict(usage) if usage else None}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass  # caching is best-effort


# --------------------------------------------------------------------------- the wrapper


@dataclass
class LLMStats:
    requests: int = 0  # complete() calls received
    api_calls: int = 0  # calls sent to the model (including retries)
    ok: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limited: int = 0  # 429 answers
    unavailable: int = 0  # 5xx / timeouts
    errors: int = 0  # calls that finally raised
    quota_exhausted: bool = False
    rate_limit_wait_s: float = 0.0
    backoff_wait_s: float = 0.0


class ResilientLLM:
    """``wapsi.ports.LLM`` with caching, RPM/RPD limiting, 429/503 retries and usage accounting."""

    def __init__(
        self,
        inner: Any,
        *,
        model_id: str | None = None,
        backend: str | None = None,
        rpm: int | None = None,
        rpd: int | None = None,
        cache_dir: str | Path | None = None,
        max_retries: int = 5,
        backoff_s: float = 2.0,
        max_backoff_s: float = 65.0,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = pacific_today,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.inner = inner
        self.model_id = model_id or getattr(inner, "model", None) or getattr(inner, "name", "llm")
        self.backend = backend or getattr(inner, "backend", None)
        self.name = f"{getattr(inner, 'name', self.model_id)}+resilient"
        self.cache = ResponseCache(cache_dir) if cache_dir else None
        rpm = _env_int(RPM_ENV, DEFAULT_RPM) if rpm is None else rpm
        rpd = _env_int(RPD_ENV, DEFAULT_RPD) if rpd is None else rpd
        state = Path(cache_dir) / QUOTA_FILE if cache_dir else None
        self.limiter = limiter or RateLimiter(rpm, rpd, state_path=state, clock=clock, sleep=sleep, today=today)
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep or _sleep
        self.stats = LLMStats()
        self.usage = Usage()  # tokens billed by the API in this run
        self.cached_usage = Usage()  # tokens of answers served from the cache (not billed again)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"ResilientLLM({self.inner!r}, rpm={self.limiter.rpm}, rpd={self.limiter.rpd})"

    # ------------------------------------------------------------------ LLM protocol
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        with self._lock:
            self.stats.requests += 1
        key = request_key(str(self.model_id), messages, temperature=temperature, max_tokens=max_tokens,
                          json_schema=json_schema)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                with self._lock:
                    self.stats.cache_hits += 1
                    u = Usage(**hit["usage"]) if isinstance(hit.get("usage"), dict) else None
                    if u:
                        self.cached_usage.add(u)
                return hit["text"]

        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                waited = self.limiter.acquire()
            except QuotaExhausted:
                with self._lock:
                    self.stats.quota_exhausted = True
                    self.stats.errors += 1
                raise
            with self._lock:
                self.stats.rate_limit_wait_s += waited
                self.stats.api_calls += 1
            if hasattr(self.inner, "last_usage"):
                try:
                    self.inner.last_usage = None
                except AttributeError:
                    pass
            try:
                text = self.inner.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                           json_schema=json_schema)
            except Exception as exc:  # noqa: BLE001 - classified below
                code = _status(exc)
                with self._lock:
                    if code == 429:
                        self.stats.rate_limited += 1
                    elif (code or 0) >= 500 or (code is None and is_retryable(exc)):
                        self.stats.unavailable += 1
                if is_daily_limit(exc):
                    self.limiter.exhausted = True
                    with self._lock:
                        self.stats.quota_exhausted = True
                        self.stats.errors += 1
                    if isinstance(exc, QuotaExhausted):
                        raise
                    raise QuotaExhausted(f"Gemini daily quota reached: {str(exc)[:300]}") from exc
                if not is_retryable(exc) or attempt == attempts - 1:
                    with self._lock:
                        self.stats.errors += 1
                    raise
                delay = min(self.backoff_s * (2**attempt), self.max_backoff_s)
                hinted = server_retry_delay(exc)
                if hinted is not None:
                    delay = max(delay, min(hinted, self.max_backoff_s))
                with self._lock:
                    self.stats.retries += 1
                    self.stats.backoff_wait_s += delay
                self._sleep(delay)
                continue
            u = usage_from(getattr(self.inner, "last_usage", None))
            with self._lock:
                self.stats.ok += 1
                if u:
                    self.usage.add(u)
            if self.cache is not None:
                self.cache.put(key, text, model=str(self.model_id), usage=u)
            return text
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ reporting
    def report(self) -> dict[str, Any]:
        """Model id, call/caching/retry counts, token usage and a cost estimate (safe to publish: no secrets)."""
        p_in, p_out = paid_price(str(self.model_id))

        def cost(u: Usage) -> float:
            return round(u.prompt_tokens / 1e6 * p_in + (u.output_tokens + u.thinking_tokens) / 1e6 * p_out, 6)

        free_tier = self.backend == "aistudio"
        paid_equiv = cost(self.usage)
        s = self.stats
        return {
            "model_id": str(self.model_id),
            "backend": self.backend,
            "requests": s.requests,
            "api_calls": s.api_calls,
            "ok": s.ok,
            "cache_hits": s.cache_hits,
            "cache_hit_rate": round(s.cache_hits / s.requests, 4) if s.requests else 0.0,
            "retries": s.retries,
            "rate_limited_429": s.rate_limited,
            "unavailable_5xx": s.unavailable,
            "errors": s.errors,
            "quota_exhausted": s.quota_exhausted,
            "rate_limit_wait_s": round(s.rate_limit_wait_s, 2),
            "backoff_wait_s": round(s.backoff_wait_s, 2),
            "limits": {"rpm": self.limiter.rpm, "rpd": self.limiter.rpd, "used_today": self.limiter.used_today,
                       "remaining_today": self.limiter.remaining_today()},
            "tokens": asdict(self.usage),
            "tokens_served_from_cache": asdict(self.cached_usage),
            "cost_estimate_usd": {
                "this_run": 0.0 if free_tier else paid_equiv,
                "paid_tier_equivalent": paid_equiv,
                "saved_by_cache_paid_equivalent": cost(self.cached_usage),
                "price_per_1m_tokens": {"input": p_in, "output": p_out},
                "basis": ("Gemini API free tier: no charge (429 when over the limit). " if free_tier else "")
                + PRICE_BASIS,
            },
        }
