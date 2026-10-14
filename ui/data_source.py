"""Where the UI reads and writes case data.

The showcase world is processed by the real pipeline (``wapsi.pipeline.run.process_case`` with the offline
``FakeLLM``) when the engine is installed, and falls back to the hand-built bundles in ``wapsi.fixtures.bundles``
otherwise. ``WAPSI_UI_LLM=auto`` (the public demo, ``deploy/demo``) swaps the stub for ``prose_llm``: the live
OpenAI-compatible endpoint when one is configured (Gemma 4 on AMD), else a labelled recorded run. Engine modules are imported lazily, inside try/except, so a partial install never breaks a page.
``WAPSI_DATA_SOURCE`` (``resolve_source`` / ``load_source``) switches to a synthetic parquet world instead: ``demo``
(``data/generated/demo``) or a path; its cases come from the world's golden set (else ``group_complaints``).
Editions swap ``ShowcaseDataSource`` for a BigQuery- or Parquet-backed implementation of the same
``DataSource`` protocol.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from wapsi.audit.chain import ChainReport, append_event, chain_extend, sha256_hex, verify_chain
from wapsi.models import (
    Approval,
    ApprovalDecision,
    AuditEvent,
    Case,
    CaseBundle,
    Clock,
    ClockStatus,
    GuardResult,
    VerificationReport,
)
from wapsi.timeutil import ensure_utc, utcnow

log = logging.getLogger("wapsi.ui")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_REPORT = REPO_ROOT / "evals" / "reports" / "latest.json"

ENGINE_MODULES: dict[str, str] = {
    "Trail tracing": "wapsi.graph.trace",
    "Hold plan": "wapsi.graph.holdplan",
    "Drafting": "wapsi.draft.drafter",
    "Verification": "wapsi.verify.injection",
    "Clocks": "wapsi.domain.clocks",
    "Pipeline": "wapsi.pipeline.run",
    "Offline LLM": "wapsi.adapters.llm_fake",
}


# --------------------------------------------------------------------------- results


@dataclass
class Outcome:
    ok: bool
    message: str
    unavailable: bool = False  # engine modules missing (not an error; phase 2 wires them)
    bundle: CaseBundle | None = None
    data: Any = None  # a result object (e.g. the LEA request check)


@dataclass
class EngineStatus:
    modules: dict[str, bool] = field(default_factory=dict)

    @property
    def pipeline_ready(self) -> bool:
        return all(self.modules.values()) if self.modules else False

    @property
    def available(self) -> list[str]:
        return [k for k, v in self.modules.items() if v]

    @property
    def missing(self) -> list[str]:
        return [k for k, v in self.modules.items() if not v]


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _optional(module: str, attr: str) -> Any:
    """Import ``module.attr`` lazily; raises ImportError if either is missing (engine not installed yet)."""
    mod = importlib.import_module(module)
    try:
        return getattr(mod, attr)
    except AttributeError as exc:
        raise ImportError(f"{module}.{attr} is not available") from exc


_SEARCH_CACHE: dict[str, Any] = {}


def doc_search() -> Any | None:
    """``wapsi.search.index.LocalDocSearch`` when the docs index is available; None otherwise (cached)."""
    if "search" not in _SEARCH_CACHE:
        try:
            _SEARCH_CACHE["search"] = _optional("wapsi.search.index", "LocalDocSearch").load()
        except Exception:  # noqa: BLE001 - search is optional
            _SEARCH_CACHE["search"] = None
    return _SEARCH_CACHE["search"]


# --------------------------------------------------------------------------- the model that writes prose

RECORDING_PATH = REPO_ROOT / "data" / "recordings" / "demo_prose.json"
RECORDING_ENV = "WAPSI_PROSE_RECORDING"  # another recording file ("" / "none" = no replay)
LLM_CACHE_ENV = "WAPSI_LLM_CACHE_DIR"  # where live answers are cached (default: the system temp directory)
UI_LIVE_RPM = 12  # default requests a minute to the live endpoint (WAPSI_LLM_RPM overrides)
UI_LIVE_MAX_WAIT_S = 8.0  # a page never waits longer than this for the rate limit: it fails over instead
_LLM_STATE: dict[Any, Any] = {}
_LLM_LOCK = threading.Lock()


def recording_path() -> Path | None:
    raw = os.environ.get(RECORDING_ENV)
    if raw is None:
        return RECORDING_PATH
    raw = raw.strip()
    return None if raw.lower() in ("", "none", "off", "0") else Path(raw).expanduser()


def prose_recording() -> Any | None:
    """The recording of model-written prose that the demo replays (``wapsi.adapters.llm_replay.Recording``;
    ``data/recordings/demo_prose.json`` or ``WAPSI_PROSE_RECORDING``). Loaded once per file version; ``None`` when
    there is none."""
    path = recording_path()
    if path is None:
        return None
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = ("recording", str(path), stamp)
    with _LLM_LOCK:
        if key not in _LLM_STATE:
            try:
                _LLM_STATE[key] = _optional("wapsi.adapters.llm_replay", "Recording").load(path)
            except Exception:  # noqa: BLE001 - replay is optional
                log.exception("could not load the prose recording %s", path)
                _LLM_STATE[key] = None
        return _LLM_STATE[key]


def llm_setup() -> Any:
    """What this deployment is set up to do (``ui.components.llm_status.LlmStatus``): live endpoint, recorded run
    or offline stub. Never raises and never reads the key's value."""
    from ui.components.llm_status import llm_status

    return llm_status(recording=prose_recording())


