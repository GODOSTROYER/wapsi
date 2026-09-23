"""Memory service wiring: Memory Bank (Agent Runtime) behind ``WAPSI_MEMORY_BANK_ID``, in-memory otherwise.

``adk api_server`` / ``server.py`` pass :meth:`GcpSettings.memory_service_uri` (``agentengine://projects/...``)
to ADK, which builds the ``VertexAiMemoryBankService`` itself. :func:`build_memory_service` returns the same
service object for in-process runners (tests, the Pub/Sub workflow, scripts).
"""

from __future__ import annotations

import logging
from typing import Any

from .config import GcpSettings, agent_runtime_sdk_available

log = logging.getLogger("wapsi.gcp.memory")


def build_memory_service(settings: GcpSettings) -> Any:
    from google.adk.memory import InMemoryMemoryService

    resource = None if settings.demo_mode else settings.agent_engine_resource()
    if not resource:
        return InMemoryMemoryService()
    if not agent_runtime_sdk_available():
        log.warning("Memory Bank needs google-cloud-aiplatform (google-adk[gcp]); falling back to in-memory memory")
        return InMemoryMemoryService()
    from google.adk.memory import VertexAiMemoryBankService

    parts = resource.split("/")  # projects/{p}/locations/{l}/reasoningEngines/{id}
    return VertexAiMemoryBankService(project=parts[1], location=parts[3], agent_engine_id=parts[5])


def memory_tools(settings: GcpSettings) -> list[Any]:
    """``load_memory`` lets the officer agent recall related rings and earlier cases on demand."""
    from google.adk.tools.load_memory_tool import load_memory_tool

    return [load_memory_tool]
