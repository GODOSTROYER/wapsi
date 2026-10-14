"""Builds the AMD edition's adapters.

GPU mode (default, on the MI300X droplet):
    ledger  ``ParquetLedger(WAPSI_WORLD)`` (synthetic world mounted read-only)
    llm     ``OpenAICompatLLM`` -> vLLM-ROCm (``http://llm:8000/v1``, merged Gemma 4 31B STR checkpoint)
    guard   ``RulesGuard``, or ``LLMJudgeGuard`` (rules + verifier LLM) when ``WAPSI_VERIFIER_BASE_URL`` is set
    scorer  ``GnnHttpScorer`` (``WAPSI_GNN_URL``, heuristic fallback) or the default heuristic scorer
    store   ``FileCaseStore(WAPSI_STATE_DIR)``
Demo mode (``WAPSI_DEMO_MODE=1``, any laptop, no network): the same, with the offline ``FakeLLM``, rules guard and
heuristic scorer (the GNN is still used if ``WAPSI_GNN_URL`` is set explicitly).

Voice intake (``voice_model``): ``WAPSI_VOICE_BACKEND=auto|open|gemini|fake`` (``wapsi.intake.backends``). The open
path transcribes with Whisper (``WAPSI_ASR_BASE_URL`` endpoint, or local through transformers on ROCm / CPU) and
structures the transcript with this edition's own LLM; demo mode keeps the offline model unless ``open`` is asked
for explicitly. Built on first use.

Nothing here calls the network at build time; clients connect on first use.
"""

from __future__ import annotations

import logging
import os
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
    voice: Any | None = field(default=None, repr=False)  # the voice-intake model, built on first use

    def describe(self) -> dict[str, Any]:
        out = {
            **self.settings.describe(),
            "llm_name": getattr(self.llm, "name", type(self.llm).__name__),
            "guard": getattr(self.guard, "name", type(self.guard).__name__),
            "scorer": getattr(self.scorer, "name", "heuristic") if self.scorer is not None else "heuristic",
            "search": self.search is not None,
            "store": repr(self.store),
            "voice_backend": (os.environ.get("WAPSI_VOICE_BACKEND") or "auto").strip().lower(),
        }
        if self.voice is not None:  # only once built: /healthz makes no network call (no audio probe)
            from wapsi.intake.backends import describe_voice_model

            out["voice"] = describe_voice_model(self.voice)["label"]
        return out


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


def llm_name(base_url: str, model: str, env: Any = None) -> str:
    """The name drafts record for the model (``Draft.llm_model``; the UI's chip reads it back). It says what really
    serves the model: for this stack's own server ``vllm-rocm:<model>`` only when AMD is confirmed (the compose
    file's ``WAPSI_LLM_PLATFORM=AMD``, or ROCm visible here), else ``vllm:<model>``; otherwise the endpoint
    (``Gemma-4-31B-IT on AMD @ developer.amd.com.cn`` for the AMD Token Factory; "on AMD" for any other host only
    when the deployer states it with ``WAPSI_LLM_PLATFORM`` / ``WAPSI_LLM_ON_AMD``). Never guessed."""
    from wapsi.adapters.llm_env import declared_platform, platform_for, self_hosted_name, served_name
    from wapsi.adapters.llm_openai_compat import endpoint_host

    from .settings import is_stack_server

    if is_stack_server(base_url):
        return self_hosted_name(model, env)
    host = endpoint_host(base_url)
    return served_name(model, host, platform_for(host, declared_platform(env)))


