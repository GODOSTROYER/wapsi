"""The open-model endpoint, configured from the environment (any OpenAI-compatible server).

Three variables point every LLM-side call at one endpoint:

| Variable | Meaning |
|---|---|
| ``WAPSI_LLM_BASE_URL`` | the endpoint's base URL, e.g. ``https://developer.amd.com.cn/radeon/api/v1`` (AMD Token Factory) or ``http://<host>:8000/v1`` (your own vLLM on AMD) |
| ``WAPSI_LLM_API_KEY`` | bearer token (optional for a local server). Read at call time, never stored, printed or logged |
| ``WAPSI_LLM_MODEL`` | the served model name, e.g. ``Gemma-4-31B-IT`` |
| ``WAPSI_LLM_JSON_MODE`` | ``json_object`` (default: the Token Factory has no strict ``json_schema`` mode), ``json_schema`` or ``none`` (send no ``response_format``; the prompt asks for JSON) |
| ``WAPSI_LLM_EXTRA_BODY`` | a JSON object merged into every request, e.g. ``{"reasoning_effort": "low"}`` for a thinking model |
| ``WAPSI_LLM_TIMEOUT_S`` | per-request timeout (default 60) |
| ``WAPSI_LLM_PLATFORM`` | where the endpoint's GPUs are, as the deployer states it (e.g. ``AMD`` for a vLLM server on AMD Developer Cloud). Inferred as ``AMD`` for ``*.amd.com`` / ``*.amd.com.cn`` hosts; never guessed otherwise |

The endpoint counts as configured when the base URL and the model are set.

**Who wrote the prose.** Every draft records the name of the model that wrote its prose (``Draft.llm_model``, also
sealed into the audit chain by ``draft_created``). ``served_name`` builds that name so it carries the endpoint:
``Gemma-4-31B-IT on AMD @ developer.amd.com.cn``; a replayed recording adds ``(recorded 2026-10-14)``.
``parse_served`` reads it back (the UI's status chip). Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wapsi.adapters.llm_openai_compat import API_KEY_ENV, JSON_MODES, OpenAICompatLLM, endpoint_host

BASE_URL_ENV = "WAPSI_LLM_BASE_URL"
MODEL_ENV = "WAPSI_LLM_MODEL"
JSON_MODE_ENV = "WAPSI_LLM_JSON_MODE"
TIMEOUT_ENV = "WAPSI_LLM_TIMEOUT_S"
EXTRA_BODY_ENV = "WAPSI_LLM_EXTRA_BODY"
PLATFORM_ENV = "WAPSI_LLM_PLATFORM"
ENDPOINT_ENVS = (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV)
DEFAULT_JSON_MODE = "json_object"
DEFAULT_TIMEOUT_S = 60.0
BACKEND = "openai-compatible"
# hosts whose models run on AMD GPUs by the provider's own statement (the AMD Token Factory / Radeon Cloud)
AMD_HOST_SUFFIXES = ("amd.com", "amd.com.cn")

SETUP_MESSAGE = (
    "The OpenAI-compatible endpoint is not configured. Set, in your shell (never in a committed file):\n"
    f"  {BASE_URL_ENV}   e.g. https://developer.amd.com.cn/radeon/api/v1 (AMD Token Factory) or your vLLM URL\n"
    f"  {API_KEY_ENV}    the bearer token (not needed for a local server)\n"
    f"  {MODEL_ENV}      e.g. Gemma-4-31B-IT"
)


class EndpointNotConfigured(ValueError):
    """``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_MODEL`` are not set."""


def platform_for(host: str | None, declared: str | None = None) -> str | None:
    """Where the endpoint's GPUs are: the deployer's statement (``WAPSI_LLM_PLATFORM``) if there is one, ``AMD`` for
    AMD's own hosts, else ``None`` (never guessed)."""
    declared = (declared or "").strip()
    if declared:
        return declared
    name = (host or "").split(":")[0].lower()
    if any(name == s or name.endswith("." + s) for s in AMD_HOST_SUFFIXES):
        return "AMD"
    return None