def llm_cache_key() -> tuple[Any, ...]:
    """Changes whenever the prose would come from somewhere else (mode, endpoint, model, recording version): part
    of the key the app caches its seeded cases under."""
    from ui.components.llm_status import ui_llm_mode

    mode = ui_llm_mode()
    if mode == "fake":
        return (mode,)
    path = recording_path()
    try:
        stamp = path.stat().st_mtime_ns if path is not None else 0
    except OSError:
        stamp = 0
    status = llm_setup()
    return (mode, status.endpoint.base_url if status.live else None, status.endpoint.model if status.live else None,
            str(path), stamp)


def _live_gate() -> Any:
    with _LLM_LOCK:
        if "gate" not in _LLM_STATE:
            _LLM_STATE["gate"] = _optional("wapsi.adapters.llm_replay", "LiveGate")()
        return _LLM_STATE["gate"]


def _live_llm(fresh: bool) -> Any | None:
    """The configured live endpoint behind ``ResilientLLM`` (disk cache, rate limit, one retry), or ``None`` when
    it is not configured. One client and one rate limiter per process; ``fresh`` skips the cache read (the officer
    asked for new drafts) and still stores the answer."""
    from wapsi.adapters.llm_env import BACKEND, endpoint_config, make_endpoint_llm
    from wapsi.adapters.llm_resilience import RPM_ENV, RateLimiter, ResilientLLM

    cfg = endpoint_config()
    if not cfg.configured:
        return None
    key = ("live", cfg.base_url, cfg.model, cfg.json_mode, cfg.timeout_s, cfg.platform, cfg.has_key,
           json.dumps(cfg.extra_body, sort_keys=True, default=str))
    with _LLM_LOCK:
        pair = _LLM_STATE.get(key)
        if pair is None:
            import tempfile

            try:
                rpm = max(0, int((os.environ.get(RPM_ENV) or "").strip() or UI_LIVE_RPM))
            except ValueError:
                rpm = UI_LIVE_RPM
            cache_dir = os.environ.get(LLM_CACHE_ENV, "").strip() or str(Path(tempfile.gettempdir()) / "wapsi-llm-cache")
            inner = make_endpoint_llm(cfg, timeout=min(cfg.timeout_s, 45.0))
            limiter = RateLimiter(rpm, 0, max_wait_s=UI_LIVE_MAX_WAIT_S)
            # accept: an HTTP 200 with degenerate output is refused (never cached), and the recorded run answers
            common: dict[str, Any] = {"model_id": cfg.model, "backend": BACKEND, "cache_dir": cache_dir,
                                      "cache_namespace": cfg.host, "limiter": limiter, "max_retries": 1,
                                      "backoff_s": 2.0, "max_backoff_s": 6.0, "name": cfg.served_name,
                                      "accept": _optional("wapsi.adapters.llm_replay", "prose_answer_problem")}
            pair = (ResilientLLM(inner, **common), ResilientLLM(inner, read_cache=False, **common))
            _LLM_STATE[key] = pair
    return pair[1 if fresh else 0]