def build_prose_llm(settings: AmdSettings) -> LLM:
    """The drafts' prose client: one ``OpenAICompatLLM`` per model of ``WAPSI_LLM_MODELS``, each behind
    ``ResilientLLM`` (prompt-hash cache, 429 / 503 retries that honour ``Retry-After``), all sharing the endpoint's
    one process-wide limiter (``shared_limiter``: the agent's model turns pace on it too), behind a fail-fast
    ``FailoverLLM`` even for a single model: after a connection error the endpoint rests for a minute and the
    drafts get their fixed paragraph at once, instead of every prose field waiting on an unreachable host."""
    from wapsi.adapters.llm_failover import FailoverLLM
    from wapsi.adapters.llm_openai_compat import OpenAICompatLLM, endpoint_host
    from wapsi.adapters.llm_replay import prose_answer_problem
    from wapsi.adapters.llm_resilience import ResilientLLM, announced_sleep, shared_limiter
    from wapsi.intake.llm_chain import is_degenerate

    def unusable(messages: Any, text: str) -> str | None:
        """An HTTP 200 with garbage is refused before the cache sees it (never cached, never replayed)."""
        return prose_answer_problem(messages, text) or ("degenerate output" if is_degenerate(text) else None)

    host = endpoint_host(settings.llm_base_url)
    limiter = shared_limiter(host or settings.llm_base_url, rpm=settings.llm_rpm,
                             min_interval_s=settings.llm_min_interval_s)
    members = []
    for model in (settings.llm_model, *settings.llm_fallback_models):
        name = llm_name(settings.llm_base_url, model)
        inner = OpenAICompatLLM(base_url=settings.llm_base_url, model=model, timeout=settings.llm_timeout_s,
                                json_mode=settings.llm_json_mode, max_retries=0, name=name)
        members.append(ResilientLLM(inner, model_id=model, backend="openai-compatible", rpm=0, rpd=0,
                                    min_interval_s=0.0, limiter=limiter, cache_dir=settings.llm_cache_dir,
                                    cache_namespace=host, max_retries=1, name=name, sleep=announced_sleep(),
                                    accept=unusable))
    return FailoverLLM(members, fail_fast=True)


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
            # one client per model of WAPSI_LLM_MODELS: a model that is withdrawn or answers in garbage is skipped
            # and the next one writes the prose (each draft records which one did)
            llm = build_prose_llm(settings)

    guard: Guard = RulesGuard()
    if verifier_llm is None and settings.verifier_base_url and not settings.demo_mode:
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        verifier_llm = OpenAICompatLLM(base_url=settings.verifier_base_url, model=settings.verifier_model,
                                       api_key=settings.verifier_api_key, timeout=min(settings.llm_timeout_s, 60),
                                       max_retries=1, json_mode=settings.llm_json_mode,
                                       name=llm_name(settings.verifier_base_url, settings.verifier_model))
    if verifier_llm is not None:
        from .judge import LLMJudgeGuard

        guard = LLMJudgeGuard(verifier_llm, rules=RulesGuard())

    if scorer is None and settings.gnn_url:
        from .gnn_client import GnnHttpScorer

        scorer = GnnHttpScorer(settings.gnn_url, timeout_s=settings.gnn_timeout_s)

    return Services(settings=settings, ledger=ledger, store=store, llm=llm, guard=guard, search=docs,
                    sink=RecordingActionSink(), scorer=scorer, verifier_llm=verifier_llm)


# --------------------------------------------------------------------------- voice intake


def build_voice(services: Services, env: Any = None) -> Any:
    """The voice model for this edition (an ``AudioLLM``); never raises (a configuration that cannot be built
    gives a stand-in that says what to set).

    Demo mode keeps the offline model (bundled samples) unless ``WAPSI_VOICE_BACKEND`` asks for something else.
    Otherwise the transcript is structured by this edition's own LLM client, so translation and field extraction
    run wherever drafting runs: vLLM-ROCm on the droplet, or the AMD Token Factory."""
    from wapsi.intake.backends import VOICE_BACKEND_ENV, llm_provider, safe_voice_model

    e = os.environ if env is None else env
    requested = (e.get(VOICE_BACKEND_ENV) or "auto").strip().lower()
    if services.settings.demo_mode:
        if requested == "auto" and callable(getattr(services.llm, "extract_media", None)):
            return services.llm
        return safe_voice_model(e)  # an explicit backend in demo mode is built from WAPSI_LLM_* / WAPSI_ASR_*
    from wapsi.adapters.llm_failover import FailoverLLM

    # the first drafting client; voice builds its own chain along the same WAPSI_LLM_MODELS list (health probe,
    # the JSON-mode ladder per client), sharing what the drafts' failover learnt through the health registry
    from wapsi.adapters.llm_resilience import ResilientLLM

    head = services.llm.primary if isinstance(services.llm, FailoverLLM) else services.llm
    if isinstance(head, ResilientLLM):  # the drafts' paced client: voice gets its own raw client (one retry)
        from wapsi.adapters.llm_openai_compat import OpenAICompatLLM

        s = services.settings
        head = OpenAICompatLLM(base_url=s.llm_base_url, model=s.llm_model, timeout=s.llm_timeout_s,
                               json_mode=s.llm_json_mode, max_retries=1, name=llm_name(s.llm_base_url, s.llm_model))
    served_by, on_amd = llm_provider(str(getattr(head, "base_url", "") or ""), e)
    return safe_voice_model(e, text_llm=head, served_by=served_by, llm_on_amd=on_amd)


def voice_model(services: Services) -> Any:
    """``services.voice``, built once (thread-safe): a local Whisper stays loaded between requests."""
    if services.voice is None:
        with services.lock:
            if services.voice is None:
                services.voice = build_voice(services)
    return services.voice


def run_voice_intake(services: Services, audio: bytes, *, filename: str = "", sample_id: str | None = None,
                     received_at: Any = None) -> Any:
    """Audio bytes -> ``VoiceIntakeResult`` with this edition's voice model, ledger and guard. Raises
    ``VoiceIntakeError`` / ``LLMError`` (the API and the UI turn them into messages)."""
    from wapsi.intake.voice import intake_voice_note

    return intake_voice_note(audio, llm=voice_model(services), ledger=services.ledger, guard=services.guard,
                             filename=filename, received_at=received_at, sample_id=sample_id)


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
