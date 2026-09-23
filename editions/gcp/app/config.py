"""Environment configuration for the Google Cloud edition.

| Variable | Meaning | Default |
|---|---|---|
| ``WAPSI_DEMO_MODE`` | ``1`` = zero cloud access (showcase/Parquet ledger, in-memory store, FakeLLM, scripted agent model) | ``1`` unless ``WAPSI_PROJECT`` is set |
| ``WAPSI_PROJECT`` | Google Cloud project id | — |
| ``WAPSI_GEMINI_BACKEND`` | ``aistudio`` = Gemini API with an AI Studio key (free tier); ``vertex`` = Agent Platform (billed) | ``aistudio`` |
| ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` | the AI Studio key for ``aistudio`` (Secret Manager on Cloud Run; never logged) | — |
| ``WAPSI_LOCATION`` | Gemini location on Agent Platform (``vertex`` only) | ``global`` |
| ``WAPSI_REGION`` | Cloud Run / BigQuery / Firestore / Model Armor / Memory Bank region | ``asia-south1`` |
| ``WAPSI_MODEL`` | agent + intake model | ``gemini-3.8-flash`` |
| ``WAPSI_DRAFT_MODEL`` | model for draft prose (the fact sheet) | ``WAPSI_MODEL`` |
| ``WAPSI_LEDGER`` | ``bigquery`` or ``parquet`` (a world baked into the image or mounted from Cloud Storage) | ``bigquery`` |
| ``WAPSI_BQ_DATASET`` | BigQuery dataset holding the ledger tables | ``wapsi`` |
| ``WAPSI_WORLD`` | path of a generated Parquet world (demo mode, or ``WAPSI_LEDGER=parquet``) | — (showcase fixture) |
| ``WAPSI_ADK_STORE`` | ADK sessions + memory: ``memory`` (per instance) or ``firestore`` (durable, free tier) | ``memory`` |
| ``WAPSI_MEMORY_BANK_ID`` | Agent Runtime (reasoning engine) id for Memory Bank and sessions (paid, optional) | — (off) |
| ``WAPSI_CONTEXT_CACHE`` | ADK explicit context caching | on for ``vertex``, off for ``aistudio`` |
| ``WAPSI_OTEL_TO_CLOUD`` | export ADK OpenTelemetry to Cloud Trace / Monitoring / Logging | ``1`` in cloud mode |
| ``WAPSI_PRIVATE_LLM_URL`` | Gemma 4 on Cloud Run GPU (vLLM, paid add-on); when set, draft prose runs there | — (off) |
| ``WAPSI_PRIVATE_LLM_MODEL`` | model id served by vLLM | ``google/gemma-4-E4B-it`` |
| ``WAPSI_MODEL_ARMOR_TEMPLATE`` | template id or ``projects/P/locations/L/templates/T`` (optional) | — (RulesGuard only) |
| ``WAPSI_FIRESTORE_DATABASE`` / ``WAPSI_CASES_COLLECTION`` | Firestore database / collection | ``(default)`` / ``cases`` |
| ``WAPSI_SUPERVISORS`` / ``WAPSI_AUDITORS`` | comma-separated IAP emails granted those roles | — |
| ``WAPSI_ROLE_SIGNING_KEY`` | HMAC key for role grants in session state (Secret Manager) | random per process |
| ``WAPSI_ALLOW_ORIGINS`` | CORS origins for the API | — |
| ``WAPSI_ADK_WEB`` | serve the ADK dev UI from the API service | ``1`` in demo mode |
| ``WAPSI_TRUST_ROLE_HEADER`` | accept the ``X-Wapsi-Role`` header without IAP (public judging demo on synthetic data only) | ``0`` |

The Gemini API key is read by ``wapsi.adapters.gemini_llm.gemini_api_key`` when a client is built; it is never
stored in ``GcpSettings`` and never appears in ``public_summary``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from wapsi.adapters.gemini_llm import DEFAULT_MODEL, gemini_api_key, resolve_backend

log = logging.getLogger("wapsi.gcp.config")

TRUE = {"1", "true", "yes", "on"}
LEDGERS = ("bigquery", "parquet")
ADK_STORES = ("memory", "firestore")
FIRESTORE_URI = "firestore://"


def _choice(name: str, value: str | None, choices: tuple[str, ...], default: str) -> str:
    v = (value or default).strip().lower()
    if v not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)} (got {v!r})")
    return v


def _opt_flag(value: str | None) -> bool | None:
    return None if value is None or not value.strip() else value.strip().lower() in TRUE


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
    gemini_backend: str = "aistudio"
    location: str = "global"
    region: str = "asia-south1"
    bq_dataset: str = "wapsi"
    model: str = DEFAULT_MODEL
    draft_model: str | None = None
    ledger: str = "bigquery"
    adk_store: str = "memory"
    context_cache: bool | None = None  # None = on for vertex, off for aistudio
    otel_to_cloud: bool | None = None  # None = on in cloud mode
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
            gemini_backend=resolve_backend(None, e),
            location=(e.get("WAPSI_LOCATION") or "global").strip(),
            region=(e.get("WAPSI_REGION") or "asia-south1").strip(),
            bq_dataset=(e.get("WAPSI_BQ_DATASET") or "wapsi").strip(),
            model=(e.get("WAPSI_MODEL") or DEFAULT_MODEL).strip(),
            draft_model=(e.get("WAPSI_DRAFT_MODEL") or "").strip() or None,
            ledger=_choice("WAPSI_LEDGER", e.get("WAPSI_LEDGER"), LEDGERS, "bigquery"),
            adk_store=_choice("WAPSI_ADK_STORE", e.get("WAPSI_ADK_STORE"), ADK_STORES, "memory"),
            context_cache=_opt_flag(e.get("WAPSI_CONTEXT_CACHE")),
            otel_to_cloud=_opt_flag(e.get("WAPSI_OTEL_TO_CLOUD")),
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
        resolve_backend(self.gemini_backend)
        _choice("WAPSI_LEDGER", self.ledger, LEDGERS, "bigquery")
        _choice("WAPSI_ADK_STORE", self.adk_store, ADK_STORES, "memory")
        if not self.demo_mode and self.ledger == "parquet" and not self.world_path:
            raise ValueError("WAPSI_LEDGER=parquet needs WAPSI_WORLD (a generated world directory)")
        return self

    # ------------------------------------------------------------------ feature switches
    def context_cache_enabled(self) -> bool:
        """ADK explicit context caching: on for Agent Platform, off for the Gemini API free tier by default."""
        if self.demo_mode:
            return False
        return self.context_cache if self.context_cache is not None else self.gemini_backend == "vertex"

    def otel_enabled(self) -> bool:
        if self.demo_mode:
            return False
        return True if self.otel_to_cloud is None else self.otel_to_cloud

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
        """Memory Bank (paid, only when ``WAPSI_MEMORY_BANK_ID`` is set), else Firestore when
        ``WAPSI_ADK_STORE=firestore``, else ``None`` (in-memory). Demo mode is always in-memory."""
        if self.demo_mode:
            return None
        res = self.agent_engine_resource()
        if res:
            if agent_runtime_sdk_available():
                return f"agentengine://{res}"
            log.warning("WAPSI_MEMORY_BANK_ID is set but google-cloud-aiplatform is not installed; "
                        "falling back to WAPSI_ADK_STORE=%s", self.adk_store)
        return FIRESTORE_URI if self.adk_store == "firestore" else None

    def session_service_uri(self) -> str | None:
        # Agent Runtime or Firestore sessions persist across Cloud Run instances; otherwise per-instance memory.
        return self.memory_service_uri()

    def memory_kind(self) -> str:
        uri = self.memory_service_uri() or ""
        return "memory_bank" if uri.startswith("agentengine://") else ("firestore" if uri else "in_memory")

    def public_summary(self) -> dict[str, object]:
        """Safe to expose on /healthz (no secrets, no emails; only whether an API key is present)."""
        cloud = not self.demo_mode
        return {
            "demo_mode": self.demo_mode,
            "project_set": bool(self.project),
            "gemini_backend": self.gemini_backend if cloud else "demo",
            "gemini_api_key_set": bool(gemini_api_key()) if cloud and self.gemini_backend == "aistudio" else None,
            "location": self.location,
            "region": self.region,
            "model": self.model if cloud else "demo-scripted",
            "ledger": self.ledger if cloud else ("parquet" if self.world_path else "showcase"),
            "case_store": "firestore" if cloud else "memory",
            "guard": "model_armor+rules" if (self.model_armor_template and cloud) else "rules",
            "private_llm": bool(self.private_llm_url) and cloud,
            "memory": self.memory_kind(),
            "context_cache": self.context_cache_enabled(),
        }


def load_settings(env: Mapping[str, str] | None = None) -> GcpSettings:
    return GcpSettings.from_env(env).validate()