def prose_llm(fake_cls: Any, *, prefer: str = "live", fresh: bool = False, allow_live: bool = True) -> Any:
    """The LLM a pipeline run in the UI writes prose with (``WAPSI_UI_LLM``, see ``ui.components.llm_status``):

    - ``fake`` (default): ``fake_cls()``, the offline stub, exactly as before.
    - ``auto`` (the public demo): the live OpenAI-compatible endpoint when ``WAPSI_LLM_BASE_URL`` /
      ``WAPSI_LLM_API_KEY`` / ``WAPSI_LLM_MODEL`` are set, else (or when the live call fails) the recorded run,
      else the stub. ``prefer="recorded"`` puts the recording first; ``allow_live=False`` keeps a start-up batch off
      the endpoint.
    - ``recorded``: recording, then the stub; never a network call.
    - ``live``: the live endpoint only; a failed call shows as the fixed paragraph, flagged on the draft.

    Every draft records who wrote its prose, so the page can say so. Any set-up problem falls back to the stub."""
    stub = fake_cls()
    try:
        from ui.components.llm_status import MODE_AUTO, MODE_FAKE, MODE_LIVE, MODE_RECORDED, ui_llm_mode

        mode = ui_llm_mode()
        if mode == MODE_FAKE:
            return stub
        demo_llm = _optional("wapsi.adapters.llm_replay", "DemoLLM")
        live = _live_llm(fresh) if allow_live and mode in (MODE_AUTO, MODE_LIVE) else None
        if mode == MODE_LIVE:
            return demo_llm(live=live, gate=_live_gate()) if live is not None else stub
        recording = prose_recording() if mode in (MODE_AUTO, MODE_RECORDED) else None
        if live is None and (recording is None or not len(recording)):
            return stub
        return demo_llm(live=live, recording=recording, stub=stub, prefer=prefer, gate=_live_gate())
    except Exception:  # noqa: BLE001 - the page must work without the demo LLM
        log.exception("could not set up the demo LLM; using the offline stub")
        return stub


SOURCE_PIPELINE, SOURCE_FIXTURES, SOURCE_MIXED = "pipeline", "fixtures", "mixed"


def build_showcase_bundles(ledger: Any | None = None, *, live: bool = True) -> tuple[list[CaseBundle], str]:
    """(bundles, source). Runs each showcase case through the pipeline when ``live`` and the engine is installed;
    any case that fails falls back to its hand-built fixture bundle."""
    from wapsi.fixtures.bundles import all_showcase_bundles

    fixtures = all_showcase_bundles()
    if not live:
        return fixtures, SOURCE_FIXTURES
    try:
        process_case = _optional("wapsi.pipeline.run", "process_case")
        fake_llm_cls = _optional("wapsi.adapters.llm_fake", "FakeLLM")
    except ImportError:
        return fixtures, SOURCE_FIXTURES
    if ledger is None:
        from wapsi.fixtures.showcase import showcase_ledger

        ledger = showcase_ledger()
    search = doc_search()
    out: list[CaseBundle] = []
    live_count = 0
    for fx in fixtures:
        try:
            b = process_case(ledger, list(fx.case.ncrp_ids), llm=prose_llm(fake_llm_cls), search=search,
                             case_id=fx.case.case_id, assigned_to=fx.case.assigned_to)
        except Exception:  # noqa: BLE001 - fall back to the fixture for this case
            log.exception("pipeline failed for %s; using the fixture bundle", fx.case.case_id)
            out.append(fx)
            continue
        b.case.opened_at = fx.case.opened_at  # keep the notice time of the showcase story
        out.append(b)
        live_count += 1
    if live_count == len(fixtures):
        return out, SOURCE_PIPELINE
    return out, SOURCE_FIXTURES if live_count == 0 else SOURCE_MIXED


# --------------------------------------------------------------------------- data-source switch

SOURCE_WORLD = "world"
DATA_SOURCE_ENV = "WAPSI_DATA_SOURCE"  # showcase (default) | demo | <world name> | <path to a parquet world>
MAX_CASES_ENV = "WAPSI_UI_MAX_CASES"  # cases processed from a world at start-up (0 = all)
DEFAULT_MAX_CASES = 12
GENERATED_DIR = REPO_ROOT / "data" / "generated"
GOLDEN_DIR = REPO_ROOT / "data" / "golden"


@dataclass(frozen=True)
class SourceSpec:
    """Which case data the UI shows: the hand-computed showcase world, or a synthetic parquet world."""

    kind: str  # "showcase" | "world"
    key: str  # what WAPSI_DATA_SOURCE / the sidebar switch hold: "showcase", "demo" or a path
    label: str
    path: Path | None = None


SHOWCASE_SOURCE = SourceSpec("showcase", "showcase", "Showcase world (2 hand-computed cases)")


def resolve_source(raw: str | None = None) -> SourceSpec:
    """``WAPSI_DATA_SOURCE`` (or ``raw``) -> SourceSpec.

    ``""`` / ``showcase`` -> the showcase world; a name such as ``demo`` -> ``data/generated/<name>``; anything else
    is a path to a parquet world directory (``python -m wapsi.synth --out <dir>``). Raises ``ValueError`` when the
    world directory does not exist.
    """
    raw = (os.environ.get(DATA_SOURCE_ENV, "") if raw is None else raw).strip()
    if raw.lower() in ("", "showcase"):
        return SHOWCASE_SOURCE
    named = GENERATED_DIR / raw
    path = named if "/" not in raw and "\\" not in raw and named.is_dir() else Path(raw).expanduser()
    if not path.is_dir():
        raise ValueError(f"no synthetic world at {path} (run `uv run python -m wapsi.synth --size demo --out {path}`)")
    path = path.resolve()
    demo = path.name == "demo" and path.parent == GENERATED_DIR
    return SourceSpec("world", raw, "Demo world (synthetic)" if demo else f"Synthetic world · {path.name}", path)


