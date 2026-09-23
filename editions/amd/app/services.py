"""Builds the AMD edition's adapters.

GPU mode (default, on the MI300X droplet):
    ledger  ``ParquetLedger(WAPSI_WORLD)`` (synthetic world mounted read-only)
    llm     ``OpenAICompatLLM`` -> vLLM-ROCm (``http://llm:8000/v1``, merged Gemma 4 31B STR checkpoint)
    guard   ``RulesGuard``, or ``LLMJudgeGuard`` (rules + verifier LLM) when ``WAPSI_VERIFIER_BASE_URL`` is set
    scorer  ``GnnHttpScorer`` (``WAPSI_GNN_URL``, heuristic fallback) or the default heuristic scorer
    store   ``FileCaseStore(WAPSI_STATE_DIR)``
Demo mode (``WAPSI_DEMO_MODE=1``, any laptop, no network): the same, with the offline ``FakeLLM``, rules guard and
heuristic scorer (the GNN is still used if ``WAPSI_GNN_URL`` is set explicitly).

Nothing here calls the network at build time; clients connect on first use.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wapsi.adapters.memory import RecordingActionSink
from wapsi.config import WapsiConfig
from wapsi.models import CaseBundle
from wapsi.ports import LLM, ActionSink, CaseStore, DocSearch, Guard, Ledger

from .file_store import FileCaseStore
from .settings import AmdSettings, load_settings

log = logging.getLogger("wapsi.amd")

_NCRP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Services:
    settings: AmdSettings
    ledger: Ledger
    store: CaseStore
    llm: LLM
    guard: Guard
    search: DocSearch | None
    sink: ActionSink
    scorer: Any | None = None  # wapsi.classify.Scorer; None = heuristic default
    verifier_llm: LLM | None = None
    cfg: WapsiConfig = field(default_factory=WapsiConfig)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def describe(self) -> dict[str, Any]:
        return {
            **self.settings.describe(),
            "llm_name": getattr(self.llm, "name", type(self.llm).__name__),
            "guard": getattr(self.guard, "name", type(self.guard).__name__),
            "scorer": getattr(self.scorer, "name", "heuristic") if self.scorer is not None else "heuristic",
            "search": self.search is not None,
            "store": repr(self.store),
        }


def _docs_search() -> DocSearch | None:
    try:
        from wapsi.search.index import LocalDocSearch

        return LocalDocSearch.load()
    except Exception:  # noqa: BLE001 - the corpus index is optional
        log.warning("docs index not available; citations are checked against the registry only")
        return None


def load_ledger(world_path: str | None) -> Ledger:
    if world_path and Path(world_path, "transactions.parquet").exists():
        from wapsi.adapters.parquet_ledger import ParquetLedger

        return ParquetLedger(world_path)
    if world_path:
        log.warning("world %s not found; using the in-memory showcase ledger", world_path)
    from wapsi.fixtures.showcase import showcase_ledger

    return showcase_ledger()


def build_services(settings: AmdSettings | None = None, *, llm: LLM | None = None,
                   verifier_llm: LLM | None = None, scorer: Any | None = None,
                   search: DocSearch | None | bool = True) -> Services:
    """``llm`` / ``verifier_llm`` / ``scorer`` override the configured adapters (tests, benchmarks).
    ``search=True`` loads the local docs index when present; ``False``/``None`` disables it."""
    settings = settings or load_settings()
    from wapsi.verify.injection import RulesGuard

    ledger = load_ledger(settings.world_path)
    store = FileCaseStore(settings.state_dir)
    docs = _docs_search() if search is True else (search or None)

    if llm is None:
        if settings.demo_mode:
            from wapsi.adapters.llm_fake import FakeLLM

            llm = FakeLLM(name="fake-llm (demo mode)")
        else:
            from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

            llm = OpenAICompatLLM(base_url=settings.llm_base_url, model=settings.llm_model,
                                  timeout=settings.llm_timeout_s, json_mode=settings.llm_json_mode,
                                  name=f"vllm-rocm:{settings.llm_model}")

    guard: Guard = RulesGuard()
    if verifier_llm is None and settings.verifier_base_url and not settings.demo_mode:
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        verifier_llm = OpenAICompatLLM(base_url=settings.verifier_base_url, model=settings.verifier_model,
                                       api_key=settings.verifier_api_key, timeout=min(settings.llm_timeout_s, 60),
                                       max_retries=1, json_mode=settings.llm_json_mode,
                                       name=f"vllm-rocm:{settings.verifier_model}")
    if verifier_llm is not None:
        from .judge import LLMJudgeGuard

        guard = LLMJudgeGuard(verifier_llm, rules=RulesGuard())

    if scorer is None and settings.gnn_url:
        from .gnn_client import GnnHttpScorer

        scorer = GnnHttpScorer(settings.gnn_url, timeout_s=settings.gnn_timeout_s)

    return Services(settings=settings, ledger=ledger, store=store, llm=llm, guard=guard, search=docs,
                    sink=RecordingActionSink(), scorer=scorer, verifier_llm=verifier_llm)


# --------------------------------------------------------------------------- case helpers


def resolve_ncrp_ids(services: Services, ids: str) -> list[str]:
    """``"31009260000101"``, ``"a,b"`` or ``"CASE-31009260000101"`` -> NCRP ids (validated against the ledger).

    Raises ``ValueError`` for malformed ids and ``LookupError`` for unknown complaints."""
    out: list[str] = []
    for raw in (p.strip() for p in ids.split(",")):
        if not raw:
            continue
        if not _NCRP_ID.match(raw):
            raise ValueError(f"malformed id {raw!r}")
        if raw.startswith("CASE-"):
            known = services.store.get_bundle(raw)
            out.extend(known.case.ncrp_ids if known else [raw.removeprefix("CASE-")])
        else:
            out.append(raw)
    out = sorted(dict.fromkeys(out))
    if not out:
        raise ValueError("no complaint ids given")
    for n in out:
        try:
            services.ledger.complaint(n)
        except (KeyError, LookupError) as exc:
            raise LookupError(f"unknown complaint {n}") from exc
    return out


def process(services: Services, ncrp_ids: list[str], *, case_id: str | None = None, draft: bool = True,
            persist: bool = True) -> CaseBundle:
    from wapsi.pipeline.run import process_case

    return process_case(services.ledger, ncrp_ids, llm=services.llm, cfg=services.cfg, search=services.search,
                        guard=services.guard, scorer=services.scorer,
                        store=services.store if persist else None, case_id=case_id, draft=draft)


def complaint_groups(services: Services) -> list[list[str]]:
    from wapsi.pipeline.run import group_complaints

    return group_complaints(services.ledger, services.ledger.complaints())


def seed_store(services: Services, limit: int) -> list[str]:
    """Process up to ``limit`` complaint groups when the store is empty (so the UI has cases to show)."""
    if limit <= 0 or services.store.list_cases():
        return []
    done: list[str] = []
    for ids in complaint_groups(services)[:limit]:
        try:
            done.append(process(services, ids).case.case_id)
        except Exception:  # noqa: BLE001 - seeding is best effort
            log.exception("seeding failed for %s", ids)
    return done


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
    """Install (or clear) the process-wide services; used by tests and benchmarks."""
    global _services
    with _lock:
        _services = services
