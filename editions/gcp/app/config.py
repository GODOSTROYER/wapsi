"""Environment configuration for the Google Cloud edition.

| Variable | Meaning | Default |
|---|---|---|
| ``WAPSI_DEMO_MODE`` | ``1`` = zero cloud access (showcase/Parquet ledger, in-memory store, FakeLLM, scripted agent model) | ``1`` unless ``WAPSI_PROJECT`` is set |
| ``WAPSI_PROJECT`` | Google Cloud project id | — |
| ``WAPSI_LOCATION`` | Gemini (Agent Platform) location | ``global`` |
| ``WAPSI_REGION`` | Cloud Run / BigQuery / Firestore / Model Armor / Memory Bank region | ``asia-south1`` |
| ``WAPSI_BQ_DATASET`` | BigQuery dataset holding the ledger tables | ``wapsi`` |
| ``WAPSI_MODEL`` | agent + intake model | ``gemini-3.5-flash`` |
| ``WAPSI_DRAFT_MODEL`` | model for draft prose (the fact sheet) | ``WAPSI_MODEL`` |
| ``WAPSI_PRIVATE_LLM_URL`` | Gemma 4 on Cloud Run GPU (vLLM); when set, draft prose runs there | — |
| ``WAPSI_PRIVATE_LLM_MODEL`` | model id served by vLLM | ``google/gemma-4-E4B-it`` |
| ``WAPSI_MODEL_ARMOR_TEMPLATE`` | template id or ``projects/P/locations/L/templates/T`` | — (RulesGuard only) |
| ``WAPSI_MEMORY_BANK_ID`` | Agent Runtime (reasoning engine) id for Memory Bank and sessions | — (in-memory) |
| ``WAPSI_FIRESTORE_DATABASE`` / ``WAPSI_CASES_COLLECTION`` | Firestore database / collection | ``(default)`` / ``cases`` |
| ``WAPSI_WORLD`` | demo mode: path of a generated Parquet world | — (showcase fixture) |
| ``WAPSI_SUPERVISORS`` / ``WAPSI_AUDITORS`` | comma-separated IAP emails granted those roles | — |
| ``WAPSI_ROLE_SIGNING_KEY`` | HMAC key for role grants in session state (Secret Manager) | random per process |
| ``WAPSI_ALLOW_ORIGINS`` | CORS origins for the API | — |
| ``WAPSI_ADK_WEB`` | serve the ADK dev UI from the API service | ``1`` in demo mode |
| ``WAPSI_TRUST_ROLE_HEADER`` | accept the ``X-Wapsi-Role`` header without IAP (public judging demo on synthetic data only) | ``0`` |
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

log = logging.getLogger("wapsi.gcp.config")

TRUE = {"1", "true", "yes", "on"}


def agent_runtime_sdk_available() -> bool:
    """Memory Bank and Agent Runtime sessions need ``google-cloud-aiplatform`` (``google-adk[gcp]``)."""
    try:
        return importlib.util.find_spec("vertexai") is not None
    except (ImportError, ValueError):
        return False


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in TRUE


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class GcpSettings:
    demo_mode: bool = True
    project: str | None = None
    location: str = "global"
    region: str = "asia-south1"
    bq_dataset: str = "wapsi"
    model: str = "gemini-3.5-flash"
    draft_model: str | None = None
    private_llm_url: str | None = None
    private_llm_model: str = "google/gemma-4-E4B-it"
    model_armor_template: str | None = None
    memory_bank_id: str | None = None
    firestore_database: str | None = None
    cases_collection: str = "cases"
    world_path: str | None = None
    supervisors: tuple[str, ...] = ()
    auditors: tuple[str, ...] = ()
    role_signing_key: str | None = field(default=None, repr=False)
    allow_origins: tuple[str, ...] = ()
    serve_adk_web: bool = True
    trust_role_header: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GcpSettings:
        e = os.environ if env is None else env
        project = (e.get("WAPSI_PROJECT") or "").strip() or None
        demo = _flag(e.get("WAPSI_DEMO_MODE"), default=project is None)
        return cls(
            demo_mode=demo,
            project=project,
            location=(e.get("WAPSI_LOCATION") or "global").strip(),
            region=(e.get("WAPSI_REGION") or "asia-south1").strip(),
            bq_dataset=(e.get("WAPSI_BQ_DATASET") or "wapsi").strip(),
            model=(e.get("WAPSI_MODEL") or "gemini-3.5-flash").strip(),
            draft_model=(e.get("WAPSI_DRAFT_MODEL") or "").strip() or None,
            private_llm_url=(e.get("WAPSI_PRIVATE_LLM_URL") or "").strip() or None,
            private_llm_model=(e.get("WAPSI_PRIVATE_LLM_MODEL") or "google/gemma-4-E4B-it").strip(),
            model_armor_template=(e.get("WAPSI_MODEL_ARMOR_TEMPLATE") or "").strip() or None,
            memory_bank_id=(e.get("WAPSI_MEMORY_BANK_ID") or "").strip() or None,
            firestore_database=(e.get("WAPSI_FIRESTORE_DATABASE") or "").strip() or None,
            cases_collection=(e.get("WAPSI_CASES_COLLECTION") or "cases").strip(),
            world_path=(e.get("WAPSI_WORLD") or "").strip() or None,
            supervisors=_csv(e.get("WAPSI_SUPERVISORS")),
            auditors=_csv(e.get("WAPSI_AUDITORS")),
            role_signing_key=(e.get("WAPSI_ROLE_SIGNING_KEY") or "").strip() or None,
            allow_origins=tuple(o.strip() for o in (e.get("WAPSI_ALLOW_ORIGINS") or "").split(",") if o.strip()),
            serve_adk_web=_flag(e.get("WAPSI_ADK_WEB"), default=demo),
            trust_role_header=_flag(e.get("WAPSI_TRUST_ROLE_HEADER"), default=False),
        )

    def validate(self) -> GcpSettings:
        if not self.demo_mode and not self.project:
            raise ValueError("WAPSI_PROJECT is required when WAPSI_DEMO_MODE is off")
        return self

    # ------------------------------------------------------------------ derived values
    def model_armor(self) -> tuple[str, str, str] | None:
        """(project, location, template_id) or None."""
        t = self.model_armor_template
        if not t:
            return None
        parts = t.split("/")
        if len(parts) == 6 and parts[0] == "projects" and parts[2] == "locations" and parts[4] == "templates":
            return parts[1], parts[3], parts[5]
        if "/" in t:
            raise ValueError(f"WAPSI_MODEL_ARMOR_TEMPLATE is not a template id or resource name: {t!r}")
        if not self.project:
            raise ValueError("WAPSI_PROJECT is required for a short WAPSI_MODEL_ARMOR_TEMPLATE")
        return self.project, self.region, t

    def agent_engine_resource(self) -> str | None:
        """Full Agent Runtime resource name (the short id form would inherit the *global* Gemini location)."""
        if not self.memory_bank_id:
            return None
        if self.memory_bank_id.startswith("projects/"):
            return self.memory_bank_id
        if not self.project:
            raise ValueError("WAPSI_PROJECT is required for WAPSI_MEMORY_BANK_ID")
        return f"projects/{self.project}/locations/{self.region}/reasoningEngines/{self.memory_bank_id}"

    def memory_service_uri(self) -> str | None:
        res = None if self.demo_mode else self.agent_engine_resource()
        if res and not agent_runtime_sdk_available():
            log.warning("WAPSI_MEMORY_BANK_ID is set but google-cloud-aiplatform is not installed; "
                        "using in-memory sessions and memory")
            return None
        return f"agentengine://{res}" if res else None

    def session_service_uri(self) -> str | None:
        # Agent Runtime sessions persist across Cloud Run instances; demo mode keeps sessions in memory.
        return self.memory_service_uri()

    def public_summary(self) -> dict[str, object]:
        """Safe to expose on /healthz (no secrets, no emails)."""
        return {
            "demo_mode": self.demo_mode,
            "project_set": bool(self.project),
            "location": self.location,
            "region": self.region,
            "model": self.model if not self.demo_mode else "demo-scripted",
            "ledger": "bigquery" if not self.demo_mode else ("parquet" if self.world_path else "showcase"),
            "case_store": "firestore" if not self.demo_mode else "memory",
            "guard": "model_armor+rules" if (self.model_armor_template and not self.demo_mode) else "rules",
            "private_llm": bool(self.private_llm_url) and not self.demo_mode,
            "memory": "memory_bank" if self.memory_service_uri() else "in_memory",
        }


def load_settings(env: Mapping[str, str] | None = None) -> GcpSettings:
    return GcpSettings.from_env(env).validate()