def available_sources(current: SourceSpec | None = None) -> list[SourceSpec]:
    """The sources the sidebar switch offers: showcase, the local demo world when generated, and ``current`` (in
    place of the entry for the same data, so the switch shows the key the session was built from)."""
    out = [SHOWCASE_SOURCE]
    if (GENERATED_DIR / "demo" / "transactions.parquet").exists():
        out.append(resolve_source("demo"))
    if current is not None:
        same = [i for i, s in enumerate(out) if (s.kind, s.path) == (current.kind, current.path)]
        if same:
            out[same[0]] = current
        else:
            out.append(current)
    return out


def world_case_groups(ledger: Any, world_dir: Path, limit: int | None = None) -> list[list[str]]:
    """NCRP ids per case for a world: the golden set's cases when there is one (``data/golden/<name>/cases.jsonl``
    for a generated world, else ``<world>/golden/cases.jsonl``), else ``wapsi.pipeline.run.group_complaints`` over
    every complaint. ``limit`` keeps the first N (``None`` / 0 = all)."""
    groups: list[list[str]] = []
    candidates = [world_dir / "golden" / "cases.jsonl"]
    if world_dir.parent == GENERATED_DIR:  # a committed golden set belongs to the generated world of that name
        candidates.insert(0, GOLDEN_DIR / world_dir.name / "cases.jsonl")
    for golden in candidates:
        if golden.exists():
            for line in golden.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    groups.append(sorted(json.loads(line)["ncrp_ids"]))
            break
    if not groups:
        group_complaints = _optional("wapsi.pipeline.run", "group_complaints")
        groups = group_complaints(ledger, ledger.complaints())
    return groups[:limit] if limit else groups


def max_world_cases() -> int | None:
    raw = os.environ.get(MAX_CASES_ENV, "").strip()
    try:
        n = int(raw) if raw else DEFAULT_MAX_CASES
    except ValueError:
        log.warning("ignoring invalid %s=%r", MAX_CASES_ENV, raw)
        n = DEFAULT_MAX_CASES
    return n if n > 0 else None


def build_world_bundles(ledger: Any, groups: list[list[str]]) -> tuple[list[CaseBundle], str]:
    """(bundles, source) for a synthetic parquet world: every group through the live pipeline with the offline
    ``FakeLLM``. A case that fails is skipped (logged); there are no hand-built fixtures to fall back to."""
    process_case = _optional("wapsi.pipeline.run", "process_case")
    fake_llm_cls = _optional("wapsi.adapters.llm_fake", "FakeLLM")
    search = doc_search()
    out: list[CaseBundle] = []
    for ncrp_ids in groups:
        try:
            # start-up batch: recorded prose (else the stub), never a burst of live calls
            llm = prose_llm(fake_llm_cls, prefer="recorded", allow_live=False)
            out.append(process_case(ledger, list(ncrp_ids), llm=llm, search=search))
        except Exception:  # noqa: BLE001 - one bad case must not hide the others
            log.exception("pipeline failed for complaints %s; case skipped", ", ".join(ncrp_ids))
    return out, SOURCE_WORLD


def load_source(spec: SourceSpec, *, live: bool = True) -> tuple[list[CaseBundle], Any, str]:
    """(bundles, ledger, source) for a data source. The showcase honours ``live`` (pipeline vs fixtures); a
    world is always processed by the pipeline (``WAPSI_UI_MAX_CASES`` cases, default 12)."""
    if spec.kind != "world" or spec.path is None:
        from wapsi.fixtures.showcase import showcase_ledger

        ledger = showcase_ledger()
        bundles, source = build_showcase_bundles(ledger, live=live)
        return bundles, ledger, source
    parquet_ledger = _optional("wapsi.adapters.parquet_ledger", "ParquetLedger")
    ledger = parquet_ledger(spec.path)
    bundles, source = build_world_bundles(ledger, world_case_groups(ledger, spec.path, max_world_cases()))
    return bundles, ledger, source


def engine_status() -> EngineStatus:
    return EngineStatus({label: _module_exists(mod) for label, mod in ENGINE_MODULES.items()})


def now_for_ui() -> datetime:
    """Wall clock, or ``WAPSI_NOW`` (ISO 8601) to freeze the demo clock."""
    raw = os.environ.get("WAPSI_NOW", "").strip()
    if raw:
        try:
            return ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            log.warning("ignoring invalid WAPSI_NOW=%r", raw)
    return utcnow()


