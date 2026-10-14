"""``AgentEndpoint``: the OpenAI-compatible endpoint the agent's model runs behind, read from the environment.

The same variables as the drafting LLM, so one endpoint serves both, with agent-only overrides:

| Variable | Default | Meaning |
|---|---|---|
| ``WAPSI_LLM_BASE_URL`` | unset | OpenAI-compatible base URL (``.../v1``); **when set, the agent runs there** |
| ``WAPSI_LLM_API_KEY`` | unset | bearer token (never logged, never part of ``describe()`` or ``repr``) |
| ``WAPSI_LLM_MODEL`` | ``Gemma-4-31B-IT`` | model id as the endpoint names it |
| ``WAPSI_AGENT_BASE_URL`` / ``WAPSI_AGENT_API_KEY`` / ``WAPSI_AGENT_MODEL`` | the three above | agent-only endpoint (e.g. base Gemma 4 for the agent, a fine-tuned checkpoint for drafts) |
| ``WAPSI_AGENT_FALLBACK_MODELS`` | ``WAPSI_LLM_MODELS`` | comma-separated model ids on the same endpoint to use, in order, while the model above is not served or returns unusable output (shared catalogs change); every run is labelled with the model that answered. Unset: the other models of ``WAPSI_LLM_MODELS`` (else ``WAPSI_LLM_FALLBACK_MODELS``), the list voice intake and the kickoff model plan read, when the agent runs on the drafting endpoint; no fallback when neither is set |
| ``WAPSI_AGENT_EXTRA_BODY`` | unset | JSON object merged into every request body (e.g. ``{"reasoning_effort": "low"}``, or vLLM's ``{"chat_template_kwargs": {"enable_thinking": false}}``) |
| ``WAPSI_AGENT_TOOL_MODE`` | ``auto`` | ``auto`` = capability probe; ``native`` = OpenAI tool calls; ``json`` = strict JSON tool protocol |
| ``WAPSI_AGENT_TOOL_RESULT_ROLE`` | ``auto`` | message role of tool results in native mode: ``auto`` = what the probe found (``tool`` without a probe), ``tool`` (OpenAI), ``tool_responses`` (what ADK's LiteLlm sends for ``gemma-4`` model ids) |
| ``WAPSI_AGENT_TIMEOUT_S`` | ``90`` | per-request timeout |
| ``WAPSI_AGENT_MAX_TOKENS`` | ``2048`` | ``max_tokens`` per model turn |
| ``WAPSI_AGENT_TEMPERATURE`` | ``0.1`` | sampling temperature |
| ``WAPSI_AGENT_RETRIES`` | ``2`` | client retries on 429 / 5xx (``Retry-After`` honoured up to 60 s) |
| ``WAPSI_AGENT_PARALLEL_TOOLS`` | ``0`` | ``0`` / ``1`` = send ``parallel_tool_calls`` false / true with tools; ``omit`` = do not send it |
| ``WAPSI_AGENT_JSON_OBJECT`` | ``1`` | JSON protocol only: ask for ``response_format: json_object`` (dropped automatically if rejected) |
| ``WAPSI_AGENT_MAX_TOOL_CALLS`` | ``12`` | tool calls allowed per officer message |
| ``WAPSI_AGENT_MAX_LLM_CALLS`` | ``24`` | model turns allowed per run (ADK ``RunConfig.max_llm_calls``) |
| ``WAPSI_AGENT_MIN_INTERVAL_S`` | ``WAPSI_LLM_MIN_INTERVAL_S``, else 3 s for a shared endpoint and 0 for a local server or an offline fake | minimum seconds between model calls (AMD Token Factory: 20 requests/min for the whole team). The agent paces on the same process-wide limiter as the drafts' prose (``wapsi.adapters.llm_resilience.shared_limiter``), and a wait in progress is announced on ``RATE_WAITS`` so the chat can say so |
| ``WAPSI_AGENT_PROVIDER_LABEL`` | from the host | how the UI names the serving platform (e.g. ``vLLM-ROCm on MI300X``). Unset: ``WAPSI_LLM_PROVIDER`` (voice intake's label), else the host's own name, with the deployer's ``WAPSI_LLM_PLATFORM`` / ``WAPSI_LLM_ON_AMD`` statement added "as declared" for a host that says nothing itself |

Default model: ``Gemma-4-31B-IT`` is the id AMD Token Factory serves Google's Gemma 4 31B IT under; a vLLM server
names it with ``--served-model-name`` (the AMD compose file uses ``wapsi-llm``), so set ``WAPSI_LLM_MODEL`` there.

This module imports neither google-adk nor LiteLLM; ``build_endpoint_model`` does, on demand.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

DEFAULT_AGENT_MODEL = "Gemma-4-31B-IT"
TOOL_MODES = ("auto", "native", "json")
TOOL_RESULT_ROLES = ("auto", "tool", "tool_responses")
TRUE = {"1", "true", "yes", "on"}
_KEY_LIKE = re.compile(r"\b(?:rc|sk|hf|AIza)[-_][A-Za-z0-9_\-]{8,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")


def _text(env: Mapping[str, str], *keys: str) -> str | None:
    for k in keys:
        raw = (env.get(k) or "").strip()
        if raw:
            return raw
    return None


def _number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


@dataclass
class ModelTiming:
    """Where one model turn's time went, measured at the HTTP call (``endpoint_model.EndpointClient``): the
    requests themselves (``http_ms``, every attempt) and the pauses before them (``waited_ms``: client-side pacing
    and a server's ``Retry-After``). Local work (LiteLLM's first import, ADK's request building) is in neither."""

    http_ms: float = 0.0
    waited_ms: float = 0.0
    calls: int = 0


#: The turn being timed: set by the trace plugin before a model call, filled by the endpoint client, read after.
MODEL_TIMING: ContextVar[ModelTiming | None] = ContextVar("wapsi_model_timing", default=None)


def _default_gap(env: Mapping[str, str], url: str) -> float:
    """The agent's pause between model calls: ``WAPSI_AGENT_MIN_INTERVAL_S``, else the endpoint's pacing
    (``wapsi.adapters.llm_env.pacing``: 3 s for a shared endpoint such as the AMD Token Factory)."""
    if (env.get("WAPSI_AGENT_MIN_INTERVAL_S") or "").strip():
        return max(0.0, _number(env, "WAPSI_AGENT_MIN_INTERVAL_S", 0.0))
    from wapsi.adapters.llm_env import pacing

    return pacing(url, env)[1]


def _shared_fallbacks(env: Mapping[str, str], model: str, same_endpoint: bool) -> tuple[str, ...]:
    """The agent's fallback models: its own list, else the other models of the drafting endpoint's ordered list
    (``WAPSI_LLM_MODELS``, else ``WAPSI_LLM_FALLBACK_MODELS``) when the agent runs on that endpoint."""
    raw = (env.get("WAPSI_AGENT_FALLBACK_MODELS") or "").strip()
    if not raw and same_endpoint:
        raw = (env.get("WAPSI_LLM_MODELS") or env.get("WAPSI_LLM_FALLBACK_MODELS") or "").strip()
    names = [m.strip() for m in raw.split(",") if m.strip() and m.strip() != model]
    return tuple(dict.fromkeys(names))


def _declared_label(base_url: str, env: Mapping[str, str]) -> str:
    """A label for a host that says nothing about its hardware, when the deployer stated the platform
    (``WAPSI_LLM_PLATFORM`` / ``WAPSI_LLM_ON_AMD``, read through ``wapsi.adapters.llm_env``). "" otherwise."""
    name = provider_label(base_url)
    if not name.startswith(("OpenAI-compatible endpoint", "local OpenAI-compatible server")):
        return ""  # the host names its platform itself (AMD Token Factory, the compose stack, the offline fake)
    from wapsi.adapters.llm_env import declared_platform

    declared = declared_platform(env)
    return f"{name}, on {declared} as declared" if declared else ""


def _rpm(env: Mapping[str, str], url: str) -> int:
    from wapsi.adapters.llm_env import pacing

    return pacing(url, env)[0]


def provider_label(base_url: str) -> str:
    """How to name the serving platform from the endpoint's host (a label only; no claim about hardware beyond
    what the host says)."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host.endswith("amd.com.cn") or host.endswith("amd.com"):
        return "AMD Token Factory"
    if host in ("llm", "verifier-llm") or host.startswith("vllm"):
        return "vLLM-ROCm (AMD edition compose)"
    if host in ("127.0.0.1", "localhost", "::1"):
        return "local OpenAI-compatible server"
    if host.endswith(".offline") or host.endswith(".test") or host.endswith(".invalid"):
        return "offline fake endpoint"
    return f"OpenAI-compatible endpoint ({host or 'unknown host'})"


@dataclass(frozen=True)
class AgentEndpoint:
    base_url: str
    model: str = DEFAULT_AGENT_MODEL
    api_key: str | None = field(default=None, repr=False)
    tool_mode: str = "auto"
    tool_result_role: str = "auto"
    timeout_s: float = 90.0
    max_tokens: int = 2048
    temperature: float = 0.1
    max_retries: int = 2
    parallel_tool_calls: bool | None = False  # None = do not send the parameter
    json_object: bool = True
    max_tool_calls: int = 12
    max_llm_calls: int = 24
    min_interval_s: float = 0.0
    rpm: int = 0  # requests a minute on the shared limiter (0 = only the gap above)
    label: str = ""
    fallback_models: tuple[str, ...] = ()
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url or not self.base_url.strip():
            raise ValueError("the agent endpoint needs a base URL (WAPSI_LLM_BASE_URL)")
        if not self.model or not self.model.strip():
            raise ValueError("the agent endpoint needs a model id (WAPSI_LLM_MODEL)")
        if self.tool_mode not in TOOL_MODES:
            raise ValueError(f"WAPSI_AGENT_TOOL_MODE must be one of {', '.join(TOOL_MODES)} (got {self.tool_mode!r})")
        if self.tool_result_role not in TOOL_RESULT_ROLES:
            raise ValueError(f"WAPSI_AGENT_TOOL_RESULT_ROLE must be one of {', '.join(TOOL_RESULT_ROLES)} "
                             f"(got {self.tool_result_role!r})")
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        object.__setattr__(self, "model", self.model.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, base_url: str | None = None,
                 model: str | None = None) -> AgentEndpoint | None:
        """The endpoint the environment describes, or ``None`` when no base URL is set.

        ``base_url`` / ``model`` are the edition's defaults for the unset case (the AMD edition passes its vLLM
        URL and served model name); the ``WAPSI_AGENT_*`` and ``WAPSI_LLM_*`` variables win over them.
        """
        e = os.environ if env is None else env
        url = _text(e, "WAPSI_AGENT_BASE_URL", "WAPSI_LLM_BASE_URL") or (base_url or "").strip() or None
        if not url:
            return None
        raw_extra = (e.get("WAPSI_AGENT_EXTRA_BODY") or "").strip()
        try:
            extra = json.loads(raw_extra) if raw_extra else {}
        except ValueError as exc:
            raise ValueError("WAPSI_AGENT_EXTRA_BODY must be a JSON object") from exc
        if not isinstance(extra, dict) or {"model", "messages", "tools", "stream"} & set(extra):
            raise ValueError("WAPSI_AGENT_EXTRA_BODY must be a JSON object without model, messages, tools or stream")
        parallel_raw = (e.get("WAPSI_AGENT_PARALLEL_TOOLS") or "0").strip().lower()
        parallel = None if parallel_raw in ("omit", "none", "") else parallel_raw in TRUE
        return cls(
            base_url=url,
            model=_text(e, "WAPSI_AGENT_MODEL", "WAPSI_LLM_MODEL") or (model or "").strip() or DEFAULT_AGENT_MODEL,
            api_key=_text(e, "WAPSI_AGENT_API_KEY", "WAPSI_LLM_API_KEY"),
            tool_mode=(_text(e, "WAPSI_AGENT_TOOL_MODE") or "auto").lower(),
            tool_result_role=(_text(e, "WAPSI_AGENT_TOOL_RESULT_ROLE") or "auto").lower(),
            timeout_s=_number(e, "WAPSI_AGENT_TIMEOUT_S", cls.timeout_s),
            max_tokens=max(64, int(_number(e, "WAPSI_AGENT_MAX_TOKENS", cls.max_tokens))),
            temperature=_number(e, "WAPSI_AGENT_TEMPERATURE", cls.temperature),
            max_retries=max(0, int(_number(e, "WAPSI_AGENT_RETRIES", cls.max_retries))),
            parallel_tool_calls=parallel,
            json_object=(e.get("WAPSI_AGENT_JSON_OBJECT") or "1").strip().lower() in TRUE,
            max_tool_calls=max(1, int(_number(e, "WAPSI_AGENT_MAX_TOOL_CALLS", cls.max_tool_calls))),
            max_llm_calls=max(2, int(_number(e, "WAPSI_AGENT_MAX_LLM_CALLS", cls.max_llm_calls))),
            min_interval_s=_default_gap(e, url),
            rpm=_rpm(e, url),
            label=_text(e, "WAPSI_AGENT_PROVIDER_LABEL", "WAPSI_LLM_PROVIDER") or _declared_label(url, e),
            fallback_models=_shared_fallbacks(
                e, _text(e, "WAPSI_AGENT_MODEL", "WAPSI_LLM_MODEL") or (model or "").strip() or DEFAULT_AGENT_MODEL,
                same_endpoint=not _text(e, "WAPSI_AGENT_BASE_URL")
                or _text(e, "WAPSI_AGENT_BASE_URL") == _text(e, "WAPSI_LLM_BASE_URL")),
            extra_body=extra,
        )

    @property
    def litellm_model(self) -> str:
        """LiteLLM routes ``openai/<id>`` to any OpenAI-compatible server at ``api_base``."""
        return f"openai/{self.model}"

    @property
    def provider(self) -> str:
        return self.label or provider_label(self.base_url)

    def describe(self) -> dict[str, Any]:
        """Non-secret view (health endpoints, UI, CLI): never contains the key."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "provider": self.provider,
            "api_key_set": bool(self.api_key),
            "tool_mode": self.tool_mode,
            "fallback_models": list(self.fallback_models),
            "max_tool_calls": self.max_tool_calls,
            "max_llm_calls": self.max_llm_calls,
            "timeout_s": self.timeout_s,
        }

    def scrub(self, text: str) -> str:
        """``text`` with this endpoint's key (and anything key-shaped) masked, for errors shown to people."""
        out = str(text or "")
        if self.api_key and len(self.api_key) >= 6:
            out = out.replace(self.api_key, "***")
        return _BEARER.sub(r"\1***", _KEY_LIKE.sub("***", out))


