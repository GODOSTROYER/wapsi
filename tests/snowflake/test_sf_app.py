"""Streamlit in Snowflake app: the data source seeds, reads, drafts and approves through the procedures."""

from __future__ import annotations

import ast
from pathlib import Path

from wapsi.models import ApprovalDecision, AuditEvent
from wapsi.timeutil import utcnow

APP = Path(__file__).resolve().parents[2] / "editions" / "snowflake" / "app"


def _source(sf_session):
    from sf_data_source import SnowflakeDataSource

    return SnowflakeDataSource(sf_session)


def test_empty_tables_are_seeded_with_the_showcase_cases(sf_session):
    ds = _source(sf_session)
    ids = sorted(c.case_id for c in ds.list_cases())
    assert ids == ["CASE-31009260000101", "CASE-31109260000102"]
    b = ds.get_bundle("CASE-31009260000101")
    assert b.plan is not None and b.plan.total_hold == 142848.0
    assert ds.ledger() is None  # the app owner never reads RAW


def test_generate_drafts_and_approve_through_the_procedures(sf_session):
    ds = _source(sf_session)
    out = ds.run_pipeline("CASE-31009260000101", actor="nodal.officer@hb01", role="analyst")
    assert out.ok and "all passed" in out.message
    b = ds.get_bundle("CASE-31009260000101")
    hold = next(d for d in b.drafts if d.kind.value == "HOLD_INSTRUCTION" and d.account_id == "HB01-1000000001")

    sf_session.sql("INSERT INTO APP.USER_ROLES (user_name, app_role) VALUES ('supervisor@hb01', 'SUPERVISOR')")
    sf_session.set_roles(["WAPSI_APP_OWNER"])
    refused = ds.save_approval("CASE-31009260000101", draft_id=hold.draft_id, decision=ApprovalDecision.APPROVED,
                               approver="nodal.officer@hb01", role="analyst", reason_code="SOP_COMPLIANT")
    assert not refused.ok
    ok = ds.save_approval("CASE-31009260000101", draft_id=hold.draft_id, decision=ApprovalDecision.APPROVED,
                          approver="supervisor@hb01", role="supervisor", reason_code="SOP_COMPLIANT", comment="ok")
    assert ok.ok, ok.message
    assert ok.bundle.approvals[-1].approver == "supervisor@hb01"
    liens = sf_session.rows("SELECT SUM(amount) AS total FROM CASEMGMT.LIENS")[0]["total"]
    assert float(liens) == 43200.0
    assert ds.run_pipeline("CASE-31009260000101", actor="auditor@hb01", role="auditor").ok is False


def test_case_audit_events_reach_the_audit_log(sf_session):
    ds = _source(sf_session)
    ev = AuditEvent(at=utcnow(), actor="auditor@hb01", role="auditor", action="viewed", object_type="page",
                    object_id="Hold Plan")
    ds.append_audit("CASE-31009260000101", ev)
    rows = sf_session.rows("SELECT actor FROM CASEMGMT.AUDIT_LOG WHERE action = 'viewed'")
    assert [r["actor"] for r in rows] == ["auditor@hb01"]


def test_streamlit_entry_point_wires_the_shared_ui():
    src = (APP / "streamlit_app.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert 'runpy.run_module("ui.app"' in src and "wapsi_app.zip" in src and "get_active_session" in src
    assert '"_wapsi_ds"' in src


def test_app_requests_approves_an_exception_and_checks_lea_through_the_procedures(sf_session):
    from wapsi.verify.lea_request import LeaHoldRequest, LeaRequestCheck

    ds = _source(sf_session)
    case = "CASE-31009260000101"
    sf_session.sql("INSERT INTO APP.USER_ROLES (user_name, app_role) VALUES ('supervisor@hb01', 'SUPERVISOR'), "
                   "('nodal.officer@hb01', 'ANALYST')")
    sf_session.set_roles(["WAPSI_APP_OWNER"])
    out = ds.request_account_level(case, "HB01-1000000001", ["FRAUDSTER_CONTROLLED_ACCOUNT"],
                                   actor="nodal.officer@hb01", role="analyst", note="LEA order")
    assert out.ok, out.message
    assert out.data.kind.value == "ACCOUNT_LEVEL_HOLD_REQUEST" and out.data.structured["requested_by"] == \
        "nodal.officer@hb01"
    assert not ds.request_account_level(case, "HB01-1000000001", [], actor="nodal.officer@hb01", role="analyst").ok
    ok = ds.save_approval(case, draft_id=out.data.draft_id, decision=ApprovalDecision.APPROVED,
                          approver="supervisor@hb01", role="supervisor", reason_code="SOP_COMPLIANT")
    assert ok.ok and "redrafted" in ok.message, ok.message
    dec = next(d for d in ok.bundle.plan.decisions if d.account_id == "HB01-1000000001")
    assert dec.disposition.value == "EXCEPTIONAL_ACCOUNT_LEVEL_HOLD" and dec.hold_amount == 43200.0

    lea = ds.check_lea_request(case, LeaHoldRequest(request_id="LEA-1", officer_name="R. Sharma",
                                                    account_id="HB01-1000000001", amount=10000.0),
                               actor="nodal.officer@hb01", role="analyst")
    assert lea.ok and isinstance(lea.data, LeaRequestCheck) and not lea.data.complete
    assert "missing" in lea.message
    report = ds.verify_audit(case)
    assert report.ok and report.anchored and report.length == len(ds.get_bundle(case).audit)