# --------------------------------------------------------------------------- derived views (pure)


def plan_reports(bundle: CaseBundle) -> list[VerificationReport]:
    """Verification reports that are not about a draft (e.g. the pipeline's hold-plan check)."""
    draft_ids = {d.draft_id for d in bundle.drafts}
    return [r for k, r in bundle.verifications.items() if k not in draft_ids]


def latest_approval(bundle: CaseBundle, draft_id: str | None) -> Approval | None:
    matches = [a for a in bundle.approvals if a.draft_id == draft_id]
    return max(matches, key=lambda a: a.at) if matches else None


def clock_status(clock: Clock, now: datetime) -> ClockStatus:
    """Display status: DONE stays done; otherwise overdue / warning / open against ``now``."""
    if clock.status == ClockStatus.DONE:
        return ClockStatus.DONE
    now = ensure_utc(now)
    if ensure_utc(clock.due) < now:
        return ClockStatus.OVERDUE
    if clock.warn_at is not None and ensure_utc(clock.warn_at) <= now:
        return ClockStatus.WARNING
    if clock.status in (ClockStatus.WARNING, ClockStatus.OVERDUE):
        return clock.status
    return ClockStatus.OPEN


def screen_text(text: str) -> GuardResult | None:
    """Screen untrusted text with the engine's RulesGuard when installed; None when it isn't."""
    try:
        guard = _optional("wapsi.verify.injection", "RulesGuard")()
        result = guard.screen(text)
    except Exception:  # noqa: BLE001 - the guard is optional for display purposes
        return None
    return result if isinstance(result, GuardResult) else None


