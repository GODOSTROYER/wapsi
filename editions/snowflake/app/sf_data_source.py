"""Streamlit data source for the Snowflake edition: the shared UI (``ui/``) reads cases from ``WAPSI.CASEMGMT`` and
writes through the stored procedures, so the app shows exactly what TRACE_CASE / BUILD_HOLD_PLAN / DRAFT wrote.

- Reads: ``CASE_BUNDLES`` via ``SnowflakeCaseStore`` (refreshed on every Inbox render).
- "Generate drafts": ``CALL WAPSI.CASEMGMT.DRAFT(case, 'ALL')`` (Cortex AI_COMPLETE prose + verifier).
- Approvals of drafts: ``CALL WAPSI.CASEMGMT.APPROVE(...)`` with the viewer's persona as ``P_ACTOR``; the
  procedure enforces supervisor-only (``APP.USER_ROLES``) and checker != maker. An approved account-level request
  is applied by the procedure's owner's-rights step (it redrafts the hold instruction and CFCFRMS update).
- Exceptional account-level hold requests: ``CALL WAPSI.CASEMGMT.REQUEST_ACCOUNT_LEVEL_HOLD(...)`` (maker = the
  viewer's persona; >= 1 recorded reason); police / LEA hold requests: ``CALL WAPSI.CASEMGMT.CHECK_LEA(...)``.
- Audit events of a case are sealed onto its hash chain in ``CASEMGMT.AUDIT_LOG`` (INSERT-only, plus a checkpoint in
  ``AUDIT_ANCHOR``); ``verify_audit`` reads the log back and checks it against the latest checkpoint.
- No ledger access from the app: the owner role has no RAW grant (holder names stay out of the app).
If the case tables are empty, the two showcase cases are traced and planned once so the app never opens blank.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from wapsi_sf.casestore import SnowflakeCaseStore

from ui.data_source import Outcome, ShowcaseDataSource
from wapsi.audit import ChainReport
from wapsi.models import ApprovalDecision, AuditEvent, CaseBundle

log = logging.getLogger("wapsi.snowflake.ui")
SOURCE_SNOWFLAKE = "snowflake"
SEED_NCRP_IDS = ("31009260000101", "31109260000102")


def call(session: Any, proc: str, *args: Any) -> dict[str, Any]:
    """CALL WAPSI.CASEMGMT.<proc>(?, ...) with binds; the VARIANT result as a dict."""
    marks = ", ".join("?" for _ in args)
    rows = session.sql(f"CALL WAPSI.CASEMGMT.{proc}({marks})", params=list(args)).collect()
    row = rows[0]
    raw = list(row.as_dict().values())[0] if hasattr(row, "as_dict") else row[0]
    data = json.loads(raw) if isinstance(raw, str) else raw
    return data if isinstance(data, dict) else {"ok": True, "result": data}


class SnowflakeDataSource(ShowcaseDataSource):
    def __init__(self, session: Any, *, seed: bool = True) -> None:
        self.session = session
        self.store = SnowflakeCaseStore(session)
        bundles = self._load_all()
        if not bundles and seed:
            for n in SEED_NCRP_IDS:
                try:
                    traced = call(session, "TRACE_CASE", n)
                    if traced.get("ok"):
                        call(session, "BUILD_HOLD_PLAN", traced["case_id"])
                except Exception:  # noqa: BLE001 - an empty inbox is better than a crash
                    log.exception("could not seed %s", n)
            bundles = self._load_all()
        super().__init__(bundles=bundles, ledger=None, source=SOURCE_SNOWFLAKE)

    def _load_all(self) -> list[CaseBundle]:
        out = []
        for cid in self.store.case_ids():
            b = self.store.get_bundle(cid)
            if b is not None:
                out.append(b)
        return out

    def refresh(self, case_id: str | None = None) -> None:
        ids = [case_id] if case_id else self.store.case_ids()
        for cid in ids:
            b = self.store.get_bundle(cid)
            if b is not None:
                self._bundles[cid] = b
        self._clock_cache = {k: v for k, v in self._clock_cache.items() if case_id is None or k[0] != case_id}

    def list_cases(self) -> list[Any]:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - keep showing what we have
            log.exception("refresh failed")
        return super().list_cases()

    def ledger(self) -> Any | None:
        return None  # the app owner role cannot read RAW (personal data); pages degrade gracefully

    def append_audit(self, case_id: str | None, event: AuditEvent) -> None:
        super().append_audit(case_id, event)
        if case_id and case_id in self._bundles:
            try:
                self.store.append_audit(case_id, event.model_copy(deep=True))
            except Exception:  # noqa: BLE001
                log.exception("could not append audit for %s", case_id)

    def verify_audit(self, case_id: str | None) -> ChainReport:
        """A case's stored audit chain (AUDIT_LOG read back, checked against AUDIT_ANCHOR); ``None``: the
        session's own events."""
        if case_id is None:
            return super().verify_audit(None)
        return self.store.verify_audit(case_id)

    def save_approval(self, case_id: str, *, draft_id: str | None, decision: ApprovalDecision, approver: str,
                      role: str, reason_code: str = "", comment: str = "") -> Outcome:
        if draft_id is None:  # plan-level sign-off: recorded in the session and the audit log
            out = super().save_approval(case_id, draft_id=None, decision=decision, approver=approver, role=role,
                                        reason_code=reason_code, comment=comment)
            if out.ok and out.bundle is not None and out.bundle.audit:
                self.append_audit(case_id, out.bundle.audit[-1])
            return out
        if role.lower() != "supervisor":
            return Outcome(False, "Only a supervisor can record an approval decision (maker-checker).")
        if not reason_code:
            return Outcome(False, "Pick a reason before recording the decision.")
        try:
            res = call(self.session, "APPROVE", draft_id, decision.value, reason_code, comment, approver)
        except Exception as exc:  # noqa: BLE001
            log.exception("APPROVE failed")
            return Outcome(False, f"The decision couldn't be recorded: {exc}")
        if not res.get("ok"):
            return Outcome(False, str(res.get("error", "not approved")))
        self.refresh(case_id)
        if res.get("disposition") == "EXCEPTIONAL_ACCOUNT_LEVEL_HOLD":
            return Outcome(True, "Exception approved and applied. The hold instruction and CFCFRMS update were "
                                 "redrafted: approve the new versions.", bundle=self._bundles.get(case_id))
        msg = {"APPROVED": "Approved. Actions staged; the lien register is updated.",
               "REJECTED": "Rejected. The decision is recorded in the audit trail.",
               "CHANGES_REQUESTED": "Sent back with a change request."}.get(decision.value, "Recorded.")
        return Outcome(True, msg, bundle=self._bundles.get(case_id))

    def run_pipeline(self, case_id: str, *, actor: str, role: str) -> Outcome:
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        try:
            res = call(self.session, "DRAFT", case_id, "ALL")
        except Exception as exc:  # noqa: BLE001
            log.exception("DRAFT failed for %s", case_id)
            return Outcome(False, f"Drafts couldn't be generated: {exc}")
        if not res.get("ok"):
            return Outcome(False, str(res.get("error", "drafting failed")))
        self.refresh(case_id)
        drafts = res.get("drafts", [])
        failed = sum(1 for d in drafts if not d.get("passed"))
        msg = f"Generated {len(drafts)} draft{'s' if len(drafts) != 1 else ''} with {res.get('llm', 'Cortex')}"
        msg += " — all passed their checks." if failed == 0 else f" — {failed} need attention."
        return Outcome(True, msg, bundle=self._bundles.get(case_id))

    def request_account_level(self, case_id: str, account_id: str, reasons: list[str], *, actor: str, role: str,
                              note: str = "") -> Outcome:
        """An officer (maker) requests an exceptional account-level hold through REQUEST_ACCOUNT_LEVEL_HOLD; a
        different supervisor decides on it in Approvals. Nothing changes in the plan until then."""
        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        if not reasons:
            return Outcome(False, "Record at least one reason from the list before sending the request.")
        try:
            res = call(self.session, "REQUEST_ACCOUNT_LEVEL_HOLD", case_id, account_id, json.dumps(list(reasons)),
                       note, actor)
        except Exception as exc:  # noqa: BLE001
            log.exception("REQUEST_ACCOUNT_LEVEL_HOLD failed")
            return Outcome(False, f"The request couldn't be recorded: {exc}")
        if not res.get("ok"):
            return Outcome(False, f"Can't request it: {res.get('error', 'not requested')}")
        self.refresh(case_id)
        bundle = self._bundles.get(case_id)
        draft = next((d for d in (bundle.drafts if bundle else []) if d.draft_id == res.get("draft_id")), None)
        return Outcome(True, "Request recorded. A supervisor other than you decides on it in Approvals.",
                       bundle=bundle, data=draft)

    def check_lea_request(self, case_id: str, request: Any, *, actor: str, role: str) -> Outcome:
        """Check a police / LEA hold request through CHECK_LEA (eight-field checklist, hold limited to the
        identifiable amount); recorded in the audit chain. The plan is never changed."""
        from wapsi.verify.lea_request import LeaRequestCheck

        if role.lower() == "auditor":
            return Outcome(False, "Auditors have read-only access.")
        payload = request.model_dump_json() if hasattr(request, "model_dump_json") else json.dumps(request)
        try:
            res = call(self.session, "CHECK_LEA", case_id, payload, actor)
        except Exception as exc:  # noqa: BLE001
            log.exception("CHECK_LEA failed")
            return Outcome(False, f"The request couldn't be checked: {exc}")
        if not res.get("ok"):
            return Outcome(False, f"The request couldn't be checked: {res.get('error', 'check failed')}")
        self.refresh(case_id)
        result = LeaRequestCheck.model_validate(res["check"])
        msg = ("Every checklist field is present." if result.complete
               else f"{len(result.missing)} of 8 checklist fields missing or unclear.")
        return Outcome(True, msg, bundle=self._bundles.get(case_id), data=result)
