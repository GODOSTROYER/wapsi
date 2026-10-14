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
| ``WAPSI_LLM_PLATFORM`` | where the endpoint's GPUs are, as the deployer states it (e.g. ``AMD`` for a vLLM server on AMD Developer Cloud). Inferred as ``AMD`` for ``*.amd.com`` / ``*.amd.com.cn`` hosts; never guessed otherwise. ``WAPSI_LLM_ON_AMD=1`` (the switch voice intake and the AMD compose file use) states the same thing and reads as ``AMD`` here |
| ``WAPSI_LLM_MODELS`` | ordered, comma-separated models on the same endpoint to fail over along while ``WAPSI_LLM_MODEL`` is withdrawn or answers in garbage (``chain_models``; ``WAPSI_LLM_FALLBACK_MODELS`` is the same list). Unset: no substitute, a failed answer becomes the fixed paragraph |

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
from collections.abc import Callable, Mapping
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
ON_AMD_ENV = "WAPSI_LLM_ON_AMD"
MODELS_ENV = "WAPSI_LLM_MODELS"  # the ordered failover list (voice intake, the agent and the kickoff plan read it)
FALLBACK_MODELS_ENV = "WAPSI_LLM_FALLBACK_MODELS"  # the kickoff model plan's spelling of the same list
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


def declared_platform(env: Mapping[str, str] | None = None) -> str | None:
    """Where the deployer says the endpoint's GPUs are: ``WAPSI_LLM_PLATFORM`` (a label), else ``AMD`` when
    ``WAPSI_LLM_ON_AMD`` is switched on, else ``None``. One statement, two spellings: every surface (draft chip,
    voice intake, the agent's label) reads it through here, so they cannot disagree."""
    env = os.environ if env is None else env
    label = (env.get(PLATFORM_ENV) or "").strip()
    if label:
        return label
    return "AMD" if (env.get(ON_AMD_ENV) or "").strip().lower() in ("1", "true", "yes", "on") else None


def on_amd(base_url: str | None, env: Mapping[str, str] | None = None) -> bool:
    """True when the endpoint is known to run on AMD hardware: AMD's own host (the Token Factory), or the
    deployer's statement (``declared_platform``) names AMD. Never guessed from anything else."""
    host = endpoint_host(base_url) if base_url else None
    return "amd" in (platform_for(host, declared_platform(env)) or "").lower()


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"})
OFFLINE_SUFFIXES = (".offline", ".test", ".invalid", ".localhost", ".example")  # in-process fakes, never a network
SHARED_ENDPOINT_RPM = 18  # the AMD Token Factory allows 20 a minute per team: keep two in hand for teammates
SHARED_ENDPOINT_GAP_S = 3.0  # and space requests out, so one burst cannot earn a 60-second Retry-After


def _host_of(base_url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(base_url if "://" in base_url else f"//{base_url}").hostname or "").lower()


def is_local_server(base_url: str) -> bool:
    """True for a model server next to the app: a loopback address, or a bare service name such as the AMD compose
    file's ``llm``. Any other host is somebody's endpoint (the AMD Token Factory, a vLLM behind a public URL). This
    says where the server is, never what hardware it runs on (``self_hosted_name``)."""
    host = _host_of(base_url)
    return host in LOCAL_HOSTS or "." not in host


def is_offline_fake(base_url: str) -> bool:
    """An in-process fake endpoint (``*.offline``, ``*.test``, ...): no network, nothing to pace."""
    return _host_of(base_url).endswith(OFFLINE_SUFFIXES)


def pacing(base_url: str | None, env: Mapping[str, str] | None = None) -> tuple[int, float]:
    """(requests per minute, minimum seconds between requests) for a client of ``base_url``.

    ``WAPSI_LLM_RPM`` / ``WAPSI_LLM_MIN_INTERVAL_S`` win. Otherwise a shared endpoint (any host that is neither a
    local server nor an offline fake: the AMD Token Factory allows 20 requests a minute for the whole team) gets
    ``SHARED_ENDPOINT_RPM`` and ``SHARED_ENDPOINT_GAP_S``, and a server of one's own gets no limit."""
    env = os.environ if env is None else env
    shared = bool(base_url) and not is_local_server(base_url or "") and not is_offline_fake(base_url or "")

    def num(key: str, default: float) -> float:
        raw = (env.get(key) or "").strip()
        try:
            return max(0.0, float(raw)) if raw else default
        except ValueError as exc:
            raise ValueError(f"{key} must be a number, got {raw!r}") from exc

    rpm = int(num("WAPSI_LLM_RPM", SHARED_ENDPOINT_RPM if shared else 0))
    return rpm, num("WAPSI_LLM_MIN_INTERVAL_S", SHARED_ENDPOINT_GAP_S if shared else 0.0)


