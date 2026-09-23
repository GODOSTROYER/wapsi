"""Memory and session service wiring.

- Default: in-memory sessions and memory (per Cloud Run instance; free).
- ``WAPSI_ADK_STORE=firestore``: ADK's own ``FirestoreSessionService`` / ``FirestoreMemoryService`` (keyword memory)
  in the project's Firestore database, so sessions survive scale-to-zero and work across instances, within the
  Firestore free tier at demo scale. ``register_firestore_services`` registers the ``firestore://`` scheme with
  ADK's service registry; ``server.py`` calls it before ``get_fast_api_app``.
- ``WAPSI_MEMORY_BANK_ID``: Memory Bank and sessions on Agent Runtime (``agentengine://projects/...``). A paid,
  optional feature that also needs ``google-cloud-aiplatform``; off unless the id is set.

:func:`build_memory_service` returns the same service object for in-process runners (tests, the Pub/Sub workflow,
scripts).
"""

from __future__ import annotations

import logging
from typing import Any

from .config import FIRESTORE_URI, GcpSettings, agent_runtime_sdk_available

log = logging.getLogger("wapsi.gcp.memory")

ADK_SESSIONS_COLLECTION = "wapsi-adk-sessions"
ADK_MEMORIES_COLLECTION = "wapsi-adk-memories"


def _firestore_async_client(settings: GcpSettings) -> Any:
    from google.cloud import firestore  # lazy; the client connects on first use

    return firestore.AsyncClient(project=settings.project, database=settings.firestore_database)


def firestore_session_service(settings: GcpSettings) -> Any:
    from google.adk.integrations.firestore.firestore_session_service import FirestoreSessionService

    return FirestoreSessionService(client=_firestore_async_client(settings), root_collection=ADK_SESSIONS_COLLECTION)


def firestore_memory_service(settings: GcpSettings) -> Any:
    from google.adk.integrations.firestore.firestore_memory_service import FirestoreMemoryService

    return FirestoreMemoryService(client=_firestore_async_client(settings),
                                  memories_collection=ADK_MEMORIES_COLLECTION)


def register_firestore_services(settings: GcpSettings) -> None:
    """Teach ADK's service registry the ``firestore://`` session and memory URIs for this project."""
    from google.adk.cli.service_registry import get_service_registry

    registry = get_service_registry()
    registry.register_session_service("firestore", lambda uri, **kwargs: firestore_session_service(settings))
    registry.register_memory_service("firestore", lambda uri, **kwargs: firestore_memory_service(settings))


def build_memory_service(settings: GcpSettings) -> Any:
    from google.adk.memory import InMemoryMemoryService

    if settings.demo_mode:
        return InMemoryMemoryService()
    resource = settings.agent_engine_resource()
    if resource and agent_runtime_sdk_available():
        from google.adk.memory import VertexAiMemoryBankService

        parts = resource.split("/")  # projects/{p}/locations/{l}/reasoningEngines/{id}
        return VertexAiMemoryBankService(project=parts[1], location=parts[3], agent_engine_id=parts[5])
    if resource:
        log.warning("Memory Bank needs google-cloud-aiplatform (google-adk[gcp]); using WAPSI_ADK_STORE=%s",
                    settings.adk_store)
    if settings.memory_service_uri() == FIRESTORE_URI:
        return firestore_memory_service(settings)
    return InMemoryMemoryService()


def memory_tools(settings: GcpSettings) -> list[Any]:
    """``load_memory`` lets the officer agent recall related rings and earlier cases on demand."""
    from google.adk.tools.load_memory_tool import load_memory_tool

    return [load_memory_tool]
