"""Which models read a voice note: ``WAPSI_VOICE_BACKEND = auto | open | gemini | fake``.

- ``open``: a speech recogniser plus an open text LLM (``wapsi.intake.open_voice``), e.g. Whisper large-v3-turbo
  and Gemma 4 served on AMD. Needs a text LLM endpoint (``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_MODEL`` /
  ``WAPSI_LLM_API_KEY``, OpenAI-compatible) and a recogniser (see ``wapsi.intake.asr`` for ``WAPSI_ASR_*``).
- ``gemini``: one Gemini multimodal call (``GEMINI_API_KEY``, or ``WAPSI_GEMINI_BACKEND=vertex`` + a project).
- ``fake``: the offline ``FakeLLM``; it knows the bundled synthetic samples by SHA-256 and nothing else.
- ``auto`` (default): ``open`` when a text LLM endpoint is configured *and* a recogniser is available: an ASR
  endpoint (``WAPSI_ASR_BASE_URL``), a local Whisper whose weights are already on disk (auto never starts a
  multi-gigabyte download; ``WAPSI_ASR_BACKEND=local`` or ``WAPSI_VOICE_BACKEND=open`` does), or the chat endpoint
  itself when a one-off probe shows it accepts audio. Otherwise ``gemini`` when it is configured, else ``fake``.

``WAPSI_VOICE_OFFLINE=1`` (older switch) still forces ``fake`` while the backend is ``auto``.

The text LLM of the open path: ``WAPSI_VOICE_LLM_MODEL`` (default ``WAPSI_LLM_MODEL``) at ``WAPSI_LLM_BASE_URL``;
``WAPSI_VOICE_JSON_MODE`` picks the structured-output mode (see ``wapsi.intake.extract``). It is the head of a
model chain (``WAPSI_VOICE_LLM_MODELS`` / ``WAPSI_LLM_MODELS``, comma separated; on the AMD Token Factory the
default is Gemma-4-31B-IT, DeepSeek-V4-Flash, Qwen3.8-27B, MiniCPM-V-4.7-35B-A3B): a model that fails its health
probe or returns garbage is skipped for a while and the next one answers (``wapsi.intake.llm_chain``). "On AMD" in a label is
a claim about hardware, so it is only made when it is known: the AMD Token Factory host, a ROCm GPU seen by torch,
or an endpoint the operator marked with ``WAPSI_LLM_ON_AMD=1`` (``WAPSI_LLM_PLATFORM=AMD`` says the same) /
``WAPSI_ASR_ON_AMD=1``.

Nothing here prints, logs or returns a key.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from wapsi.adapters.llm_openai_compat import OpenAICompatLLM
from wapsi.intake import asr as A
from wapsi.intake.extract import JSON_MODE_ENV, JSON_MODES, TranscriptExtractor
from wapsi.intake.llm_chain import (
    COOLDOWN_ENV,
    HEALTH_TTL_ENV,
    MODELS_ENV,
    PROBE_ENV,
    VOICE_MODELS_ENV,
    ModelChain,
    chain_settings,
    model_names,
)
from wapsi.intake.open_voice import FakeTranscriptLLM, OpenVoiceLLM, friendly_model
from wapsi.intake.voice import VoiceIntakeError

VOICE_BACKEND_ENV = "WAPSI_VOICE_BACKEND"
VOICE_BACKENDS = ("auto", "open", "gemini", "fake")
VOICE_LLM_MODEL_ENV = "WAPSI_VOICE_LLM_MODEL"
LLM_BASE_URL_ENV = "WAPSI_LLM_BASE_URL"
LLM_MODEL_ENV = "WAPSI_LLM_MODEL"
LLM_API_KEY_ENV = "WAPSI_LLM_API_KEY"
LLM_TIMEOUT_ENV = "WAPSI_LLM_TIMEOUT_S"
LLM_ON_AMD_ENV = "WAPSI_LLM_ON_AMD"
LLM_PLATFORM_ENV = "WAPSI_LLM_PLATFORM"  # the label the draft chip uses (wapsi.adapters.llm_env); "AMD" = on AMD
LLM_PROVIDER_ENV = "WAPSI_LLM_PROVIDER"
OFFLINE_ENV = "WAPSI_VOICE_OFFLINE"
PROBE_CACHE_ENV = "WAPSI_ASR_PROBE_CACHE"
PROBE_TTL_S = 3600.0
TOKEN_FACTORY_MODEL = "Gemma-4-31B-IT"
OFFLINE_NAME = "offline demo model"
TRUE = {"1", "true", "yes", "on"}

# every variable that changes which model is built (``env_signature``); *_KEY values are hashed, never stored
_ENV_NAMES = (
    VOICE_BACKEND_ENV, OFFLINE_ENV, VOICE_LLM_MODEL_ENV, LLM_BASE_URL_ENV, LLM_MODEL_ENV, LLM_API_KEY_ENV,
    LLM_TIMEOUT_ENV, LLM_ON_AMD_ENV, LLM_PLATFORM_ENV, LLM_PROVIDER_ENV, JSON_MODE_ENV, "WAPSI_LLM_JSON_MODE", A.ASR_BACKEND_ENV,
    A.ASR_BASE_URL_ENV, A.ASR_API_KEY_ENV, A.ASR_MODEL_ENV, A.ASR_LANGUAGE_ENV, A.ASR_PROMPT_ENV, A.ASR_DEVICE_ENV,
    A.ASR_TIMEOUT_ENV, A.ASR_CHAT_AUDIO_ENV, A.ASR_ON_AMD_ENV, VOICE_MODELS_ENV, MODELS_ENV, COOLDOWN_ENV,
    HEALTH_TTL_ENV, PROBE_ENV, "WAPSI_VOICE_MODEL", "WAPSI_MODEL",
    "WAPSI_GEMINI_BACKEND", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "WAPSI_PROJECT",
)  # fmt: skip


class VoiceBackendError(VoiceIntakeError):
    """The requested voice backend cannot be built from this configuration (says what to set)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "NOT_CONFIGURED")


