"""The AMD edition's officer agent: Google's ADK agent framework on an open Google model (Gemma 4) served on AMD.

The same ``wapsi_officer`` agent as the Google Cloud edition (``editions.shared``: tools, guard, audit and trace
plugins, instruction), wired to this edition's services and to no Google Cloud service at all:

    model      Gemma 4 behind an OpenAI-compatible endpoint, through ADK's ``LiteLlm``
               (``editions.shared.endpoint_model.EndpointLlm``): AMD Token Factory (``Gemma-4-31B-IT``, the default
               path), vLLM-ROCm on an MI300X droplet (``http://llm:8000/v1``), or a notebook-hosted vLLM
    tools      the ten officer tools on ``ParquetLedger`` + ``FileCaseStore`` + ``LocalDocSearch`` + this
               edition's guard (``RulesGuard`` or rules + verifier LLM) and scorer (heuristic or GNN)
    traces     every run sealed into the case's hash-chained audit log by ``FileCaseStore.append_audit``

Backends (``WAPSI_AGENT_BACKEND``):
    auto       (default) ``endpoint`` normally, ``offline`` in demo mode (``WAPSI_DEMO_MODE=1``)
    endpoint   the live endpoint: ``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_API_KEY`` / ``WAPSI_LLM_MODEL`` (agent-only
               overrides ``WAPSI_AGENT_*``; see ``editions.shared.endpoint`` for every knob). Tool calling is
               chosen by a capability probe: native OpenAI tool calls, or the strict JSON tool protocol
    offline    the deterministic fake OpenAI-compatible backend (``editions.shared.fake_openai``): the real ADK +
               LiteLLM client stack against an in-process server; no network, no key

``WAPSI_DEMO_MODE=1 WAPSI_AGENT_BACKEND=endpoint`` runs the live agent while draft prose stays on the offline
``FakeLLM`` (the fewest endpoint calls: one per model turn).

The agent needs the ``agent`` extra (``google-adk``, ``litellm``); without it the rest of the edition still runs
and ``agent_status`` says what to install.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from editions.shared.endpoint import AgentEndpoint
from wapsi.agenttrace import ToolTraceRecorder

from .services import Services, get_services
from .settings import AmdSettings

log = logging.getLogger("wapsi.amd.agent")

APP_NAME = "wapsi_officer"
BACKENDS = ("auto", "endpoint", "offline")
BACKEND_ENDPOINT, BACKEND_OFFLINE = "endpoint", "offline"
SOURCE_ENDPOINT, SOURCE_OFFLINE = "adk-endpoint", "adk-endpoint-offline"
PROBE_CACHE_FILE = "agent_tool_probe.json"
INSTALL_HINT = "install the agent extra: uv sync --extra agent (google-adk + litellm)"


class AgentUnavailable(RuntimeError):
    """The agent cannot run here (missing packages or configuration); the message says what to do."""


def missing_dependencies() -> list[str]:
    out = []
    for module, package in (("google.adk", "google-adk"), ("litellm", "litellm")):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(package)
    return out


def resolve_backend(settings: AmdSettings, env: Mapping[str, str] | None = None) -> str:
    raw = ((os.environ if env is None else env).get("WAPSI_AGENT_BACKEND") or "auto").strip().lower()
    if raw not in BACKENDS:
        raise ValueError(f"WAPSI_AGENT_BACKEND must be one of {', '.join(BACKENDS)} (got {raw!r})")
    if raw == "auto":
        return BACKEND_OFFLINE if settings.demo_mode else BACKEND_ENDPOINT
    return raw


def agent_endpoint(settings: AmdSettings, env: Mapping[str, str] | None = None) -> AgentEndpoint:
    """The live endpoint: the ``WAPSI_AGENT_*`` / ``WAPSI_LLM_*`` variables, else this edition's LLM settings."""
    endpoint = AgentEndpoint.from_env(env, base_url=settings.llm_base_url, model=settings.llm_model)
    assert endpoint is not None  # settings always carry a base URL
    return endpoint


