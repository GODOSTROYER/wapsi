"""An ordered chain of text models with a degenerate-output detector, a health probe and a circuit breaker.

Shared model endpoints fail in ways an HTTP status does not show. On 2026-10-14 the AMD Token Factory's
``Gemma-4-31B-IT`` answered HTTP 200 with ``{"////////…`` until it ran out of tokens, for every prompt, while other
models on the same endpoint were fine. A voice note must still be read, so the open voice path:

- **detects degenerate output** (``is_degenerate``): a long run of one character, a short pattern repeated over
  and over, or a long answer made of a handful of distinct characters;
- **never retries a model that produced garbage**: it is skipped for ``cooldown_s`` (circuit breaker) and the next
  model in the chain answers;
- **probes a model before trusting it** when there is an alternative (``Reply with exactly: AMD OK``, a dozen
  tokens): garbage shows up in about a second instead of after a full-length answer. A pass is remembered for
  ``health_ttl_s``;
- **records which model actually answered** and what happened to the others (``Attempt``), so the page and the
  benchmark can say "Gemma-4-31B-IT returned unusable output, rejected; DeepSeek-V4-Flash answered".

The chain is ``WAPSI_VOICE_LLM_MODELS`` (else ``WAPSI_LLM_MODELS``, else ``WAPSI_LLM_FALLBACK_MODELS``: the same
list as the kickoff model plan and the agent read), comma separated, tried in order; on the AMD Token Factory the
default is ``DEFAULT_TOKEN_FACTORY_CHAIN`` with the configured model first. Health is kept per
(endpoint, model) for the whole process, so every extractor benefits from what one of them learnt.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wapsi.adapters.llm_openai_compat import LLMError

VOICE_MODELS_ENV = "WAPSI_VOICE_LLM_MODELS"
MODELS_ENV = "WAPSI_LLM_MODELS"
FALLBACK_MODELS_ENV = "WAPSI_LLM_FALLBACK_MODELS"  # the kickoff model plan's spelling of the same list
COOLDOWN_ENV = "WAPSI_LLM_COOLDOWN_S"
HEALTH_TTL_ENV = "WAPSI_LLM_HEALTH_TTL_S"
PROBE_ENV = "WAPSI_LLM_HEALTH_PROBE"
DEFAULT_TOKEN_FACTORY_CHAIN = ("Gemma-4-31B-IT", "DeepSeek-V4-Flash", "Qwen3.8-27B", "MiniCPM-V-4.7-35B-A3B")
DEFAULT_COOLDOWN_S = 600.0
DEFAULT_HEALTH_TTL_S = 300.0
ERROR_COOLDOWN_S = 60.0  # an endpoint error may pass sooner than a model that writes garbage
PROBE_MAX_TOKENS = 12
PROBE_MESSAGES = (
    {"role": "system", "content": "You are a health check."},
    {"role": "user", "content": "Reply with exactly: AMD OK"},
)

_RUN = re.compile(r"(\S)\1{24,}")  # one visible character 25+ times in a row
_CYCLE = re.compile(r"(\S{2,6}?)\1{11,}")  # a 2-6 character pattern 12+ times in a row


def is_degenerate(text: str) -> bool:
    """True for output a broken model produces: ``{"//////…``, ``0000//0000//…``, a page of one symbol.

    Real answers never trip it: JSON for a voice note uses dozens of distinct characters, and no field holds 25
    identical characters in a row (a 12-digit UTR of zeros is the longest plausible run)."""
    body = (text or "").strip()
    if not body:
        return False  # empty is "no answer", handled by the caller as invalid
    if _RUN.search(body) or _CYCLE.search(body):
        return True
    return len(body) > 40 and len(set(body)) < 12


def model_names(env: Mapping[str, str], primary: str = "", *, token_factory: bool = False) -> list[str]:
    """The chain to try, in order: the env list if set, else ``primary`` (plus the Token Factory defaults when the
    endpoint is the Token Factory). Duplicates are dropped; ``primary`` leads unless the env list says otherwise."""
    raw = (env.get(VOICE_MODELS_ENV) or env.get(MODELS_ENV) or env.get(FALLBACK_MODELS_ENV) or "").strip()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        names = [primary] if primary else []
        if token_factory:
            names += list(DEFAULT_TOKEN_FACTORY_CHAIN)
    elif primary and primary not in names:
        names.insert(0, primary)
    return list(dict.fromkeys(names))


@dataclass(frozen=True)
class Attempt:
    """What one model did with one request."""

    model: str
    outcome: str  # ok | garbage | invalid | error | unhealthy
    detail: str = ""
    elapsed_s: float = 0.0

    def text(self) -> str:
        words = {"ok": "answered", "garbage": "returned unusable output, rejected",
                 "invalid": "answer failed validation, rejected", "error": "endpoint error",
                 "unhealthy": "failed its health check, skipped"}  # fmt: skip
        return f"{self.model}: {words.get(self.outcome, self.outcome)}"


class HealthRegistry:
    """Per (endpoint, model): bad until a time, or good since a time. Thread-safe; one per process by default."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._bad_until: dict[str, float] = {}
        self._ok_at: dict[str, float] = {}
        self._why: dict[str, str] = {}

    def mark_bad(self, key: str, cooldown_s: float, why: str = "") -> None:
        with self._lock:
            self._bad_until[key] = self._clock() + max(0.0, cooldown_s)
            self._ok_at.pop(key, None)
            self._why[key] = why

    def mark_ok(self, key: str) -> None:
        with self._lock:
            self._ok_at[key] = self._clock()
            self._bad_until.pop(key, None)
            self._why.pop(key, None)

    def bad_for(self, key: str) -> float:
        """Seconds until the model may be tried again (0 = not in cooldown)."""
        with self._lock:
            return max(0.0, self._bad_until.get(key, 0.0) - self._clock())

    def fresh(self, key: str, ttl_s: float) -> bool:
        with self._lock:
            at = self._ok_at.get(key)
            return at is not None and self._clock() - at < ttl_s

    def why(self, key: str) -> str:
        with self._lock:
            return self._why.get(key, "")

    def reset(self) -> None:
        with self._lock:
            self._bad_until.clear()
            self._ok_at.clear()
            self._why.clear()


HEALTH = HealthRegistry()


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float((env.get(key) or "").strip() or default)
    except ValueError:
        return default


class ModelChain:
    """Text LLMs tried in order, skipping the ones known to be unhealthy."""

    def __init__(
        self,
        llms: Sequence[Any],
        *,
        probe: bool | None = None,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        health_ttl_s: float = DEFAULT_HEALTH_TTL_S,
        registry: HealthRegistry | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not llms:
            raise ValueError("a model chain needs at least one model")
        self.llms = list(llms)
        self.probe = len(self.llms) > 1 if probe is None else probe  # with one model there is nothing to fail over to
        self.cooldown_s = cooldown_s
        self.health_ttl_s = health_ttl_s
        self.registry = registry or HEALTH
        self._clock = clock

    @classmethod
    def of(cls, llm: Any) -> ModelChain:
        return llm if isinstance(llm, ModelChain) else cls([llm])

    @staticmethod
    def model_of(llm: Any) -> str:
        return str(getattr(llm, "model", None) or getattr(llm, "name", type(llm).__name__))

    def key(self, llm: Any) -> str:
        return f"{getattr(llm, 'base_url', '')}|{self.model_of(llm)}"

    @property
    def primary(self) -> Any:
        return self.llms[0]

    @property
    def models(self) -> list[str]:
        return [self.model_of(m) for m in self.llms]

    def mark_bad(self, llm: Any, why: str, cooldown_s: float | None = None) -> None:
        self.registry.mark_bad(self.key(llm), self.cooldown_s if cooldown_s is None else cooldown_s, why)

    def mark_ok(self, llm: Any) -> None:
        self.registry.mark_ok(self.key(llm))

    def check(self, llm: Any) -> Attempt | None:
        """Probe one model. None = healthy (remembered); an ``Attempt`` says why it is skipped."""
        t0 = self._clock()
        try:
            reply = llm.complete([dict(m) for m in PROBE_MESSAGES], temperature=0.0, max_tokens=PROBE_MAX_TOKENS)
        except LLMError as exc:
            if exc.status_code in (401, 403, 429):
                raise  # the key or the account's rate limit: another model on the same endpoint will not help
            self.mark_bad(llm, "health check failed", ERROR_COOLDOWN_S)
            return Attempt(self.model_of(llm), "unhealthy", str(exc)[:160], round(self._clock() - t0, 3))
        said = re.sub(r"[^a-z]+", " ", (reply or "").lower()).strip()
        if is_degenerate(reply) or not said.startswith("amd ok"):
            self.mark_bad(llm, "health check returned unusable output")
            return Attempt(self.model_of(llm), "unhealthy", (reply or "")[:60], round(self._clock() - t0, 3))
        self.mark_ok(llm)
        return None

    def candidates(self, skipped: list[Attempt] | None = None) -> Iterator[Any]:
        """Models to try, in order. Models in cooldown are skipped; a model whose health is not fresh is probed
        first (when probing is on). If every model is in cooldown, the one due back soonest gets one chance."""
        skipped = skipped if skipped is not None else []
        offered = 0
        for llm in self.llms:
            key = self.key(llm)
            if self.registry.bad_for(key) > 0:
                skipped.append(Attempt(self.model_of(llm), "unhealthy", self.registry.why(key) or "in cooldown"))
                continue
            if self.probe and not self.registry.fresh(key, self.health_ttl_s):
                failed = self.check(llm)
                if failed is not None:
                    skipped.append(failed)
                    continue
            offered += 1
            yield llm
        if offered == 0:  # half-open: better one honest attempt than "every model is resting"
            yield min(self.llms, key=lambda m: self.registry.bad_for(self.key(m)))


def chain_settings(env: Mapping[str, str]) -> dict[str, Any]:
    """``cooldown_s`` / ``health_ttl_s`` / ``probe`` for ``ModelChain`` from the environment."""
    raw = (env.get(PROBE_ENV) or "").strip().lower()
    probe = None if raw in ("", "auto") else raw in ("1", "true", "yes", "on")
    return {
        "cooldown_s": _float(env, COOLDOWN_ENV, DEFAULT_COOLDOWN_S),
        "health_ttl_s": _float(env, HEALTH_TTL_ENV, DEFAULT_HEALTH_TTL_S),
        "probe": probe,
    }
