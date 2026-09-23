"""Builds the adapters the tools use: ledger, case store, LLMs, guard, docs search, action sink.

Demo mode (zero cloud access): showcase ledger (or ``WAPSI_WORLD`` Parquet), ``InMemoryCaseStore``, ``FakeLLM``,
``RulesGuard``, local BM25 docs index, recording action sink.

Cloud mode: ``BigQueryLedger``, ``FirestoreCaseStore``, ``GeminiLLM`` (Agent Platform) for draft prose, or Gemma 4
on Cloud Run GPU when ``WAPSI_PRIVATE_LLM_URL`` is set, ``ModelArmorGuard`` (falls back to ``RulesGuard``) when a
template is configured. Google clients are created lazily on first use, so building services never calls out.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wapsi.adapters.memory import InMemoryCaseStore, RecordingActionSink
from wapsi.config import WapsiConfig
from wapsi.ports import LLM, ActionSink, CaseStore, DocSearch, Guard, Ledger

from .config import GcpSettings, load_settings
from .identity import configure_signing_key

log = logging.getLogger("wapsi.gcp")


@dataclass
class Services:
    settings: GcpSettings
    ledger: Ledger
    store: CaseStore
    llm: LLM  # draft prose (fact sheet in, placeholder prose out)
    guard: Guard
    search: DocSearch | None
    sink: ActionSink
    cfg: WapsiConfig = field(default_factory=WapsiConfig)
    extractor: Any | None = None  # google-genai client for multimodal intake (None in demo mode)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _docs_search() -> DocSearch | None:
    try:
        from wapsi.search.index import LocalDocSearch

        return LocalDocSearch.load()
    except Exception:  # noqa: BLE001 - the corpus index is optional
        log.warning("docs index not available; search_sop will be disabled", exc_info=True)
        return None


def _demo_ledger(settings: GcpSettings) -> Ledger:
    if settings.world_path and Path(settings.world_path, "transactions.parquet").exists():
        from wapsi.adapters.parquet_ledger import ParquetLedger

        return ParquetLedger(settings.world_path)
    from wapsi.fixtures.showcase import showcase_ledger

    return showcase_ledger()


def build_services(settings: GcpSettings | None = None) -> Services:
    settings = settings or load_settings()
    configure_signing_key(settings.role_signing_key)
    from wapsi.verify.injection import RulesGuard

    if settings.demo_mode:
        from wapsi.adapters.llm_fake import FakeLLM

        return Services(
            settings=settings,
            ledger=_demo_ledger(settings),
            store=InMemoryCaseStore(),
            llm=FakeLLM(name="fake-llm (demo mode)"),
            guard=RulesGuard(),
            search=_docs_search(),
            sink=RecordingActionSink(),
        )

    from wapsi.adapters.bigquery_ledger import BigQueryLedger
    from wapsi.adapters.firestore_store import FirestoreCaseStore
    from wapsi.adapters.gemini_llm import GeminiLLM, gemma_private_llm

    assert settings.project is not None
    ledger = BigQueryLedger(settings.project, settings.bq_dataset, location=settings.region)
    store = FirestoreCaseStore(project=settings.project, database=settings.firestore_database,
                               collection=settings.cases_collection)
    if settings.private_llm_url:
        llm: LLM = gemma_private_llm(settings.private_llm_url, settings.private_llm_model,
                                     id_token_audience=settings.private_llm_url.rstrip("/").removesuffix("/v1"))
    else:
        llm = GeminiLLM(settings.draft_model or settings.model, project=settings.project,
                        location=settings.location)
    guard: Guard = RulesGuard()
    ma = settings.model_armor()
    if ma is not None:
        from wapsi.adapters.model_armor_guard import ModelArmorGuard

        guard = ModelArmorGuard(*ma, fallback=RulesGuard())
    return Services(settings=settings, ledger=ledger, store=store, llm=llm, guard=guard,
                    search=_docs_search(), sink=RecordingActionSink())


_services: Services | None = None
_lock = threading.Lock()


def get_services() -> Services:
    global _services
    if _services is None:
        with _lock:
            if _services is None:
                _services = build_services()
    return _services


def set_services(services: Services | None) -> None:
    """Install (or clear, with ``None``) the process-wide services; used by tests and the server."""
    global _services
    with _lock:
        _services = services