def agent_status(services: Services | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Non-secret description for ``/healthz`` and the UI. Makes no network call and builds nothing."""
    services = services or get_services()
    missing = missing_dependencies()
    try:
        backend = resolve_backend(services.settings, env)
    except ValueError as exc:
        return {"available": False, "reason": str(exc)}
    out: dict[str, Any] = {"available": not missing, "backend": backend, "framework": "google-adk"}
    if missing:
        out["reason"] = f"{', '.join(missing)} not installed; {INSTALL_HINT}"
    if backend == BACKEND_OFFLINE:
        out.update(model="scripted planner behind a fake OpenAI-compatible endpoint", provider="offline (no network)")
    else:
        try:
            e = agent_endpoint(services.settings, env)
        except ValueError as exc:
            return {**out, "available": False, "reason": str(exc)}
        out.update(model=e.model, provider=e.provider, base_url=e.base_url, api_key_set=bool(e.api_key),
                   tool_mode=e.tool_mode, fallback_models=list(e.fallback_models))
    rt = _runtime
    if rt is not None and rt.services is services:
        out.update(rt.describe())
    return out


def build_app(services: Services, model: Any, recorder: ToolTraceRecorder, endpoint: AgentEndpoint) -> Any:
    """The ADK ``App``: one ``LlmAgent`` with the officer tools; trace plugin first, then guard, audit, notices."""
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.genai import types

    from editions.shared.instructions import officer_instruction
    from editions.shared.plugins import AuditLogPlugin, EndpointNoticePlugin, WapsiGuardPlugin
    from editions.shared.tools import OFFICER_TOOLS
    from editions.shared.tracing import ToolTracePlugin

    agent = LlmAgent(
        name=APP_NAME,
        model=model,
        description="Nodal-officer copilot: traces stolen money, plans amount-specific holds, shows verified drafts.",
        instruction=officer_instruction(one_tool_per_turn=True),
        tools=list(OFFICER_TOOLS),
        generate_content_config=types.GenerateContentConfig(temperature=endpoint.temperature),
    )
    return App(name=APP_NAME, root_agent=agent,
               plugins=[ToolTracePlugin(recorder, store=services.store), WapsiGuardPlugin(recorder=recorder),
                        AuditLogPlugin(), EndpointNoticePlugin(endpoint.scrub)])


def build_runtime(services: Services | None = None, *, backend: str | None = None,
                  env: Mapping[str, str] | None = None, fake: Any | None = None,
                  model: Any | None = None, endpoint: AgentEndpoint | None = None) -> Any:
    """An ``AgentRuntime`` for ``services``. ``fake`` (a ``FakeOpenAIBackend``) or ``model`` + ``endpoint``
    replace the configured backend (tests, the CLI). Makes no network call: the endpoint is probed on the first
    run."""
    services = services or get_services()
    missing = missing_dependencies()
    if missing:
        raise AgentUnavailable(f"{', '.join(missing)} not installed; {INSTALL_HINT}")
    from editions.shared.endpoint import build_endpoint_model
    from editions.shared.probe import ProbeCache
    from editions.shared.runtime import AgentRuntime

    if fake is not None:
        backend = BACKEND_OFFLINE
    backend = backend or resolve_backend(services.settings, env)
    if model is None:
        if backend == BACKEND_OFFLINE:
            if fake is None:
                from editions.shared.fake_openai import FakeOpenAIBackend

                fake = FakeOpenAIBackend()
            endpoint = fake.endpoint()
            model = fake.build_model()
        else:
            endpoint = endpoint or agent_endpoint(services.settings, env)
            cache = ProbeCache(Path(services.settings.state_dir) / PROBE_CACHE_FILE)
            model = build_endpoint_model(endpoint, probe_cache=cache)
    if endpoint is None:
        endpoint = getattr(model, "endpoint", None)
    if endpoint is None:
        raise AgentUnavailable("an endpoint description is required with a custom model")
    recorder = ToolTraceRecorder(source=SOURCE_OFFLINE if backend == BACKEND_OFFLINE else SOURCE_ENDPOINT,
                                 provider=endpoint.provider)

    def describe() -> dict[str, Any]:
        d = model.describe() if callable(getattr(model, "describe", None)) else endpoint.describe()
        return {"backend": backend, **d}

    runtime = AgentRuntime(lambda rec: build_app(services, model, rec, endpoint), services=services,
                           recorder=recorder, max_llm_calls=endpoint.max_llm_calls, describe=describe,
                           scrub=endpoint.scrub, prepare=getattr(model, "resolve_mode", None))
    runtime.backend = backend
    runtime.endpoint = endpoint
    runtime.model = model
    runtime.fake = fake
    return runtime


# --------------------------------------------------------------------------- the process-wide runtime

_runtime: Any | None = None
_lock = threading.Lock()


def get_runtime(services: Services | None = None) -> Any:
    """The runtime of this process, built on first use and rebuilt when the services object changed."""
    global _runtime
    services = services or get_services()
    with _lock:
        if _runtime is None or _runtime.services is not services:
            old, _runtime = _runtime, build_runtime(services)
            if old is not None:
                old.close()
        return _runtime


def set_runtime(runtime: Any | None) -> None:
    """Install (or clear, with ``None``) the process-wide runtime; the previous one is closed."""
    global _runtime
    with _lock:
        old, _runtime = _runtime, runtime
    if old is not None and old is not runtime:
        old.close()


def with_case(text: str, case_id: str | None) -> str:
    """The officer's message with the case in view appended when the message does not name it (a chat inside a
    case room; the agent then knows which case "this" is)."""
    text = (text or "").strip()
    if case_id and case_id not in text:
        return f"{text}\n\n[Case in view: {case_id}]"
    return text


def ask(text: str, *, services: Services | None = None, case_id: str | None = None, user_id: str = "officer",
        role: str = "analyst", name: str | None = None, session: str = "", runtime: Any | None = None) -> Any:
    """Run one officer message through the agent and return the ``AgentRunResult``."""
    rt = runtime or get_runtime(services)
    return rt.run(with_case(text, case_id), user_id=user_id, role=role, name=name, session=session or (case_id or ""))
