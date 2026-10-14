"""Streamlit data source for the AMD edition: the shared UI reads and writes cases through ``FileCaseStore`` and
re-runs the pipeline with the edition's LLM (vLLM-ROCm, or ``FakeLLM`` in demo mode), guard and scorer.
Launched by ``editions/amd/app/ui_app.py``.

It also gives the Case Room its "Ask the agent" box (``ask_agent`` / ``agent_status``): the Google ADK officer
agent on Gemma 4 served on AMD (``editions.amd.app.agent``), whose runs are sealed into the case's audit chain
and shown in the Agent trace tab.
"""

from __future__ import annotations

import logging
from typing import Any

from ui.data_source import Outcome, ShowcaseDataSource
from wapsi.audit.chain import append_event, chain_extend
from wapsi.models import ApprovalDecision, AuditEvent, CaseBundle
from wapsi.timeutil import utcnow

from .services import Services, process, seed_store, voice_model

log = logging.getLogger("wapsi.amd.ui")
SOURCE_CASE_STORE = "case_store"


class AmdDataSource(ShowcaseDataSource):
    def __init__(self, services: Services, *, seed_limit: int | None = None) -> None:
        self.services = services
        limit = services.settings.ui_seed_limit if seed_limit is None else seed_limit
        seed_store(services, limit)
        store = services.store
        bundles = [b for b in (store.get_bundle(c.case_id) for c in store.list_cases()) if b is not None]
        super().__init__(bundles=bundles, ledger=services.ledger, source=SOURCE_CASE_STORE)

    def refresh(self) -> None:
        """Pick up cases created elsewhere (API, benchmarks)."""
        for c in self.services.store.list_cases():
            b = self.services.store.get_bundle(c.case_id)
            if b is not None:
                self._bundles[c.case_id] = b

    def list_cases(self) -> list[Any]:
        self.refresh()
        return super().list_cases()

    def deployment_notice(self) -> tuple[str, str] | None:
        """Shown on every page when demo mode was switched on because no endpoint is configured (not on purpose)."""
        reason = self.services.settings.demo_reason
        return ("Offline stand-ins, no model endpoint configured", reason) if reason else None

    def voice_model(self) -> Any:
        """The edition's voice-intake model (the Voice Intake page asks the data source first): Whisper plus this
        edition's LLM on the open path, the offline model in demo mode. Built once, kept loaded."""
        return voice_model(self.services)

    def _persist(self, bundle: CaseBundle | None) -> None:
        if bundle is None:
            return
        try:
            self.services.store.put_bundle(bundle)
        except Exception:  # noqa: BLE001 - the page keeps the in-session state; log for the operator
            log.exception("could not persist case %s", bundle.case.case_id)

    def save_approval(self, case_id: str, *, draft_id: str | None, decision: ApprovalDecision, approver: str,
                      role: str, reason_code: str = "", comment: str = "") -> Outcome:
        out = super().save_approval(case_id, draft_id=draft_id, decision=decision, approver=approver, role=role,
                                    reason_code=reason_code, comment=comment)
        if out.ok:
            self._persist(self._bundles.get(case_id))
        return out

    def append_audit(self, case_id: str | None, event: AuditEvent) -> None:
        super().append_audit(case_id, event)
        if case_id and case_id in self._bundles:
            self._persist(self._bundles[case_id])

    # ------------------------------------------------------------------ the officer agent (ADK on AMD)
    def agent_status(self) -> dict[str, Any]:
        """Non-secret description of the agent (backend, model, serving platform, tool mode); no network call."""
        from .agent import agent_status

        try:
            return agent_status(self.services)
        except Exception as exc:  # noqa: BLE001 - the page shows why instead of failing
            log.exception("agent status failed")
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

    def ask_agent(self, case_id: str | None, text: str, *, actor: str, role: str) -> Outcome:
        """One officer message through the ADK agent; ``Outcome.message`` is the reply to show and
        ``Outcome.data`` the ``AgentRunResult``. The case is re-read from the store afterwards, so the page shows
        the sealed trace (and anything the agent's tools recorded)."""
        from . import agent as agent_mod

        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        try:
            result = agent_mod.ask(text, services=self.services, case_id=case_id, user_id=actor, role=role)
        except agent_mod.AgentUnavailable as exc:
            return Outcome(False, str(exc), unavailable=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("agent run failed")
            return Outcome(False, f"The agent could not run: {type(exc).__name__}")
        self.refresh()
        if case_id:
            self._clock_cache = {k: v for k, v in self._clock_cache.items() if k[0] != case_id}
        return Outcome(result.ok, result.reply, bundle=self._bundles.get(case_id) if case_id else None, data=result)

    def run_pipeline(self, case_id: str, *, actor: str, role: str) -> Outcome:
        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        svc = self.services
        try:
            fresh = process(svc, list(bundle.case.ncrp_ids), case_id=case_id, persist=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("process_case failed for %s", case_id)
            return Outcome(False, f"Drafts couldn't be generated: {exc}")
        fresh.case.opened_at = bundle.case.opened_at
        fresh.approvals = list(bundle.approvals) + list(fresh.approvals)
        fresh.audit = chain_extend(bundle.audit, fresh.audit, case_id=case_id)  # one hash chain, history first
        append_event(fresh.audit, AuditEvent(at=utcnow(), actor=actor, role=role, action="pipeline_run",
                                             object_type="case", object_id=case_id,
                                             details={"drafts": len(fresh.drafts),
                                                      "llm": getattr(svc.llm, "name", "")}), case_id=case_id)
        self._bundles[case_id] = fresh
        self._clock_cache = {k: v for k, v in self._clock_cache.items() if k[0] != case_id}
        self._persist(fresh)
        failed = sum(1 for d in fresh.drafts
                     if not (fresh.verifications.get(d.draft_id) and fresh.verifications[d.draft_id].passed))
        n = len(fresh.drafts)
        msg = f"Generated {n} draft{'s' if n != 1 else ''}"
        msg += " — all passed their checks." if failed == 0 else f" — {failed} need attention."
        return Outcome(True, msg, bundle=fresh)