@dataclass(frozen=True)
class VoiceChoice:
    backend: str  # open | gemini | fake
    requested: str  # auto | open | gemini | fake
    asr: str = ""  # endpoint | local | chat-audio | fake ("" unless backend == "open")
    reason: str = ""  # one sentence for tooltips, /healthz and logs


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _flag(env: Mapping[str, str], key: str) -> bool:
    return _get(env, key).lower() in TRUE


def env_signature(env: Mapping[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """A hashable fingerprint of the voice configuration (cache key). Secrets are replaced by a short digest."""
    e = os.environ if env is None else env
    out = []
    for name in _ENV_NAMES:
        value = _get(e, name)
        if value and name.endswith("_KEY"):
            value = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        out.append((name, value))
    return tuple(out)


# --------------------------------------------------------------------------- where a model runs


def is_amd_host(base_url: str) -> bool:
    """The URL is on AMD's own domain (the Token Factory: models served on AMD GPUs)."""
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host in ("amd.com", "amd.com.cn") or host.endswith((".amd.com", ".amd.com.cn"))


def llm_provider(base_url: str, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """(who serves the text LLM, whether that is known to be AMD hardware)."""
    e = os.environ if env is None else env
    label = _get(e, LLM_PROVIDER_ENV)
    if is_amd_host(base_url):
        return label or "AMD Token Factory", True
    if _flag(e, LLM_ON_AMD_ENV) or "amd" in _get(e, LLM_PLATFORM_ENV).lower():  # one statement, two spellings
        return label or "vLLM-ROCm on AMD", True
    return label or (urlsplit(base_url or "").hostname or "").lower() or "text LLM endpoint", False


def _asr_on_amd(base_url: str, env: Mapping[str, str]) -> bool:
    return is_amd_host(base_url) or _flag(env, A.ASR_ON_AMD_ENV)


# --------------------------------------------------------------------------- what is available


def local_asr_installed() -> bool:
    """torch and transformers are importable (checked without importing them)."""
    try:
        return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))
    except (ImportError, ValueError):
        return False


def local_model_cached(model: str) -> bool:
    """The recogniser's weights are already on disk: a local directory, or a Hugging Face cache entry."""
    if not model:
        return False
    if Path(model).is_dir():
        return True
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    try:
        return isinstance(try_to_load_from_cache(model, "config.json"), str)
    except Exception:  # noqa: BLE001 - an odd repo id or cache layout: treat as not cached
        return False


def local_asr_ready(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return local_asr_installed() and local_model_cached(_get(e, A.ASR_MODEL_ENV) or A.DEFAULT_ASR_MODEL)


def _probe_cache_path(env: Mapping[str, str]) -> Path:
    raw = _get(env, PROBE_CACHE_ENV)
    return Path(raw) if raw else Path(tempfile.gettempdir()) / "wapsi-chat-audio-probe.json"


_PROBES: dict[str, tuple[float, bool]] = {}
_PROBE_LOCK = threading.Lock()


def chat_audio_available(
    base_url: str,
    model: str,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], float] = time.time,
) -> bool:
    """Does the chat endpoint accept audio parts for ``model``? One tiny request, remembered for an hour (in
    memory and in a small file that holds a hash of the endpoint and model, the answer and a timestamp)."""
    e = os.environ if env is None else env
    if not base_url or not model:
        return False
    key = hashlib.sha256(f"{base_url.rstrip('/')}|{model}".encode()).hexdigest()[:24]
    with _PROBE_LOCK:
        hit = _PROBES.get(key)
        if hit and now() - hit[0] < PROBE_TTL_S:
            return hit[1]
        path = _probe_cache_path(e)
        try:
            stored = json.loads(path.read_text(encoding="utf-8")).get(key)
            if stored and now() - float(stored["at"]) < PROBE_TTL_S:
                _PROBES[key] = (float(stored["at"]), bool(stored["ok"]))
                return bool(stored["ok"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        from wapsi.adapters.asr_openai_compat import ChatAudioASR

        probe = ChatAudioASR(base_url, model, api_key=_get(e, LLM_API_KEY_ENV), timeout=15.0,
                             max_retries=0, transport=transport)  # fmt: skip
        try:
            ok = probe.probe()
        finally:
            probe.close()
        _PROBES[key] = (now(), ok)
        try:
            data: dict[str, Any] = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            data[key] = {"at": now(), "ok": ok}
            path.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError):
            pass  # the in-memory answer is enough
        return ok


def reset_probe_cache() -> None:
    with _PROBE_LOCK:
        _PROBES.clear()


# --------------------------------------------------------------------------- resolution


def _voice_llm_model(env: Mapping[str, str], base_url: str) -> str:
    """The first text model to try: the chain's head when a chain is set, else the configured model."""
    model = _get(env, VOICE_LLM_MODEL_ENV) or _get(env, LLM_MODEL_ENV)
    chain = model_names(env, model, token_factory=is_amd_host(base_url))
    if _get(env, VOICE_MODELS_ENV) or _get(env, MODELS_ENV):
        return chain[0]
    return model or (TOKEN_FACTORY_MODEL if is_amd_host(base_url) else "")


def resolve_asr(
    env: Mapping[str, str] | None = None,
    *,
    explicit: bool = False,
    llm_base_url: str = "",
    llm_model: str = "",
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str]:
    """(recogniser kind, reason). Kind is ``endpoint`` / ``local`` / ``chat-audio`` / ``fake``, or ``""`` when
    none is available. ``explicit`` = the operator asked for the open path, so a local model may be downloaded."""
    e = os.environ if env is None else env
    want = (_get(e, A.ASR_BACKEND_ENV) or "auto").lower()
    if want not in A.ASR_BACKENDS:
        raise ValueError(f"{A.ASR_BACKEND_ENV} must be one of {', '.join(A.ASR_BACKENDS)} (got {want!r})")
    base = _get(e, A.ASR_BASE_URL_ENV)
    if want == "fake":
        return "fake", "the offline recogniser was requested"
    if want == "endpoint":
        if not base:
            raise VoiceBackendError(f"{A.ASR_BACKEND_ENV}=endpoint needs {A.ASR_BASE_URL_ENV}")
        return "endpoint", f"transcription endpoint {urlsplit(base).hostname or base}"
    if want == "local":
        if not local_asr_installed():
            raise VoiceBackendError(f"{A.ASR_BACKEND_ENV}=local needs torch and transformers: uv sync --extra asr")
        return "local", "local Whisper through transformers"
    if want == "chat-audio":
        if not llm_base_url:
            raise VoiceBackendError(f"{A.ASR_BACKEND_ENV}=chat-audio needs {LLM_BASE_URL_ENV}")
        return "chat-audio", "the chat endpoint transcribes the audio itself"
    if base:
        return "endpoint", f"transcription endpoint {urlsplit(base).hostname or base}"
    if local_asr_installed() and (explicit or local_asr_ready(e)):
        return "local", "local Whisper through transformers"
    mode = (_get(e, A.ASR_CHAT_AUDIO_ENV) or "probe").lower()
    if mode in ("on", "1") and llm_base_url:
        return "chat-audio", "the chat endpoint transcribes the audio itself"
    if mode == "probe" and llm_base_url and llm_model:
        if chat_audio_available(llm_base_url, llm_model, e, transport=transport):
            return "chat-audio", "the chat endpoint accepts audio (probed)"
    return "", "no speech recogniser is configured"


def _gemini_ready(env: Mapping[str, str]) -> bool:
    from wapsi.adapters.gemini_llm import gemini_api_key

    backend = (_get(env, "WAPSI_GEMINI_BACKEND") or "aistudio").lower()
    if backend == "vertex":
        return bool(_get(env, "GOOGLE_CLOUD_PROJECT") or _get(env, "WAPSI_PROJECT"))
    return bool(gemini_api_key(env))


_NEED_ASR = (
    f"set {A.ASR_BASE_URL_ENV} to an OpenAI-compatible transcription endpoint, or install the local recogniser "
    f"(uv sync --extra asr) and set {A.ASR_BACKEND_ENV}=local"
)


def resolve_voice_backend(
    env: Mapping[str, str] | None = None,
    *,
    have_text_llm: bool | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> VoiceChoice:
    """Decide the backend from the environment. ``have_text_llm`` / ``llm_base_url`` / ``llm_model`` describe a
    text LLM the caller already holds (an edition's own client); by default they come from ``WAPSI_LLM_*``.
    Raises ``VoiceBackendError`` when ``open`` is requested but cannot be built, ``ValueError`` for a bad value."""
    e = os.environ if env is None else env
    requested = (_get(e, VOICE_BACKEND_ENV) or "auto").lower()
    if requested not in VOICE_BACKENDS:
        raise ValueError(f"{VOICE_BACKEND_ENV} must be one of {', '.join(VOICE_BACKENDS)} (got {requested!r})")
    base = llm_base_url if llm_base_url is not None else _get(e, LLM_BASE_URL_ENV)
    model = llm_model if llm_model is not None else _voice_llm_model(e, base)
    text_llm = bool(base and model) if have_text_llm is None else have_text_llm

    if requested == "fake":
        return VoiceChoice("fake", requested, reason="the offline model was requested")
    if requested == "gemini":
        return VoiceChoice("gemini", requested, reason="Gemini was requested")
    if requested == "open":
        kind, why = resolve_asr(e, explicit=True, llm_base_url=base, llm_model=model, transport=transport)
        if not kind:
            raise VoiceBackendError(f"{VOICE_BACKEND_ENV}=open needs a speech recogniser: {_NEED_ASR}")
        if not text_llm and kind != "fake":
            raise VoiceBackendError(
                f"{VOICE_BACKEND_ENV}=open needs a text LLM: set {LLM_BASE_URL_ENV} and {LLM_MODEL_ENV} "
                f"(OpenAI-compatible, for example Gemma 4 on the AMD Token Factory)"
            )
        return VoiceChoice("open", requested, asr=kind, reason=why)
    if _flag(e, OFFLINE_ENV):
        return VoiceChoice("fake", requested, reason=f"{OFFLINE_ENV}=1")
    if text_llm:
        kind, why = resolve_asr(e, llm_base_url=base, llm_model=model, transport=transport)
        if kind and kind != "fake":
            return VoiceChoice("open", requested, asr=kind, reason=why)
    if _gemini_ready(e):
        return VoiceChoice("gemini", requested, reason="a Gemini key or project is configured")
    if text_llm:
        return VoiceChoice("fake", requested, reason=f"a text LLM is configured but no recogniser: {_NEED_ASR}")
    return VoiceChoice("fake", requested, reason="no voice model is configured")


# --------------------------------------------------------------------------- building


class VoiceTextLLM(OpenAICompatLLM):
    """``OpenAICompatLLM`` that also keeps the cost an endpoint reports (``usage.cost``, as the AMD Token Factory
    does) for the calling thread's latest request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cost = threading.local()

    @property
    def last_cost_usd(self) -> float | None:
        return getattr(self._cost, "value", None)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = super()._post(payload)
        usage = data.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        self._cost.value = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
        return data


class UnconfiguredVoiceModel:
    """Stands in when the requested backend cannot be built: the page still renders, and transcribing says what
    to configure."""

    backend = "unconfigured"

    def __init__(self, problem: str) -> None:
        self.name = "voice model not configured"
        self.problem = problem

    def extract_media(self, *args: Any, **kwargs: Any) -> str:
        raise VoiceBackendError(self.problem)


def _timeout(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(_get(env, key) or default)
    except ValueError:
        return default


def build_asr(
    kind: str,
    env: Mapping[str, str] | None = None,
    *,
    llm_base_url: str = "",
    llm_model: str = "",
    transport: httpx.BaseTransport | None = None,
) -> A.SpeechToText:
    e = os.environ if env is None else env
    model = _get(e, A.ASR_MODEL_ENV) or A.DEFAULT_ASR_MODEL
    language = _get(e, A.ASR_LANGUAGE_ENV) or A.DEFAULT_LANGUAGE
    prompt = _get(e, A.ASR_PROMPT_ENV) or None
    if kind == "fake":
        return A.FakeASR(language=A.requested_language(language) or "hi")
    if kind == "endpoint":
        from wapsi.adapters.asr_openai_compat import OpenAICompatASR, asr_api_key

        base = _get(e, A.ASR_BASE_URL_ENV)
        return OpenAICompatASR(
            base, model, api_key=asr_api_key(base, e) or "", language=language, prompt=prompt,
            timeout=_timeout(e, A.ASR_TIMEOUT_ENV, 120.0), on_amd=_asr_on_amd(base, e), transport=transport,
        )  # fmt: skip
    if kind == "local":
        from wapsi.adapters.asr_transformers import TransformersASR

        return TransformersASR(model, device=_get(e, A.ASR_DEVICE_ENV) or "auto", language=language, prompt=prompt)
    if kind == "chat-audio":
        from wapsi.adapters.asr_openai_compat import ChatAudioASR

        return ChatAudioASR(
            llm_base_url, llm_model, api_key=_get(e, LLM_API_KEY_ENV) or "", language=language,
            timeout=_timeout(e, A.ASR_TIMEOUT_ENV, 120.0), on_amd=llm_provider(llm_base_url, e)[1],
            transport=transport,
        )  # fmt: skip
    raise ValueError(f"unknown recogniser kind {kind!r}")


def _json_mode(env: Mapping[str, str]) -> str:
    mode = (_get(env, JSON_MODE_ENV) or "auto").lower()
    if mode not in JSON_MODES:
        raise ValueError(f"{JSON_MODE_ENV} must be one of {', '.join(JSON_MODES)} (got {mode!r})")
    return mode


def open_text_llm(
    env: Mapping[str, str] | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """One text LLM of the open path from ``WAPSI_LLM_BASE_URL`` / ``WAPSI_VOICE_LLM_MODEL`` (or
    ``WAPSI_LLM_MODEL``) / ``WAPSI_LLM_API_KEY``. One retry: the caller is an officer waiting on a page."""
    e = os.environ if env is None else env
    base = base_url if base_url is not None else _get(e, LLM_BASE_URL_ENV)
    model = model or _voice_llm_model(e, base)
    if not base or not model:
        raise VoiceBackendError(f"the open voice path needs {LLM_BASE_URL_ENV} and {LLM_MODEL_ENV}")
    mode = (_get(e, "WAPSI_LLM_JSON_MODE") or "json_object").lower()
    return VoiceTextLLM(
        base, model, api_key=_get(e, LLM_API_KEY_ENV), timeout=_timeout(e, LLM_TIMEOUT_ENV, 120.0), max_retries=1,
        json_mode=mode if mode in ("json_schema", "json_object") else "json_object", transport=transport,
    )  # fmt: skip


def text_model_chain(
    env: Mapping[str, str] | None = None,
    *,
    text_llm: Any | None = None,
    wrap: Any | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ModelChain:
    """The ordered text models of the open path. ``text_llm`` is a client the caller already holds (it keeps its
    place in the chain; the other models are built for the same endpoint with the key from the environment).
    ``wrap`` decorates every client (the benchmark's answer cache)."""
    e = os.environ if env is None else env
    base = str(getattr(text_llm, "base_url", "") or "") if text_llm is not None else _get(e, LLM_BASE_URL_ENV)
    own = str(getattr(text_llm, "model", "") or "") if text_llm is not None else ""
    primary = own or _get(e, VOICE_LLM_MODEL_ENV) or _get(e, LLM_MODEL_ENV)
    primary = primary or (TOKEN_FACTORY_MODEL if is_amd_host(base) else "")
    names = model_names(e, primary, token_factory=is_amd_host(base)) if base else [primary]
    llms: list[Any] = []
    for name in names:
        if text_llm is not None and (name == own or not base):
            llm = text_llm
        else:
            llm = open_text_llm(e, model=name, base_url=base, transport=transport)
        llms.append(wrap(llm) if wrap is not None else llm)
    if not llms:
        raise VoiceBackendError(f"the open voice path needs {LLM_BASE_URL_ENV} and {LLM_MODEL_ENV}")
    return ModelChain(llms, **chain_settings(e))


def build_voice_model(
    env: Mapping[str, str] | None = None,
    *,
    choice: VoiceChoice | None = None,
    text_llm: Any | None = None,
    asr: A.SpeechToText | None = None,
    served_by: str | None = None,
    llm_on_amd: bool | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Any:
    """The voice model for this configuration (an ``AudioLLM``). ``text_llm`` / ``asr`` inject ready adapters (an
    edition's own LLM client, a test double); ``served_by`` / ``llm_on_amd`` describe an injected LLM."""
    e = os.environ if env is None else env
    head = text_llm.primary if isinstance(text_llm, ModelChain) else text_llm
    base = str(getattr(head, "base_url", "") or "") if head is not None else _get(e, LLM_BASE_URL_ENV)
    model = str(getattr(head, "model", "") or "") if head is not None else _voice_llm_model(e, base)
    if choice is None:
        choice = resolve_voice_backend(
            e, have_text_llm=True if text_llm is not None else None, llm_base_url=base, llm_model=model,
            transport=transport,
        )  # fmt: skip
    if choice.backend == "fake":
        from wapsi.adapters.llm_fake import FakeLLM

        return FakeLLM(name=OFFLINE_NAME)
    if choice.backend == "gemini":
        from wapsi.adapters.gemini_llm import DEFAULT_MODEL, GeminiLLM

        name = _get(e, "WAPSI_VOICE_MODEL") or _get(e, "WAPSI_MODEL") or DEFAULT_MODEL
        backend = (_get(e, "WAPSI_GEMINI_BACKEND") or "aistudio").lower()
        project = _get(e, "GOOGLE_CLOUD_PROJECT") or _get(e, "WAPSI_PROJECT") or None
        if backend == "vertex":
            return GeminiLLM(name, project=project, backend="vertex", max_retries=2, timeout_s=90)
        return GeminiLLM(name, backend="aistudio", max_retries=2, timeout_s=90)
    recogniser = asr or build_asr(choice.asr, e, llm_base_url=base, llm_model=model, transport=transport)
    if isinstance(text_llm, ModelChain):
        chain = text_llm
    elif text_llm is None and choice.asr == "fake" and not base:
        chain = ModelChain([FakeTranscriptLLM()])  # the whole open pipeline offline
    else:
        chain = text_model_chain(e, text_llm=text_llm, transport=transport)
    provider = llm_provider(base, e)
    # the transcript is screened by the rules guard before the text model reads it (no model call needed)
    from wapsi.verify.injection import RulesGuard

    return OpenVoiceLLM(
        recogniser,
        TranscriptExtractor(chain, json_mode=_json_mode(e)),
        served_by=served_by if served_by is not None else provider[0],
        llm_on_amd=provider[1] if llm_on_amd is None else llm_on_amd,
        guard=RulesGuard(),
    )


def safe_voice_model(env: Mapping[str, str] | None = None, **kwargs: Any) -> Any:
    """``build_voice_model`` that never raises: a configuration problem gives an ``UnconfiguredVoiceModel``."""
    try:
        return build_voice_model(env, **kwargs)
    except (VoiceIntakeError, ValueError) as exc:
        return UnconfiguredVoiceModel(str(exc))


# --------------------------------------------------------------------------- description (labels)


def describe_voice_model(model: Any) -> dict[str, Any]:
    """Non-secret facts about a voice model for chips, ``/healthz`` and benchmark records:
    ``backend``, ``label``, ``tone`` (UI chip), ``hint`` (tooltip), plus the open path's own description."""
    if isinstance(model, OpenVoiceLLM):
        d = model.describe()
        where = f"; fields by {friendly_model(model.llm_model)} served by {model.served_by}" if model.served_by else ""
        d.update(tone="accent", hint=f"Speech-to-text: {model.asr_model} ({model.asr_backend}){where}.")
        return d
    if isinstance(model, UnconfiguredVoiceModel):
        return {"backend": "unconfigured", "label": "Voice model not configured", "tone": "warn",
                "hint": model.problem}  # fmt: skip
    name = str(getattr(model, "name", type(model).__name__))
    if name.startswith(("gemini-api:", "vertex:")):
        return {"backend": "gemini", "label": f"Gemini · {getattr(model, 'model', name.split(':', 1)[-1])}",
                "tone": "accent", "hint": "One Gemini multimodal call returns transcript, translation and fields.",
                "structured_by": str(getattr(model, "model", name))}  # fmt: skip
    return {
        "backend": "fake",
        "label": "Offline demo model · knows the bundled samples",
        "tone": "gray",
        "hint": f"To transcribe any recording on open models set {LLM_BASE_URL_ENV} (Gemma 4 on AMD) and "
        f"{A.ASR_BASE_URL_ENV} or {A.ASR_BACKEND_ENV}=local (Whisper); or set GEMINI_API_KEY.",
        "structured_by": name,
    }
