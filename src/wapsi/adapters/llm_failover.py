"""Fail over along an ordered list of models on one endpoint, for the prose of the drafts.

A shared endpoint can lose a model without an HTTP error. On 2026-10-14 the AMD Token Factory's ``Gemma-4-31B-IT``
answered HTTP 200 with runs of ``/`` for about half an hour, then HTTP 400 "model not supported", then came back,
while ``DeepSeek-V4-Flash`` on the same endpoint answered normally. The prose layer already refuses such an answer
(``wapsi.draft.prose.not_prose_reason``) and writes the fixed paragraph instead, so no garbage reaches a draft. With
``FailoverLLM`` the next model in the list writes the prose instead of the fixed paragraph:

- the models are tried in order (``WAPSI_LLM_MODELS``, the list voice intake, the agent and the kickoff plan read);
- an answer that is not language (``accept``, then ``wapsi.intake.llm_chain.is_degenerate``) or an endpoint error
  for that model (HTTP 400 "model not supported", 404, 5xx after the inner retries, a timeout) puts the model in
  cool-down (circuit breaker) and the next model answers the same request. A model that wrote garbage is never
  asked again for ``cooldown_s``; health is kept per (endpoint, model) for the whole process, shared with voice
  intake (``wapsi.intake.llm_chain.HEALTH``);
- errors that another model on the same endpoint cannot fix are raised at once: a bad key (401 / 403) and the
  account's rate limit or daily cap (429, ``QuotaExhausted``, ``RateLimited``);
- ``name`` is the name of the model that wrote the latest answer in this thread (``Draft.llm_model`` reads it, so
  every draft says which model wrote its prose), and ``report()`` counts answers and rejections per model.

With one model it behaves like that model (``build`` then returns it unwrapped). Nothing here touches the network.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

ACCOUNT_LEVEL_STATUS = frozenset({401, 403, 429})
_ACCOUNT_LEVEL_NAMES = frozenset({"QuotaExhausted", "RateLimited"})
MAX_EVENTS = 200


def _status(exc: BaseException) -> int | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "status_code", None)
        if isinstance(code, int):
            return code
        cur = cur.__cause__ or cur.__context__
    return None


def account_level(exc: BaseException) -> bool:
    """True for a failure no other model on the same endpoint can fix: the key, or the account's limits."""
    return type(exc).__name__ in _ACCOUNT_LEVEL_NAMES or _status(exc) in ACCOUNT_LEVEL_STATUS