@dataclass(frozen=True)
class EndpointConfig:
    """The endpoint as the environment describes it. Holds no secret: only whether a key is set."""

    base_url: str | None = None
    model: str | None = None
    has_key: bool = False
    json_mode: str = DEFAULT_JSON_MODE
    timeout_s: float = DEFAULT_TIMEOUT_S
    platform: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)  # merged into every request (WAPSI_LLM_EXTRA_BODY)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def host(self) -> str | None:
        return endpoint_host(self.base_url) or None if self.base_url else None

    @property
    def missing(self) -> tuple[str, ...]:
        """The variables still to set. The key is listed only while the endpoint is not configured: a local server
        needs none."""
        out = []
        if not self.base_url:
            out.append(BASE_URL_ENV)
        if not self.has_key and not self.configured:
            out.append(API_KEY_ENV)
        if not self.model:
            out.append(MODEL_ENV)
        return tuple(out)

    @property
    def served_name(self) -> str:
        """The name drafts record for this endpoint (``served_name``)."""
        return served_name(self.model or "model", self.host, self.platform)

    def describe(self) -> dict[str, Any]:
        """Non-secret view (logs, health pages, reports)."""
        return {"configured": self.configured, "host": self.host, "model": self.model, "platform": self.platform,
                "api_key_set": self.has_key, "json_mode": self.json_mode, "timeout_s": self.timeout_s,
                "extra_body_fields": sorted(self.extra_body)}