def load_eval_report(path: Path | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """(report, error). (None, None) means no report yet."""
    env = os.environ.get("WAPSI_EVAL_REPORT", "").strip()
    target = path or (Path(env) if env else DEFAULT_EVAL_REPORT)
    if not target.exists():
        return None, None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"Couldn't read {target.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{target.name} is not a JSON object"
    return data, None


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class DataSource(Protocol):
    def list_cases(self) -> list[Case]: ...

    def get_bundle(self, case_id: str) -> CaseBundle | None: ...

    def save_approval(self, case_id: str, *, draft_id: str | None, decision: ApprovalDecision, approver: str,
                      role: str, reason_code: str = "", comment: str = "") -> Outcome: ...

    def run_pipeline(self, case_id: str, *, actor: str, role: str) -> Outcome: ...

    def append_audit(self, case_id: str | None, event: AuditEvent) -> None: ...

    def audit_events(self) -> list[tuple[str | None, AuditEvent]]: ...

    def clocks(self, bundle: CaseBundle, now: datetime) -> list[Clock]: ...

    def ledger(self) -> Any | None: ...


# --------------------------------------------------------------------------- fixture-backed implementation


def _audit_event(action: str, object_type: str, object_id: str, *, actor: str, role: str,
                 at: datetime | None = None, **details: Any) -> AuditEvent:
    return AuditEvent(at=at or utcnow(), actor=actor, role=role, action=action, object_type=object_type,
                      object_id=object_id, details=details)


def _chain(bundle: CaseBundle, event: AuditEvent) -> None:
    """Append ``event`` to the case's hash-chained, append-only audit log (``wapsi.audit``)."""
    append_event(bundle.audit, event, case_id=bundle.case.case_id)


class ShowcaseDataSource:
    """Session-scoped in-memory store over the showcase world; writes (approvals, audit) stay in the session.

    ``bundles=None`` builds them with :func:`build_showcase_bundles` (``live`` selects pipeline vs fixtures).
    A synthetic parquet world (:func:`load_source`) is served the same way: pass its bundles, its ledger,
    ``source=SOURCE_WORLD`` and a ``label`` for the sidebar.
    """

    def __init__(self, bundles: list[CaseBundle] | None = None, ledger: Any | None = None, *,
                 live: bool = False, source: str | None = None, label: str | None = None) -> None:
        if bundles is None:
            bundles, source = build_showcase_bundles(ledger, live=live)
        self.source = source or SOURCE_FIXTURES
        self.label = label
        self._bundles: dict[str, CaseBundle] = {b.case.case_id: b for b in bundles}
        self._global_audit: list[AuditEvent] = []
        self._ledger = ledger
        self._clock_cache: dict[tuple[str, int, int], list[Clock]] = {}

    # -- reads
    def list_cases(self) -> list[Case]:
        return [b.case for b in self._bundles.values()]

    def get_bundle(self, case_id: str) -> CaseBundle | None:
        return self._bundles.get(case_id)

    def bundles(self) -> list[CaseBundle]:
        return list(self._bundles.values())

    def ledger(self) -> Any | None:
        if self._ledger is None:
            from wapsi.fixtures.showcase import showcase_ledger

            self._ledger = showcase_ledger()
        return self._ledger

    def audit_events(self) -> list[tuple[str | None, AuditEvent]]:
        out: list[tuple[str | None, AuditEvent]] = [(None, e) for e in self._global_audit]
        for cid, b in self._bundles.items():
            out.extend((cid, e) for e in b.audit)
        return sorted(out, key=lambda x: ensure_utc(x[1].at), reverse=True)

    def clocks(self, bundle: CaseBundle, now: datetime) -> list[Clock]:
        """Stored clocks, else clocks computed by ``wapsi.domain.clocks`` when it exists, else []."""
        if bundle.clocks:
            clocks = list(bundle.clocks)
        else:
            key = (bundle.case.case_id, len(bundle.approvals), len(bundle.drafts))
            if key not in self._clock_cache:
                self._clock_cache[key] = self._compute_clocks(bundle, now)
            clocks = self._clock_cache[key]
        try:
            evaluate_clocks = _optional("wapsi.domain.clocks", "evaluate_clocks")
        except ImportError:
            return clocks
        try:
            evaluated = evaluate_clocks(clocks, now)
        except Exception:  # noqa: BLE001 - never let a helper break the page
            log.exception("evaluate_clocks failed")
            return clocks
        if isinstance(evaluated, list) and all(isinstance(c, Clock) for c in evaluated):
            return evaluated
        return clocks

    @staticmethod
    def _compute_clocks(bundle: CaseBundle, now: datetime) -> list[Clock]:
        try:
            clocks_for_bundle = _optional("wapsi.domain.clocks", "clocks_for_bundle")
        except ImportError:
            return []
        try:
            return list(clocks_for_bundle(bundle, now))
        except Exception:  # noqa: BLE001
            log.exception("clocks_for_bundle failed for %s", bundle.case.case_id)
            return []

    # -- writes
    def append_audit(self, case_id: str | None, event: AuditEvent) -> None:
        """Append a copy of ``event`` to the case's hash-chained audit log (session-level events such as role
        switches go to the session's own chain)."""
        if case_id and case_id in self._bundles:
            _chain(self._bundles[case_id], event.model_copy(deep=True))
        else:
            append_event(self._global_audit, event.model_copy(deep=True), case_id=None)

    def verify_audit(self, case_id: str | None) -> ChainReport:
        """Verify a case's audit chain (``None``: the session's own events)."""
        if case_id is None:
            return verify_chain(self._global_audit, case_id=None)
        bundle = self._bundles[case_id]
        return verify_chain(bundle.audit, case_id=case_id)

    def save_approval(self, case_id: str, *, draft_id: str | None, decision: ApprovalDecision, approver: str,
                      role: str, reason_code: str = "", comment: str = "") -> Outcome:
        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        role = role.lower()
        if role != "supervisor":
            return Outcome(False, "Only a supervisor can record an approval decision (maker-checker).")
        if not reason_code:
            return Outcome(False, "Pick a reason before recording the decision.")

        if draft_id is not None:
            try:
                approve = _optional("wapsi.pipeline.run", "approve")
            except ImportError:
                approve = None
            if approve is not None:
                work = bundle.model_copy(deep=True)
                try:
                    from wapsi.adapters.memory import RecordingActionSink

                    updated = approve(work, draft_id, approver=approver, role=role, decision=decision,
                                      reason_code=reason_code, comment=comment, ledger=self.ledger(),
                                      sink=RecordingActionSink())
                except PermissionError as exc:
                    return Outcome(False, f"Not allowed: {exc}")
                except ValueError as exc:
                    return Outcome(False, f"Can't approve yet: {exc}")
                except KeyError:
                    return Outcome(False, "That draft no longer exists. Generate drafts again.")
                except ImportError:
                    updated = None  # engine partly present; fall back to the local record below
                except Exception as exc:  # noqa: BLE001
                    log.exception("approve failed")
                    return Outcome(False, f"The decision couldn't be recorded: {exc}")
                if updated is not None:
                    self._bundles[case_id] = updated
                    return Outcome(True, _decision_message(decision), bundle=updated)
            draft = next((d for d in bundle.drafts if d.draft_id == draft_id), None)
            if draft is None:
                return Outcome(False, "That draft no longer exists. Generate drafts again.")
            report = bundle.verifications.get(draft_id)
            if decision == ApprovalDecision.APPROVED and (report is None or not report.passed):
                return Outcome(False, "Can't approve yet: this draft hasn't passed every check.")
            object_type, object_id, prefix = "draft", draft_id, "draft"
        else:
            if bundle.plan is None:
                return Outcome(False, "There is no hold plan to approve yet.")
            failing = [r for r in plan_reports(bundle) if not r.passed]
            if decision == ApprovalDecision.APPROVED and failing:
                return Outcome(False, "Can't approve yet: the hold plan failed a policy check.")
            object_type, object_id, prefix = "plan", case_id, "plan"

        now = utcnow()
        approval = Approval(
            approval_id=f"APR-{uuid.uuid4().hex[:10]}", case_id=case_id, draft_id=draft_id, decision=decision,
            approver=approver, role=role, reason_code=reason_code, comment=comment, at=now,
        )
        bundle.approvals.append(approval)
        # the decided content's fingerprint is sealed into the audit chain with the decision
        fingerprint = ({"plan_sha256": sha256_hex(bundle.plan.model_dump(mode="json"))}
                       if draft_id is None and bundle.plan is not None else {})
        _chain(bundle, _audit_event(f"{prefix}_{decision.value.lower()}", object_type, object_id,
                                    actor=approver, role=role, at=now, reason_code=reason_code,
                                    comment=comment, approval_id=approval.approval_id, **fingerprint))
        return Outcome(True, _decision_message(decision), bundle=bundle)

    def run_pipeline(self, case_id: str, *, actor: str, role: str) -> Outcome:
        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        missing_msg = ("Drafting isn't switched on in this build yet. The hold plan is ready to review; drafts, "
                       "checks and clocks will appear here once the drafting engine is installed.")
        try:
            process_case = _optional("wapsi.pipeline.run", "process_case")
            fake_llm_cls = _optional("wapsi.adapters.llm_fake", "FakeLLM")
        except ImportError as exc:
            _chain(bundle, _audit_event("pipeline_unavailable", "case", case_id, actor=actor, role=role,
                                        missing=str(exc)))
            return Outcome(False, missing_msg, unavailable=True)
        search = doc_search()
        try:
            fresh = process_case(self.ledger(), list(bundle.case.ncrp_ids),
                                 llm=prose_llm(fake_llm_cls, fresh=True), search=search,
                                 case_id=case_id, assigned_to=bundle.case.assigned_to)
        except ImportError as exc:
            _chain(bundle, _audit_event("pipeline_unavailable", "case", case_id, actor=actor, role=role,
                                        missing=str(exc)))
            return Outcome(False, missing_msg, unavailable=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("process_case failed for %s", case_id)
            _chain(bundle, _audit_event("pipeline_failed", "case", case_id, actor=actor, role=role,
                                        error=str(exc)))
            return Outcome(False, f"Drafts couldn't be generated: {exc}")
        fresh.case.opened_at = bundle.case.opened_at
        fresh.approvals = list(bundle.approvals) + list(fresh.approvals)
        fresh.audit = chain_extend(bundle.audit, fresh.audit, case_id=case_id)  # history first, one chain
        _chain(fresh, _audit_event("pipeline_run", "case", case_id, actor=actor, role=role,
                                   drafts=len(fresh.drafts)))
        self._bundles[case_id] = fresh
        self._clock_cache = {k: v for k, v in self._clock_cache.items() if k[0] != case_id}
        n = len(fresh.drafts)
        failed = sum(1 for d in fresh.drafts if not (fresh.verifications.get(d.draft_id)
                                                     and fresh.verifications[d.draft_id].passed))
        msg = f"Generated {n} draft{'s' if n != 1 else ''}"
        msg += " — all passed their checks." if failed == 0 else f" — {failed} need attention."
        return Outcome(True, msg, bundle=fresh)


    def request_account_level(self, case_id: str, account_id: str, reasons: list[str], *, actor: str, role: str,
                              note: str = "") -> Outcome:
        """An officer (maker) requests an exceptional account-level hold; a different supervisor approves it in
        Approvals. Amount-specific holds stay the default; nothing changes in the plan until then."""
        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        if not reasons:
            return Outcome(False, "Record at least one reason from the list before sending the request.")
        try:
            request = _optional("wapsi.pipeline.run", "request_account_level_hold")
        except ImportError:
            return Outcome(False, "Account-level requests aren't available in this build.", unavailable=True)
        work = bundle.model_copy(deep=True)
        try:
            draft = request(work, self.ledger(), account_id, list(reasons), requested_by=actor, role=role.lower(),
                            note=note, search=doc_search())
        except PermissionError as exc:
            return Outcome(False, f"Not allowed: {exc}")
        except (KeyError, ValueError) as exc:
            return Outcome(False, f"Can't request it: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("request_account_level_hold failed")
            return Outcome(False, f"The request couldn't be recorded: {exc}")
        self._bundles[case_id] = work
        return Outcome(True, "Request recorded. A supervisor other than you decides on it in Approvals.",
                       bundle=work, data=draft)

    def check_lea_request(self, case_id: str, request: Any, *, actor: str, role: str) -> Outcome:
        """Check a police / LEA hold request against the eight-field checklist (``wapsi.verify.lea_request``);
        the result (``Outcome.data``) is recorded as an audit event. The plan is never changed."""
        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        try:
            check_lea = _optional("wapsi.pipeline.run", "check_lea")
        except ImportError:
            return Outcome(False, "The LEA request check isn't available in this build.", unavailable=True)
        try:
            result = check_lea(bundle, request, self.ledger(), now=now_for_ui(), actor=actor, role=role.lower())
        except Exception as exc:  # noqa: BLE001
            log.exception("check_lea failed")
            return Outcome(False, f"The request couldn't be checked: {exc}")
        msg = ("Every checklist field is present." if result.complete
               else f"{len(result.missing)} of 8 checklist fields missing or unclear.")
        return Outcome(True, msg, bundle=bundle, data=result)

    def open_case_for_ncrp(self, ncrp_ids: list[str], *, actor: str, role: str,
                           details: dict[str, Any] | None = None) -> Outcome:
        """Open the case for complaints a voice note was matched to (``wapsi.intake``): an existing case that holds
        them is linked, otherwise the pipeline runs for exactly these NCRP ids. Amounts come from the ledger; the
        voice note only names the complaint. The link is recorded in the case's audit chain."""
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        ids = sorted(dict.fromkeys(str(i).strip() for i in ncrp_ids if str(i).strip()))
        if not ids:
            return Outcome(False, "No NCRP complaint matched this voice note, so there is no case to open.")
        extra = dict(details or {})
        existing = next((b for b in self._bundles.values() if set(ids) <= set(b.case.ncrp_ids)), None)
        if existing is not None:
            cid = existing.case.case_id
            _chain(existing, _audit_event("voice_intake_linked", "case", cid, actor=actor, role=role,
                                          ncrp_ids=ids, **extra))
            return Outcome(True, f"Voice note linked to {cid}. Every amount shown comes from the ledger.",
                           bundle=existing)
        try:
            process_case = _optional("wapsi.pipeline.run", "process_case")
            fake_llm_cls = _optional("wapsi.adapters.llm_fake", "FakeLLM")
        except ImportError:
            return Outcome(False, "The case pipeline isn't installed in this build.", unavailable=True)
        case_id = f"CASE-{ids[0]}"
        try:
            fresh = process_case(self.ledger(), ids, llm=prose_llm(fake_llm_cls, fresh=True), search=doc_search(),
                                 case_id=case_id, assigned_to=actor)
        except KeyError:
            return Outcome(False, f"Complaint(s) {', '.join(ids)} are not in this ledger.")
        except Exception as exc:  # noqa: BLE001
            log.exception("process_case failed for %s", case_id)
            return Outcome(False, f"The case couldn't be opened: {exc}")
        _chain(fresh, _audit_event("voice_intake_opened", "case", case_id, actor=actor, role=role, ncrp_ids=ids,
                                   **extra))
        self._bundles[case_id] = fresh
        return Outcome(True, f"Opened {case_id} from the voice note and ran the pipeline. Every amount comes from "
                             "the ledger.", bundle=fresh)


def voice_llm() -> Any:
    """The model that reads voice notes: Gemini when a key (``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``) or Agent
    Platform (``WAPSI_GEMINI_BACKEND=vertex`` + a project) is configured, else the offline ``FakeLLM``, which knows
    the bundled synthetic samples only. Never prints or stores the key."""
    from wapsi.adapters.gemini_llm import DEFAULT_MODEL, GeminiLLM, gemini_api_key
    from wapsi.adapters.llm_fake import FakeLLM

    model = os.environ.get("WAPSI_VOICE_MODEL") or os.environ.get("WAPSI_MODEL") or DEFAULT_MODEL
    backend = os.environ.get("WAPSI_GEMINI_BACKEND", "aistudio").strip().lower()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("WAPSI_PROJECT")
    if os.environ.get("WAPSI_VOICE_OFFLINE", "").strip() != "1":
        if backend == "vertex" and project:
            return GeminiLLM(model, project=project, backend="vertex", max_retries=2, timeout_s=90)
        if backend != "vertex" and gemini_api_key():
            return GeminiLLM(model, backend="aistudio", max_retries=2, timeout_s=90)
    return FakeLLM(name="offline demo model")


FixtureDataSource = ShowcaseDataSource  # backwards-compatible name


def _decision_message(decision: ApprovalDecision) -> str:
    return {
        ApprovalDecision.APPROVED: "Approved. The decision is recorded in the audit trail.",
        ApprovalDecision.CHANGES_REQUESTED: "Sent back with a change request.",
        ApprovalDecision.REJECTED: "Rejected. The decision is recorded in the audit trail.",
    }[decision]