@dataclass
class EndpointStats:
    """What the endpoint reported for the agent's model calls in this process (thread-safe)."""

    calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost_reported: int = 0  # responses whose usage carried a cost (AMD Token Factory reports one per response)
    latency_ms_total: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, latency_ms: float, usage: Any = None) -> None:
        def num(key: str) -> float | None:
            value = usage.get(key) if isinstance(usage, Mapping) else getattr(usage, key, None)
            return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

        with self._lock:
            self.calls += 1
            self.latency_ms_total += latency_ms
            self.latencies_ms = (self.latencies_ms + [round(latency_ms, 1)])[-200:]
            if usage is not None:
                self.prompt_tokens += int(num("prompt_tokens") or 0)
                self.completion_tokens += int(num("completion_tokens") or 0)
                cost = num("cost")
                if cost is not None:
                    self.cost_usd += cost
                    self.cost_reported += 1

    def error(self) -> None:
        with self._lock:
            self.errors += 1

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            lat = sorted(self.latencies_ms)
            return {
                "calls": self.calls,
                "errors": self.errors,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 8) if self.cost_reported else None,
                "latency_ms_mean": round(self.latency_ms_total / self.calls, 1) if self.calls else None,
                "latency_ms_p50": lat[len(lat) // 2] if lat else None,
                "latency_ms_max": lat[-1] if lat else None,
            }


def build_endpoint_model(endpoint: AgentEndpoint, **kwargs: Any) -> Any:
    """The ADK model for ``endpoint``: a ``LiteLlm`` (``endpoint_model.EndpointLlm``) that probes the endpoint on
    first use and speaks native tool calls or the JSON tool protocol. Building it makes no network call."""
    from .endpoint_model import EndpointLlm

    return EndpointLlm(endpoint, **kwargs)