def rocm_visible() -> bool:
    """True when this machine shows an AMD ROCm GPU: the ``/dev/kfd`` device, ``amd-smi`` / ``rocm-smi`` on the PATH,
    or an already imported PyTorch built for ROCm (``torch.version.hip``). Never imports PyTorch."""
    import shutil
    import sys

    if Path("/dev/kfd").exists() or shutil.which("amd-smi") or shutil.which("rocm-smi"):
        return True
    torch = sys.modules.get("torch")
    return bool(getattr(getattr(torch, "version", None), "hip", None))


def self_hosted_name(model: str, env: Mapping[str, str] | None = None) -> str:
    """The name drafts record for a vLLM server of one's own (a local or compose-stack host): ``vllm-rocm:<model>``
    only when AMD is confirmed (the deployer's ``WAPSI_LLM_PLATFORM=AMD`` / ``WAPSI_LLM_ON_AMD=1``, which the AMD
    compose file sets, or ROCm visible on this machine), else ``vllm:<model>``. Never guessed from the host name."""
    amd = "amd" in (declared_platform(env) or "").lower() or rocm_visible()
    return f"{'vllm-rocm' if amd else 'vllm'}:{model}"


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
                          json_mode=json_mode, timeout_s=timeout, platform=platform_for(host, declared_platform(env)),
                          extra_body=extra)


def chain_models(cfg: EndpointConfig | None = None, env: Mapping[str, str] | None = None) -> list[str]:
    """The models to fail over along, in order: ``WAPSI_LLM_MODEL`` first, then the others of ``WAPSI_LLM_MODELS``
    (else ``WAPSI_LLM_FALLBACK_MODELS``). Only the configured model when no list is set: a named model is never
    swapped for another unless the deployer lists the substitutes, and every draft names the model that wrote it."""
    env = os.environ if env is None else env
    cfg = cfg or endpoint_config(env)
    raw = (env.get(MODELS_ENV) or env.get(FALLBACK_MODELS_ENV) or "").strip()
    names = ([cfg.model] if cfg.model else []) + [n.strip() for n in raw.split(",") if n.strip()]
    return list(dict.fromkeys(names))


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
                           inner: Any | None = None, failover: bool = False,
                           make_inner: Callable[[EndpointConfig], Any] | None = None, **kwargs: Any):
    """The configured endpoint behind ``ResilientLLM``: prompt-hash cache (kept apart per endpoint host), RPM
    limiting, 429 / 503 retries that honour ``Retry-After``, token and cost accounting. ``rpd=0``: the endpoint
    enforces its own daily cap. ``inner`` replaces the adapter (tests); ``kwargs`` go to ``ResilientLLM``.

    ``failover=True`` with a model list (``chain_models``: ``WAPSI_LLM_MODELS``): one ``ResilientLLM`` per model, all
    sharing one rate limiter (the endpoint's limit is per account), behind ``wapsi.adapters.llm_failover.FailoverLLM``:
    a model that answers in garbage or is withdrawn is skipped for a while and the next one answers. ``make_inner``
    builds the adapter of each further model (tests); default ``make_endpoint_llm``."""
    from dataclasses import replace

    from wapsi.adapters.llm_resilience import ResilientLLM

    cfg = cfg or endpoint_config(env)
    inner = inner if inner is not None else make_endpoint_llm(cfg, env=env)
    kwargs.setdefault("name", cfg.served_name)
    first = ResilientLLM(inner, model_id=cfg.model, backend=BACKEND, rpm=rpm, rpd=rpd, cache_dir=cache_dir,
                         cache_namespace=cfg.host, **kwargs)
    models = chain_models(cfg, env) if failover else [cfg.model]
    if len(models) <= 1:
        return first
    from wapsi.adapters.llm_failover import FailoverLLM

    members = [first]
    for model in models[1:]:
        other = replace(cfg, model=model)
        adapter = make_inner(other) if make_inner is not None else make_endpoint_llm(other, env=env)
        members.append(ResilientLLM(adapter, model_id=model, backend=BACKEND, rpm=rpm, rpd=rpd, cache_dir=cache_dir,
                                    cache_namespace=cfg.host,
                                    **{**kwargs, "name": other.served_name, "limiter": first.limiter}))
    return FailoverLLM(members)


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
    if base.lower().startswith("vllm-rocm:"):  # a vLLM server of one's own, AMD confirmed (self_hosted_name)
        return Writer("recorded" if noted else "other", raw, model=base.split(":", 1)[1],
                      platform="AMD (vLLM-ROCm)", recorded_on=recorded_on)
    if base.lower().startswith("vllm:"):  # a vLLM server of one's own; its hardware was not confirmed
        return Writer("recorded" if noted else "other", raw, model=base.split(":", 1)[1],
                      platform=None, recorded_on=recorded_on)
    return Writer("recorded" if noted else "other", raw, model=base, recorded_on=recorded_on)
