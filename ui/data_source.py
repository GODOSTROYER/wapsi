"""Where the UI reads and writes case data.

The showcase world is processed by the real pipeline (``wapsi.pipeline.run.process_case`` with the offline
``FakeLLM``) when the engine is installed, and falls back to the hand-built bundles in ``wapsi.fixtures.bundles``
otherwise. Engine modules are imported lazily, inside try/except, so a partial install never breaks a page.
Editions swap ``ShowcaseDataSource`` for a Snowflake / BigQuery / Postgres-backed implementation of the same
``DataSource`` protocol.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
            b = process_case(ledger, list(fx.case.ncrp_ids), llm=fake_llm_cls(), search=search,
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


class ShowcaseDataSource:
    """Session-scoped in-memory store over the showcase world; writes (approvals, audit) stay in the session.

    ``bundles=None`` builds them with :func:`build_showcase_bundles` (``live`` selects pipeline vs fixtures).
    """

    def __init__(self, bundles: list[CaseBundle] | None = None, ledger: Any | None = None, *,
                 live: bool = False, source: str | None = None) -> None:
        if bundles is None:
            bundles, source = build_showcase_bundles(ledger, live=live)
        self.source = source or SOURCE_FIXTURES
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
        if case_id and case_id in self._bundles:
            self._bundles[case_id].audit.append(event)
        else:
            self._global_audit.append(event)

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
        bundle.approvals.append(Approval(
            approval_id=f"APR-{uuid.uuid4().hex[:10]}", case_id=case_id, draft_id=draft_id, decision=decision,
            approver=approver, role=role, reason_code=reason_code, comment=comment, at=now,
        ))
        bundle.audit.append(_audit_event(f"{prefix}_{decision.value.lower()}", object_type, object_id,
                                         actor=approver, role=role, at=now, reason_code=reason_code,
                                         comment=comment))
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
            bundle.audit.append(_audit_event("pipeline_unavailable", "case", case_id, actor=actor, role=role,
                                             missing=str(exc)))
            return Outcome(False, missing_msg, unavailable=True)
        search = doc_search()
        try:
            fresh = process_case(self.ledger(), list(bundle.case.ncrp_ids), llm=fake_llm_cls(), search=search,
                                 case_id=case_id, assigned_to=bundle.case.assigned_to)
        except ImportError as exc:
            bundle.audit.append(_audit_event("pipeline_unavailable", "case", case_id, actor=actor, role=role,
                                             missing=str(exc)))
            return Outcome(False, missing_msg, unavailable=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("process_case failed for %s", case_id)
            bundle.audit.append(_audit_event("pipeline_failed", "case", case_id, actor=actor, role=role,
                                             error=str(exc)))
            return Outcome(False, f"Drafts couldn't be generated: {exc}")
        fresh.case.opened_at = bundle.case.opened_at
        fresh.approvals = list(bundle.approvals) + list(fresh.approvals)
        fresh.audit = list(bundle.audit) + list(fresh.audit)
        fresh.audit.append(_audit_event("pipeline_run", "case", case_id, actor=actor, role=role,
                                        drafts=len(fresh.drafts)))
        self._bundles[case_id] = fresh
        self._clock_cache = {k: v for k, v in self._clock_cache.items() if k[0] != case_id}
        n = len(fresh.drafts)
        failed = sum(1 for d in fresh.drafts if not (fresh.verifications.get(d.draft_id)
                                                     and fresh.verifications[d.draft_id].passed))
        msg = f"Generated {n} draft{'s' if n != 1 else ''}"
        msg += " — all passed their checks." if failed == 0 else f" — {failed} need attention."
        return Outcome(True, msg, bundle=fresh)


FixtureDataSource = ShowcaseDataSource  # backwards-compatible name


def _decision_message(decision: ApprovalDecision) -> str:
    return {
        ApprovalDecision.APPROVED: "Approved. The decision is recorded in the audit trail.",
        ApprovalDecision.CHANGES_REQUESTED: "Sent back with a change request.",
        ApprovalDecision.REJECTED: "Rejected. The decision is recorded in the audit trail.",
    }[decision]
