"""AMD edition settings, read from the environment (``docker-compose.yml`` / ``.env``).

| Variable | Default | Meaning |
|---|---|---|
| ``WAPSI_DEMO_MODE`` | ``0`` | ``1`` = offline ``FakeLLM``, no GPU, no GNN (runs on any laptop) |
| ``WAPSI_LLM_BASE_URL`` | ``http://llm:8000/v1`` | vLLM OpenAI-compatible endpoint |
| ``WAPSI_LLM_MODEL`` | ``wapsi-llm`` | served model name (``--served-model-name``) |
| ``WAPSI_LLM_API_KEY`` | unset | bearer token for vLLM (``VLLM_API_KEY``); read by ``OpenAICompatLLM`` |
| ``WAPSI_LLM_TIMEOUT_S`` | ``120`` | per-request timeout |
| ``WAPSI_LLM_MODELS`` | unset | ordered, comma-separated models on the same endpoint the drafting prose fails over along while ``WAPSI_LLM_MODEL`` is withdrawn or answers in garbage (``WAPSI_LLM_FALLBACK_MODELS`` is the same list); every draft names the model that wrote it |
| ``WAPSI_LLM_JSON_MODE`` | by endpoint | ``json_schema`` or ``json_object``. Unset: ``json_schema`` for a server of this stack (the compose ``llm`` service, a loopback address: vLLM guided decoding), ``json_object`` for any other host (a provider endpoint such as the AMD Token Factory serves no strict JSON schema) |
| ``WAPSI_VERIFIER_BASE_URL`` | unset | second vLLM (``verifier-llm``) used as an LLM judge; unset = rules only |
| ``WAPSI_VERIFIER_MODEL`` | ``wapsi-verifier`` | served name of the verifier model |
| ``WAPSI_VERIFIER_API_KEY`` | ``WAPSI_LLM_API_KEY`` | bearer token for the verifier |
| ``WAPSI_GNN_URL`` | unset | GNN scoring service base URL (``http://gnn:8081``); unset = heuristic scorer |
| ``WAPSI_GNN_TIMEOUT_S`` | ``5`` | GNN request timeout |
| ``WAPSI_WORLD`` | unset | Parquet world directory; unset or missing = in-memory showcase ledger |
| ``WAPSI_STATE_DIR`` | ``~/.wapsi/amd-state`` | ``FileCaseStore`` root |
| ``WAPSI_API_TOKEN`` | unset | if set, POST endpoints require ``Authorization: Bearer <token>`` |
| ``WAPSI_UI_SEED_LIMIT`` | ``3`` | cases the UI processes when the store is empty (0 = none) |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TRUE = {"1", "true", "yes", "on"}
JSON_MODES = ("json_schema", "json_object")


def is_stack_server(base_url: str) -> bool:
    """True for a model server of this stack: a loopback address, or a bare service name such as the compose
    file's ``llm`` (vLLM-ROCm next to the app). Any other host is somebody's endpoint (the AMD Token Factory, a
    vLLM behind a public URL)."""
    host = (urlsplit(base_url if "://" in base_url else f"//{base_url}").hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal") or "." not in host


def default_json_mode(base_url: str) -> str:
    """``json_schema`` for a server of this stack (vLLM guided decoding); ``json_object`` for any other host. A
    provider endpoint (the AMD Token Factory) answers HTTP 400 to a strict JSON schema, and Wapsi validates the
    answer itself either way."""
    return "json_schema" if is_stack_server(base_url) else "json_object"


def _flag(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    return default if raw is None or raw.strip() == "" else raw.strip().lower() in TRUE


def _opt(env: Mapping[str, str], key: str) -> str | None:
    raw = (env.get(key) or "").strip()
    return raw or None


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    return int(_float(env, key, float(default)))


def _fallbacks(env: Mapping[str, str], model: str) -> tuple[str, ...]:
    """The other models of ``WAPSI_LLM_MODELS`` (else ``WAPSI_LLM_FALLBACK_MODELS``), in order."""
    raw = (env.get("WAPSI_LLM_MODELS") or env.get("WAPSI_LLM_FALLBACK_MODELS") or "").strip()
    return tuple(dict.fromkeys(m.strip() for m in raw.split(",") if m.strip() and m.strip() != model))


@dataclass(frozen=True)
class AmdSettings:
    demo_mode: bool = False
    llm_base_url: str = "http://llm:8000/v1"
    llm_model: str = "wapsi-llm"
    llm_timeout_s: float = 120.0
    llm_json_mode: str = "json_schema"
    llm_fallback_models: tuple[str, ...] = ()  # WAPSI_LLM_MODELS minus llm_model, in order
    verifier_base_url: str | None = None
    verifier_model: str = "wapsi-verifier"
    verifier_api_key: str | None = None
    gnn_url: str | None = None
    gnn_timeout_s: float = 5.0
    world_path: str | None = None
    state_dir: str = str(Path.home() / ".wapsi" / "amd-state")
    api_token: str | None = None
    ui_seed_limit: int = 3

    def describe(self) -> dict[str, object]:
        """Non-secret view for /healthz and logs."""
        return {
            "demo_mode": self.demo_mode,
            "llm_base_url": None if self.demo_mode else self.llm_base_url,
            "llm_model": "fake-llm" if self.demo_mode else self.llm_model,
            "llm_fallback_models": [] if self.demo_mode else list(self.llm_fallback_models),
            "verifier": None if (self.demo_mode or not self.verifier_base_url) else self.verifier_model,
            "gnn_url": self.gnn_url,
            "world": self.world_path or "showcase (in-memory)",
            "api_token_required": bool(self.api_token),
        }


def load_settings(env: Mapping[str, str] | None = None) -> AmdSettings:
    env = os.environ if env is None else env
    base_url = _opt(env, "WAPSI_LLM_BASE_URL") or AmdSettings.llm_base_url
    json_mode = (env.get("WAPSI_LLM_JSON_MODE") or "").strip() or default_json_mode(base_url)
    if json_mode not in JSON_MODES:
        raise ValueError("WAPSI_LLM_JSON_MODE must be json_schema or json_object")
    return AmdSettings(
        demo_mode=_flag(env, "WAPSI_DEMO_MODE"),
        llm_base_url=base_url,
        llm_model=_opt(env, "WAPSI_LLM_MODEL") or AmdSettings.llm_model,
        llm_timeout_s=_float(env, "WAPSI_LLM_TIMEOUT_S", AmdSettings.llm_timeout_s),
        llm_json_mode=json_mode,
        llm_fallback_models=_fallbacks(env, _opt(env, "WAPSI_LLM_MODEL") or AmdSettings.llm_model),
        verifier_base_url=_opt(env, "WAPSI_VERIFIER_BASE_URL"),
        verifier_model=_opt(env, "WAPSI_VERIFIER_MODEL") or AmdSettings.verifier_model,
        verifier_api_key=_opt(env, "WAPSI_VERIFIER_API_KEY") or _opt(env, "WAPSI_LLM_API_KEY"),
        gnn_url=_opt(env, "WAPSI_GNN_URL"),
        gnn_timeout_s=_float(env, "WAPSI_GNN_TIMEOUT_S", AmdSettings.gnn_timeout_s),
        world_path=_opt(env, "WAPSI_WORLD"),
        state_dir=_opt(env, "WAPSI_STATE_DIR") or AmdSettings.state_dir,
        api_token=_opt(env, "WAPSI_API_TOKEN"),
        ui_seed_limit=max(0, _int(env, "WAPSI_UI_SEED_LIMIT", AmdSettings.ui_seed_limit)),
    )
