"""Streamlit data source for the GCP edition: the shared UI reads and writes cases through the ``CaseStore``
(Firestore on Cloud Run) and the ``Ledger`` (BigQuery), so it shows the same cases the agents and the Pub/Sub
pipeline create. Launched by ``editions/gcp/ui_app.py``.

In demo mode an empty store is seeded with the showcase cases and, with ``seed_agent_trace=True`` (the UI service),
one offline run of the real ADK agent (scripted model, ``trace_demo.DEMO_SCRIPT``) whose traces land in the
showcase cases' hash-chained audit logs, so the Case Room's Agent trace tab shows a run recorded on this instance.

On the shared judging deployment (``WAPSI_SEED_CASES=showcase``, or demo mode) every visitor works on the same
stored showcase cases. The sidebar's **Reset demo** (``reset_demo``) restores them to their freshly seeded state,
and a new session resets them by itself when someone worked on them and nobody has touched them for
``WAPSI_DEMO_IDLE_RESET_MIN`` minutes (default 30), so the next judge starts on a fresh case. The audit log keeps
every earlier event and records each reset (``seed.seed_cases``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ui.data_source import Outcome, ShowcaseDataSource, build_showcase_bundles, doc_search
from wapsi.audit.chain import ChainReport, append_event, chain_extend
from wapsi.models import ApprovalDecision, AuditEvent, CaseBundle
from wapsi.timeutil import utcnow

from .services import Services

log = logging.getLogger("wapsi.gcp.ui")
SOURCE_CASE_STORE = "case_store"


class CaseStoreDataSource(ShowcaseDataSource):
    def __init__(self, services: Services, *, seed_demo: bool = True, seed_agent_trace: bool = False) -> None:
        self.services = services
        self.demo_notice: str | None = None  # what the session's start did to the shared demo (idle reset)
        store = services.store
        if self.demo_reset_enabled:
            self._idle_reset()
        bundles = [b for b in (store.get_bundle(c.case_id) for c in store.list_cases()) if b is not None]
        source = SOURCE_CASE_STORE
        if not bundles and seed_demo and services.settings.demo_mode:
            bundles, source = build_showcase_bundles(services.ledger, live=True)
            for b in bundles:
                store.put_bundle(b)
            if seed_agent_trace and os.environ.get("WAPSI_SEED_AGENT_TRACE", "1").strip() != "0":
                bundles = self._seed_agent_trace(bundles)
        super().__init__(bundles=bundles, ledger=services.ledger, source=source)

    def _seed_agent_trace(self, bundles: list[CaseBundle]) -> list[CaseBundle]:
        """One offline run of the ADK agent on the showcase cases (demo mode); the store keeps its traces."""
        try:
            from .trace_demo import seed_case_traces

            traces = seed_case_traces(self.services)
        except Exception:  # noqa: BLE001 - the panel falls back to the recorded run
            log.exception("could not seed the agent trace demo")
            return bundles
        log.info("seeded %d offline agent runs", len(traces))
        store = self.services.store
        return [store.get_bundle(b.case.case_id) or b for b in bundles]

    # -- the shared judging demo
    @property
    def demo_reset_enabled(self) -> bool:
        """A public judging deployment (``WAPSI_SEED_CASES=showcase``) or demo mode: visitors share the stored
        showcase cases, so the sidebar offers **Reset demo**."""
        from .seed import SEED_ENV, seed_choice

        try:
            seeded = seed_choice(os.environ.get(SEED_ENV)) == "showcase"
        except ValueError:
            seeded = False
        return seeded or bool(self.services.settings.demo_mode)

    def demo_decisions(self) -> int:
        """Decisions recorded on the showcase cases (by anyone) since they were last seeded or reset."""
        from .seed import SHOWCASE_CASE_IDS

        return sum(len(b.approvals) for cid, b in self._bundles.items() if cid in SHOWCASE_CASE_IDS)

    def _idle_reset(self) -> None:
        from .seed import reset_if_idle

        try:
            results = reset_if_idle(self.services)
        except Exception:  # noqa: BLE001 - a new session must open even if the reset fails
            log.exception("idle reset of the shared demo failed")
            return
        if results and "reset" in results.values() and all(v in ("reset", "fresh", "seeded")
                                                            for v in results.values()):
            self.demo_notice = ("The shared demo had been idle, so the showcase cases were reset for you: no "
                                "decisions recorded yet. Earlier visits stay in each case's audit log.")

    def reset_demo(self, *, actor: str, role: str) -> Outcome:
        """Restore the showcase cases to their freshly seeded state (plan and drafts as computed, no decisions).
        Every earlier event, including each decision, stays in the case's audit log, followed by the reset."""
        from .seed import SHOWCASE_CASE_IDS, seed_cases

        if not self.demo_reset_enabled:
            return Outcome(False, "Reset is only available on the shared demo deployment.")
        try:
            results = seed_cases(self.services, "showcase", reset=True, actor=actor, role=role, wait_s=20.0,
                                 reason="reset from the sidebar of the shared demo")
        except Exception as exc:  # noqa: BLE001
            log.exception("demo reset failed")
            return Outcome(False, f"The demo couldn't be reset: {exc}")
        for cid in SHOWCASE_CASE_IDS:
            try:
                fresh = self.services.store.get_bundle(cid)
            except Exception:  # noqa: BLE001
                log.exception("could not re-read case %s", cid)
                fresh = None
            if fresh is not None:
                self._bundles[cid] = fresh
        self._clock_cache = {}
        values = set(results.values())
        if values <= {"reset", "seeded", "fresh"}:
            if values <= {"fresh"}:
                return Outcome(True, "Nothing to reset: the showcase cases are already fresh, with no decisions "
                                     "recorded.")
            return Outcome(True, "Demo reset: the showcase cases are back to their freshly seeded state, with no "
                                 "decisions recorded. Earlier decisions stay in each case's audit log.")
        if "busy" in values:
            return Outcome(False, "Another reset is running. Try again in a few seconds.")
        if "missing_in_ledger" in values:
            return Outcome(False, "The showcase complaints aren't in this deployment's ledger, so there is nothing "
                                  "to reset.")
        return Outcome(False, "Some showcase cases couldn't be reset. Try again, or check the service logs.")

    def refresh(self) -> None:
        """Pick up cases created elsewhere (agents, Pub/Sub pipeline)."""
        for c in self.services.store.list_cases():
            b = self.services.store.get_bundle(c.case_id)
            if b is not None:
                self._bundles[c.case_id] = b

    def list_cases(self) -> list[Any]:
        self.refresh()
        return super().list_cases()

    def _persist(self, bundle: CaseBundle | None) -> None:
        if bundle is None:
            return
        try:
            self.services.store.put_bundle(bundle)
        except Exception:  # noqa: BLE001 - the page shows the in-session state; log for the operator
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
            # hand the store the link just sealed into the session's chain; the store seals it onto its own head
            # (Firestore and the in-memory store both do) and stamps it with the seal it recorded (the receipt)
            local = self._bundles[case_id].audit[-1]
            sealed = local.model_copy(deep=True)
            try:
                self.services.store.append_audit(case_id, sealed)
            except Exception:  # noqa: BLE001
                log.exception("could not append audit for %s", case_id)
                return
            if (sealed.seq, sealed.hash) != (local.seq, local.hash):
                # the store holds events this session has not seen (agents, the Pub/Sub pipeline): catch up, so
                # the page shows the store's chain
                self._catch_up(case_id)

    def _catch_up(self, case_id: str) -> None:
        try:
            fresh = self.services.store.get_bundle(case_id)
        except Exception:  # noqa: BLE001
            log.exception("could not re-read case %s", case_id)
            return
        if fresh is not None:
            self._bundles[case_id] = fresh

    def verify_audit(self, case_id: str | None) -> ChainReport:
        """A case's chain as the case store holds it, checked against the store's anchor (Firestore:
        ``audit_anchors/{case_id}``), so the audit page's badge reports what is in production, not the session's
        copy. Session-level events (``None``) and cases the store does not hold are verified in the session."""
        verify = getattr(self.services.store, "verify_audit", None)
        if case_id is not None and callable(verify):
            try:
                return verify(case_id)
            except KeyError:
                pass  # not in the store (yet)
            except Exception:  # noqa: BLE001 - the page falls back to the session's copy; log for the operator
                log.exception("could not verify the stored audit chain of %s", case_id)
        return super().verify_audit(case_id)

    def run_pipeline(self, case_id: str, *, actor: str, role: str) -> Outcome:
        """Re-run the pipeline with the edition's LLM (Gemini / Gemma) and guard, then persist."""
        from wapsi.pipeline.run import process_case

        bundle = self._bundles.get(case_id)
        if bundle is None:
            return Outcome(False, f"Case {case_id} was not found.")
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        svc = self.services
        try:
            fresh = process_case(svc.ledger, list(bundle.case.ncrp_ids), llm=svc.llm, cfg=svc.cfg,
                                 search=svc.search or doc_search(), guard=svc.guard, case_id=case_id,
                                 assigned_to=bundle.case.assigned_to)
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


    def open_case_for_ncrp(self, ncrp_ids: list[str], *, actor: str, role: str,
                           details: dict[str, Any] | None = None) -> Outcome:
        """Voice intake's "Open case": link or open the case, then persist it (and its audit link) to the store."""
        out = super().open_case_for_ncrp(ncrp_ids, actor=actor, role=role, details=details)
        if out.ok:
            self._persist(out.bundle)
        return out