def endpoint_config(env: Mapping[str, str] | None = None) -> EndpointConfig:
    env = os.environ if env is None else env

    def opt(key: str) -> str | None:
        return (env.get(key) or "").strip() or None

    json_mode = opt(JSON_MODE_ENV) or DEFAULT_JSON_MODE
    if json_mode not in JSON_MODES:
        raise ValueError(f"{JSON_MODE_ENV} must be one of {', '.join(JSON_MODES)}")
    try:
        extra = json.loads(opt(EXTRA_BODY_ENV) or "{}")
    except ValueError as exc:
        raise ValueError(f"{EXTRA_BODY_ENV} must be a JSON object") from exc
    if not isinstance(extra, dict):
        raise ValueError(f"{EXTRA_BODY_ENV} must be a JSON object")
    try:
        timeout = float(opt(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    except ValueError as exc:
        raise ValueError(f"{TIMEOUT_ENV} must be a number of seconds") from exc
    base_url = opt(BASE_URL_ENV)
    host = endpoint_host(base_url) if base_url else None
    return EndpointConfig(base_url=base_url, model=opt(MODEL_ENV), has_key=bool(opt(API_KEY_ENV)),
                          json_mode=json_mode, timeout_s=timeout, platform=platform_for(host, opt(PLATFORM_ENV)),
                          extra_body=extra)


def make_endpoint_llm(cfg: EndpointConfig | None = None, *, env: Mapping[str, str] | None = None,
                      max_retries: int = 0, timeout: float | None = None, **kwargs: Any) -> OpenAICompatLLM:
    """The raw adapter for the configured endpoint (``max_retries=0``: a ``ResilientLLM`` around it owns the retry
    policy). ``kwargs`` go to ``OpenAICompatLLM`` (``transport=`` in tests). Raises ``EndpointNotConfigured``."""
    cfg = cfg or endpoint_config(env)
    if not cfg.configured:
        raise EndpointNotConfigured(SETUP_MESSAGE)
    key = ((os.environ if env is None else env).get(API_KEY_ENV) or "").strip() or None
    kwargs.setdefault("json_mode", cfg.json_mode)
    if cfg.extra_body:
        kwargs.setdefault("extra_body", cfg.extra_body)
    kwargs.setdefault("name", cfg.served_name)
    # api_key="" (not None) when there is none: the adapter must not pick up another process-wide key
    return OpenAICompatLLM(base_url=cfg.base_url or "", model=cfg.model or "", api_key=key or "",
                           timeout=cfg.timeout_s if timeout is None else timeout, max_retries=max_retries, **kwargs)


def resilient_endpoint_llm(cfg: EndpointConfig | None = None, *, env: Mapping[str, str] | None = None,
                           cache_dir: str | Path | None = None, rpm: int | None = None, rpd: int | None = 0,
                           inner: Any | None = None, **kwargs: Any):
    """The configured endpoint behind ``ResilientLLM``: prompt-hash cache (kept apart per endpoint host), RPM
    limiting, 429 / 503 retries that honour ``Retry-After``, token and cost accounting. ``rpd=0``: the endpoint
    enforces its own daily cap. ``inner`` replaces the adapter (tests); ``kwargs`` go to ``ResilientLLM``."""
    from wapsi.adapters.llm_resilience import ResilientLLM

    cfg = cfg or endpoint_config(env)
    inner = inner if inner is not None else make_endpoint_llm(cfg, env=env)
    kwargs.setdefault("name", cfg.served_name)
    return ResilientLLM(inner, model_id=cfg.model, backend=BACKEND, rpm=rpm, rpd=rpd, cache_dir=cache_dir,
                        cache_namespace=cfg.host, **kwargs)


# --------------------------------------------------------------------------- who wrote the prose

RECORDED_NOTE = "recorded"
_NOTE = re.compile(r"^(?P<base>.*?) \((?P<note>" + RECORDED_NOTE + r"[^()]*)\)$")
_SERVED = re.compile(r"^(?P<model>.+?)(?: on (?P<platform>[^@()]+?))? @ (?P<host>[^\s@()]+)$")
_GEMMA = re.compile(r"(?i)\bgemma[-_ ]?(\d+)(n)?\b")
_STUBS = ("fake-llm", "offline demo model", "offline stub")


def served_name(model: str, host: str | None, platform: str | None = None, *, recorded_on: str | None = None) -> str:
    """``Gemma-4-31B-IT on AMD @ developer.amd.com.cn`` (``... (recorded 2026-10-14)`` for a replay). Without a host
    the model name is returned as is."""
    if not host:
        return model
    name = f"{model} on {platform} @ {host}" if platform else f"{model} @ {host}"
    return f"{name} ({RECORDED_NOTE} {recorded_on})" if recorded_on else name


def model_family(model: str) -> str:
    """A short display name: ``Gemma-4-31B-IT`` / ``google/gemma-4-E4B-it`` -> ``Gemma 4``; other ids unchanged
    (without a ``vendor/`` prefix)."""
    m = _GEMMA.search(model)
    if m:
        return f"Gemma {m.group(1)}{m.group(2) or ''}"
    return model.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Writer:
    """Who wrote a draft's prose, read from the name the draft recorded.

    ``kind``: ``endpoint`` (an OpenAI-compatible endpoint, live), ``recorded`` (the same, replayed from a
    recording), ``stub`` (the offline FakeLLM), ``template`` (no model: the deterministic paragraph) or ``other``
    (a name in another format, e.g. ``gemini-3.8-flash`` or ``vllm-rocm:wapsi-llm``)."""

    kind: str
    name: str
    model: str | None = None
    host: str | None = None
    platform: str | None = None
    recorded_on: str | None = None

    @property
    def label(self) -> str:
        """``Gemma 4 on AMD via developer.amd.com.cn``."""
        if self.kind == "stub":
            return "Offline stub (no model call)"
        if self.kind == "template":
            return "Fixed paragraph (no model)"
        if self.model is None:
            return self.name
        text = model_family(self.model)
        if self.platform:
            text += f" on {self.platform}"
        return f"{text} via {self.host}" if self.host else text


def parse_served(name: str | None) -> Writer | None:
    """Read a ``Draft.llm_model`` back. ``None`` for a draft without LLM prose."""
    raw = (name or "").strip()
    if not raw:
        return None
    base = raw.removesuffix("+resilient")
    low = base.lower()
    if low.startswith(_STUBS):
        return Writer("stub", raw)
    if low == "template-fallback":
        return Writer("template", raw)
    noted = _NOTE.match(base)
    recorded_on = None
    if noted:  # a replay: "<live writer> (recorded <date>)"
        base = noted.group("base").removesuffix("+resilient")
        recorded_on = noted.group("note").removeprefix(RECORDED_NOTE).strip() or None
    kind = "recorded" if noted else "endpoint"
    m = _SERVED.match(base)
    if m:
        return Writer(kind, raw, model=m.group("model").strip(), host=m.group("host"),
                      platform=(m.group("platform") or "").strip() or None, recorded_on=recorded_on)
    if base.lower().startswith("vllm-rocm:"):  # the AMD edition's own vLLM-ROCm service
        return Writer("recorded" if noted else "other", raw, model=base.split(":", 1)[1],
                      platform="AMD (vLLM-ROCm)", recorded_on=recorded_on)
    return Writer("recorded" if noted else "other", raw, model=base, recorded_on=recorded_on)
