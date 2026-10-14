"""The ADK application: ``wapsi_officer`` (Gemini) with the ``wapsi_intake`` multimodal sub-agent (complaint PDFs
and screenshots via ``extract_complaint``; victims' voice notes via ``intake_voice_note``).

``build_app()`` returns an ``App`` with the trace, guard, audit, memory and quota plugins (the trace plugin
first, so the Agent trace panel sees every call, including the ones the guard blocks). In demo mode both agents run
on ``DemoScriptedLlm`` (no network). Otherwise:

- ``WAPSI_GEMINI_BACKEND=aistudio`` (default, free tier): an ADK ``Gemini`` model whose google-genai client is
  built with ``vertexai=False`` and the key from ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` (ADK 2.9
  ``Gemini.client_kwargs``, which is excluded from ``repr`` and serialization). 429s are retried by the client
  (``retry_options``) and a persistent one becomes a plain-language reply (``ModelQuotaPlugin``);
- ``WAPSI_GEMINI_BACKEND=vertex``: the model id string with ``GOOGLE_GENAI_USE_VERTEXAI`` /
  ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_LOCATION`` defaults, i.e. Gemini on Agent Platform (billed);
- ``WAPSI_AGENT_ON_PRIVATE=1`` with ``WAPSI_PRIVATE_LLM_URL``: the private Gemma 4 service through LiteLLM;
- ``WAPSI_LLM_BASE_URL`` (or ``WAPSI_AGENT_BASE_URL``) set: any OpenAI-compatible endpoint through LiteLLM
  (``editions.shared.endpoint``: AMD Token Factory, vLLM-ROCm, ...), with ``WAPSI_LLM_API_KEY`` and
  ``WAPSI_AGENT_MODEL`` / ``WAPSI_LLM_MODEL`` (default: Gemma 4). This is how the same agent runs on an open
  Google model served on AMD; the AMD edition (``editions.amd.app.agent``) wires it without any Google Cloud
  service.

The agent's tools, plugins, instruction and offline planner are edition-neutral (``editions.shared``).
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.genai import types

from editions.shared.instructions import APP_NAME, INTAKE_NAME, intake_instruction, officer_instruction
from wapsi.adapters.gemini_llm import MISSING_KEY_MESSAGE, gemini_api_key
from wapsi.agenttrace import ToolTraceRecorder

from .config import GcpSettings
from .intake import extract_complaint
from .memory import memory_tools
from .plugins import AuditLogPlugin, MemoryPlugin, ModelQuotaPlugin, VoiceUploadPlugin, WapsiGuardPlugin
from .services import get_services
from .tools import INTAKE_TOOLS, OFFICER_TOOLS
from .tracing import ToolTracePlugin, get_recorder

# Free-tier friendly retries inside the google-genai client: 4 attempts, 2 s -> 4 s -> 8 s (+ jitter), max 30 s.
GEMINI_RETRY = types.HttpRetryOptions(attempts=4, initial_delay=2.0, max_delay=30.0, exp_base=2.0,
                                      http_status_codes=[408, 429, 500, 502, 503, 504])

OFFICER_INSTRUCTION = officer_instruction(intake_agent=INTAKE_NAME, memory=True)
INTAKE_INSTRUCTION = intake_instruction(APP_NAME)


def _configure_genai_env(settings: GcpSettings) -> None:
    """Agent Platform (``vertex``) only: point ADK's default google-genai client at Vertex AI."""
    if settings.demo_mode or settings.gemini_backend != "vertex":
        return
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    if settings.project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", settings.project)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", settings.location)


def gemini_api_model(settings: GcpSettings, api_key: str | None = None) -> Any:
    """ADK ``Gemini`` on the Gemini API with an AI Studio key (free tier). Raises ``RuntimeError`` without a key."""
    from google.adk.models import Gemini

    key = api_key or gemini_api_key()
    if not key:
        raise RuntimeError(MISSING_KEY_MESSAGE)
    return Gemini(model=settings.model, client_kwargs={"vertexai": False, "api_key": key},
                  retry_options=GEMINI_RETRY)


def build_model(settings: GcpSettings) -> Any:
    """The ADK model for both agents: scripted offline model, Gemini (API key or Agent Platform), or Gemma."""
    if settings.demo_mode:
        from .demo_model import DemoScriptedLlm

        return DemoScriptedLlm()
    if os.environ.get("WAPSI_AGENT_ON_PRIVATE", "").strip() == "1" and settings.private_llm_url:
        from google.adk.models.lite_llm import LiteLlm

        base = settings.private_llm_url.rstrip("/")
        return LiteLlm(model=f"openai/{settings.private_llm_model}",
                       api_base=base if base.endswith("/v1") else f"{base}/v1")
    from editions.shared.endpoint import AgentEndpoint, build_endpoint_model

    endpoint = AgentEndpoint.from_env()
    if endpoint is not None:  # WAPSI_LLM_BASE_URL: an OpenAI-compatible endpoint (Gemma 4 on AMD, vLLM, ...)
        return build_endpoint_model(endpoint)
    if settings.gemini_backend == "aistudio":
        return gemini_api_model(settings)
    _configure_genai_env(settings)
    return settings.model


def build_agents(settings: GcpSettings | None = None) -> LlmAgent:
    settings = settings or get_services().settings
    model = build_model(settings)
    config = types.GenerateContentConfig(temperature=0.1, max_output_tokens=4096)
    intake = LlmAgent(
        name=INTAKE_NAME,
        model=model,
        description="Extracts structured complaint fields from uploaded NCRP / 1930 PDFs, screenshots and victims' "
                    "voice notes, and reconciles them with the ledger.",
        instruction=INTAKE_INSTRUCTION,
        tools=[extract_complaint, *INTAKE_TOOLS],
        generate_content_config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=2048),
    )
    return LlmAgent(
        name=APP_NAME,
        model=model,
        description="Nodal-officer copilot: traces stolen money, plans amount-specific holds, shows verified drafts.",
        instruction=OFFICER_INSTRUCTION,
        tools=[*OFFICER_TOOLS, *memory_tools(settings)],
        sub_agents=[intake],
        generate_content_config=config,
    )


def build_app(settings: GcpSettings | None = None, *, recorder: ToolTraceRecorder | None = None) -> App:
    """``settings`` defaults to the process-wide services' settings (the server installs those first);
    ``recorder`` to the process-wide agent-trace recorder."""
    settings = settings or get_services().settings
    recorder = recorder or get_recorder()
    return App(
        name=APP_NAME,
        root_agent=build_agents(settings),
        plugins=[ToolTracePlugin(recorder), WapsiGuardPlugin(recorder=recorder), AuditLogPlugin(), MemoryPlugin(),
                 ModelQuotaPlugin(), VoiceUploadPlugin()],
        # explicit context caching (each agent keeps its cached prefix across transfers) on Agent Platform only by
        # default; the Gemini API applies implicit caching on its own
        context_cache_config=ContextCacheConfig() if settings.context_cache_enabled() else None,
    )


app = build_app()
root_agent = app.root_agent
