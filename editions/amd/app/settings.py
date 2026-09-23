"""AMD edition settings, read from the environment (``docker-compose.yml`` / ``.env``).

| Variable | Default | Meaning |
|---|---|---|
| ``WAPSI_DEMO_MODE`` | ``0`` | ``1`` = offline ``FakeLLM``, no GPU, no GNN (runs on any laptop) |
| ``WAPSI_LLM_BASE_URL`` | ``http://llm:8000/v1`` | vLLM OpenAI-compatible endpoint |
| ``WAPSI_LLM_MODEL`` | ``wapsi-llm`` | served model name (``--served-model-name``) |
| ``WAPSI_LLM_API_KEY`` | unset | bearer token for vLLM (``VLLM_API_KEY``); read by ``OpenAICompatLLM`` |
| ``WAPSI_LLM_TIMEOUT_S`` | ``120`` | per-request timeout |
| ``WAPSI_LLM_JSON_MODE`` | ``json_schema`` | ``json_schema`` or ``json_object`` |
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

TRUE = {"1", "true", "yes", "on"}


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


@dataclass(frozen=True)
class AmdSettings:
    demo_mode: bool = False
    llm_base_url: str = "http://llm:8000/v1"
    llm_model: str = "wapsi-llm"
    llm_timeout_s: float = 120.0
    llm_json_mode: str = "json_schema"
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
            "verifier": None if (self.demo_mode or not self.verifier_base_url) else self.verifier_model,
            "gnn_url": self.gnn_url,
            "world": self.world_path or "showcase (in-memory)",
            "api_token_required": bool(self.api_token),
        }


def load_settings(env: Mapping[str, str] | None = None) -> AmdSettings:
    env = os.environ if env is None else env
    json_mode = (env.get("WAPSI_LLM_JSON_MODE") or "json_schema").strip()
    if json_mode not in ("json_schema", "json_object"):
        raise ValueError("WAPSI_LLM_JSON_MODE must be json_schema or json_object")
    return AmdSettings(
        demo_mode=_flag(env, "WAPSI_DEMO_MODE"),
        llm_base_url=_opt(env, "WAPSI_LLM_BASE_URL") or AmdSettings.llm_base_url,
        llm_model=_opt(env, "WAPSI_LLM_MODEL") or AmdSettings.llm_model,
        llm_timeout_s=_float(env, "WAPSI_LLM_TIMEOUT_S", AmdSettings.llm_timeout_s),
        llm_json_mode=json_mode,
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
