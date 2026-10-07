"""Streamlit data source for the GCP edition: the shared UI reads and writes cases through the ``CaseStore``
(Firestore on Cloud Run) and the ``Ledger`` (BigQuery), so it shows the same cases the agents and the Pub/Sub
pipeline create. Launched by ``editions/gcp/ui_app.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from ui.data_source import Outcome, ShowcaseDataSource, build_showcase_bundles, doc_search
from wapsi.audit.chain import ChainReport, append_event, chain_extend
from wapsi.models import ApprovalDecision, AuditEvent, CaseBundle
from wapsi.timeutil import utcnow

from .services import Services

log = logging.getLogger("wapsi.gcp.ui")
SOURCE_CASE_STORE = "case_store"


class CaseStoreDataSource(ShowcaseDataSource):
    def __init__(self, services: Services, *, seed_demo: bool = True) -> None:
        self.services = services
        store = services.store
        bundles = [b for b in (store.get_bundle(c.case_id) for c in store.list_cases()) if b is not None]
        source = SOURCE_CASE_STORE
        if not bundles and seed_demo and services.settings.demo_mode:
            bundles, source = build_showcase_bundles(services.ledger, live=True)
            for b in bundles:
                store.put_bundle(b)
        super().__init__(bundles=bundles, ledger=services.ledger, source=source)

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