def model_of(llm: Any) -> str:
    """The model id of an adapter, or of the adapter a ``ResilientLLM`` wraps."""
    for obj in (llm, getattr(llm, "inner", None)):
        if obj is None:
            continue
        for attr in ("model_id", "model"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value:
                return value
    return str(getattr(llm, "name", type(llm).__name__))


def base_url_of(llm: Any) -> str:
    for obj in (llm, getattr(llm, "inner", None)):
        value = getattr(obj, "base_url", None) if obj is not None else None
        if isinstance(value, str) and value:
            return value
    return ""


class AllModelsFailed(RuntimeError):
    """No model in the list could answer (each one's reason is in the message and in ``report()``)."""

    def __init__(self, message: str, *, last: BaseException | None = None) -> None:
        super().__init__(message)
        self.last = last
        self.status_code = _status(last) if last is not None else None


class FailoverLLM:
    """``wapsi.ports.LLM`` over an ordered list of models on one endpoint (see the module docstring)."""

    def __init__(
        self,
        llms: Sequence[Any],
        *,
        accept: Callable[[Sequence[Mapping[str, str]], str], str | None] | None = None,
        cooldown_s: float | None = None,
        error_cooldown_s: float | None = None,
        registry: Any | None = None,
        fail_fast: bool = False,
    ) -> None:
        if not llms:
            raise ValueError("FailoverLLM needs at least one model")
        from wapsi.intake import llm_chain as chain  # the shared health registry and garbage detector

        self.llms = list(llms)
        self.accept = accept
        self._degenerate = chain.is_degenerate
        self.registry = registry if registry is not None else chain.HEALTH
        self.cooldown_s = chain.DEFAULT_COOLDOWN_S if cooldown_s is None else float(cooldown_s)
        self.error_cooldown_s = chain.ERROR_COOLDOWN_S if error_cooldown_s is None else float(error_cooldown_s)
        # fail_fast: while every model rests after a failure, refuse at once (an interactive app: the prose gets its
        # fixed paragraph now instead of waiting on an unreachable host again); otherwise one half-open attempt
        self.fail_fast = fail_fast
        self._local = threading.local()
        self._lock = threading.Lock()
        self.answers: Counter[str] = Counter()  # model -> answers it wrote
        self.rejected: Counter[str] = Counter()  # model -> answers refused as not language
        self.failed: Counter[str] = Counter()  # model -> endpoint errors
        self.skipped: Counter[str] = Counter()  # model -> requests it was skipped for (cool-down)
        self.failovers = 0  # requests a later model answered after an earlier one failed
        self.events: list[dict[str, str]] = []  # bounded: what happened to whom (no prompt, no key)

    # ------------------------------------------------------------------ identity
    @property
    def primary(self) -> Any:
        return self.llms[0]

    @property
    def models(self) -> list[str]:
        return [model_of(m) for m in self.llms]

    @property
    def model(self) -> str:
        return model_of(self.primary)

    @property
    def model_id(self) -> str:
        return self.model

    @property
    def base_url(self) -> str:
        return base_url_of(self.primary)

    @property
    def name(self) -> str:
        """The model that wrote the latest answer in this thread (the first model before any answer)."""
        return getattr(self._local, "name", None) or str(getattr(self.primary, "name", self.model))

    @property
    def last_from_cache(self) -> bool:
        return bool(getattr(self._local, "from_cache", False))

    @property
    def last_model(self) -> str | None:
        return getattr(self._local, "model", None)

    @property
    def label(self) -> str:
        """The configuration, for a report header: the first model's name and the models it fails over to."""
        return f"{getattr(self.primary, 'name', self.model)}; failover: {', '.join(self.models[1:])}"

    def __repr__(self) -> str:
        return f"FailoverLLM(models={self.models!r})"

    def key(self, llm: Any) -> str:
        # the same key as wapsi.intake.llm_chain.ModelChain.key: voice and prose share what they learn
        return f"{base_url_of(llm)}|{model_of(llm)}"

    # ------------------------------------------------------------------ the call
    def _note(self, model: str, outcome: str, detail: str = "") -> None:
        with self._lock:
            if len(self.events) < MAX_EVENTS:
                self.events.append({"model": model, "outcome": outcome, "detail": detail[:160]})

    def _order(self) -> list[Any]:
        ready = [m for m in self.llms if self.registry.bad_for(self.key(m)) <= 0]
        for m in self.llms:
            if m not in ready:
                with self._lock:
                    self.skipped[model_of(m)] += 1
        if not ready and self.fail_fast:
            soonest = min(self.registry.bad_for(self.key(m)) for m in self.llms)
            raise AllModelsFailed(f"every model is resting after a failure; the next attempt in {soonest:.0f} s")
        # half-open: when every model is resting, the one due back soonest gets one honest attempt
        return ready or [min(self.llms, key=lambda m: self.registry.bad_for(self.key(m)))]

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        reasons: list[str] = []
        last: BaseException | None = None
        for i, llm in enumerate(self._order()):
            model = model_of(llm)
            try:
                text = llm.complete(messages, temperature=temperature, max_tokens=max_tokens, json_schema=json_schema)
            except Exception as exc:  # noqa: BLE001 - classified below
                if account_level(exc):
                    raise  # the key or the account's limits: another model on this endpoint will not help
                unusable = type(exc).__name__ == "UnusableAnswer"  # ResilientLLM's own accept check refused it
                self.registry.mark_bad(self.key(llm), self.cooldown_s if unusable else self.error_cooldown_s,
                                       f"{type(exc).__name__}"[:80])
                with self._lock:
                    (self.rejected if unusable else self.failed)[model] += 1
                self._note(model, "garbage" if unusable else "error", f"{type(exc).__name__}: {exc}")
                reasons.append(f"{model}: {'unusable answer' if unusable else type(exc).__name__}")
                last = exc
                continue
            why = (self.accept(messages, text) if self.accept is not None else None) or (
                "degenerate output" if self._degenerate(text) else None)
            if why:
                self.registry.mark_bad(self.key(llm), self.cooldown_s, why[:80])
                with self._lock:
                    self.rejected[model] += 1
                self._note(model, "garbage", why)
                reasons.append(f"{model}: {why}")
                last = AllModelsFailed(f"{model} answered, but the answer is unusable: {why}")
                continue
            self.registry.mark_ok(self.key(llm))
            with self._lock:
                self.answers[model] += 1
                if i:
                    self.failovers += 1
            self._local.name = str(getattr(llm, "name", model))
            self._local.model = model
            self._local.from_cache = bool(getattr(llm, "last_from_cache", False))
            return text
        raise AllModelsFailed("no model in the list answered: " + "; ".join(reasons), last=last)

    # ------------------------------------------------------------------ reporting
    def report(self) -> dict[str, Any]:
        """Per-model counts, and the members' own reports (``ResilientLLM.report``) when they have one."""
        members = [m.report() for m in self.llms if hasattr(m, "report")]
        out: dict[str, Any] = {}
        if members:
            out.update(members[0])  # the primary's shape, so readers of a single-model report keep working
            for key in ("requests", "api_calls", "ok", "cache_hits", "retries", "rate_limited_429",
                        "unavailable_5xx", "unusable_answers", "errors"):
                if all(isinstance(r.get(key), int) for r in members):
                    out[key] = sum(r[key] for r in members)
            out["reported_cost_usd"] = round(sum(float(r.get("reported_cost_usd") or 0.0) for r in members), 6)
            out["members"] = members
        with self._lock:
            out.update({
                "model_id": self.model,
                "failover_models": self.models,
                "answers_by_model": dict(self.answers),
                "rejected_by_model": dict(self.rejected),
                "errors_by_model": dict(self.failed),
                "skipped_by_model": dict(self.skipped),
                "failovers": self.failovers,
                "failover_events": list(self.events[-20:]),
            })
        return out


def build(llms: Sequence[Any], **kwargs: Any) -> Any:
    """``FailoverLLM`` over ``llms``, or the single LLM itself when there is only one."""
    llms = list(llms)
    return llms[0] if len(llms) == 1 else FailoverLLM(llms, **kwargs)
