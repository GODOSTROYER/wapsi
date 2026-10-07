"""Audit view: chain status badges and the "Verify chain" button; approvals show the case's chain badge."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.data_source import FixtureDataSource
from ui.views.audit import chain_chip
from wapsi.audit.chain import GENESIS_HASH, ChainReport, append_event, verify_chain
from wapsi.models import ApprovalDecision, AuditEvent
from wapsi.timeutil import utcnow

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASE = "CASE-31009260000101"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_UI_SOURCE", "fixtures")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    monkeypatch.delenv("WAPSI_EVAL_REPORT", raising=False)


def _app(page: str, role: str = "auditor") -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["case_id"] = CASE
    at.session_state["role"] = role
    at.session_state["page"] = page
    return at.run()


def _text(at: AppTest) -> str:
    return "\n".join(html.unescape(m.value) for m in at.markdown)


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _go(at: AppTest, page: str) -> AppTest:
    return at.radio(key="page").set_value(page).run()


def test_chain_chip_labels_and_tones():
    intact = ChainReport(ok=True, length=3, head=GENESIS_HASH)
    broken = ChainReport(ok=False, first_bad_seq=2, reason="event #2 was changed", code="CONTENT", length=5)
    legacy = ChainReport(ok=False, first_bad_seq=0, reason="no seals", code="UNCHAINED", length=4)
    assert "chain intact · 3 events" in chain_chip("CASE-1", intact) and "--wp-good-bg" in chain_chip("C", intact)
    assert "chain broken at event #2" in chain_chip("CASE-1", broken) and "--wp-bad-bg" in chain_chip("C", broken)
    assert "not chained" in chain_chip("CASE-1", legacy) and "--wp-gray-bg" in chain_chip("C", legacy)
    assert "event #2 was changed" in chain_chip("CASE-1", broken)  # full finding in the tooltip


def test_audit_view_shows_intact_chains_and_verify_records_the_check():
    at = _app("Case Room")  # a case page: the view is recorded in the case's chain
    _go(at, "Audit")
    _ok(at)
    text = _text(at)
    assert f"{CASE} · chain intact" in text and "Session · chain intact" in text
    at.button(key="audit_verify_chain").click().run()
    _ok(at)
    assert any("every hash and link checks out" in s.value for s in at.success)
    assert not at.error
    assert "Audit chain verified" in _text(at)  # the check itself is recorded (in the session's chain)
    ds = at.session_state["_wapsi_ds"]
    assert ds.verify_audit(None).ok and ds.verify_audit(CASE).ok


def test_audit_view_flags_a_tampered_event():
    at = _app("Case Room")
    ds = at.session_state["_wapsi_ds"]
    ds.get_bundle(CASE).audit[0].actor = "someone.else"  # edit a recorded event behind the app's back
    _go(at, "Audit")
    _ok(at)
    assert f"{CASE} · chain broken at event #0" in _text(at)
    at.button(key="audit_verify_chain").click().run()
    assert any("Chain broken at event #0" in e.value for e in at.error)
    assert not at.success


def test_approvals_show_the_case_chain_badge():
    at = _app("Approvals", role="supervisor")
    _ok(at)
    assert "Audit log · chain intact" in _text(at)


def test_data_source_seals_decisions_with_the_plan_fingerprint(bundles):
    ds = FixtureDataSource(bundles=[b.model_copy(deep=True) for b in bundles])
    cid = ds.list_cases()[1].case_id
    ds.append_audit(cid, AuditEvent(at=utcnow(), actor="asha", role="analyst", action="viewed",
                                    object_type="page", object_id="Hold Plan"))
    out = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.CHANGES_REQUESTED,
                           approver="supervisor@hb01", role="supervisor", reason_code="AMOUNT_QUERY")
    assert out.ok
    b = ds.get_bundle(cid)
    ev = b.audit[-1]
    assert ev.action == "plan_changes_requested" and len(ev.details["plan_sha256"]) == 64
    assert ev.details["approval_id"] == b.approvals[-1].approval_id
    assert ds.verify_audit(cid).ok and verify_chain(b.audit, case_id=cid).ok


def test_data_source_keeps_history_first_on_a_pipeline_rerun(bundles):
    ds = FixtureDataSource(bundles=[b.model_copy(deep=True) for b in bundles])
    cid = ds.list_cases()[0].case_id
    ds.append_audit(cid, AuditEvent(at=utcnow(), actor="asha", role="analyst", action="viewed",
                                    object_type="page", object_id="Case Room"))
    first = ds.get_bundle(cid).audit[0].hash
    out = ds.run_pipeline(cid, actor="asha", role="analyst")
    if not out.ok:
        pytest.skip(f"engine not runnable: {out.message}")
    log = ds.get_bundle(cid).audit
    assert log[0].hash == first and log[-1].action == "pipeline_run"
    assert ds.verify_audit(cid).ok


def test_session_events_form_their_own_chain(bundles):
    ds = FixtureDataSource(bundles=[b.model_copy(deep=True) for b in bundles])
    for i in range(3):
        ds.append_audit(None, AuditEvent(at=utcnow(), actor="asha", role="analyst", action="role_switched",
                                         object_type="session", object_id="role", details={"n": i}))
    assert ds.verify_audit(None).ok and ds.verify_audit(None).length == 3
    append_event(ds._global_audit, AuditEvent(at=utcnow(), actor="x", role="analyst", action="viewed",
                                              object_type="page", object_id="KPIs"), case_id=None)
    ds._global_audit[1].details["n"] = 99
    assert not ds.verify_audit(None).ok


def test_a_log_from_a_store_that_does_not_seal_is_shown_as_not_chained():
    at = _app("Case Room")
    ds = at.session_state["_wapsi_ds"]
    bundle = ds.get_bundle(CASE)
    bundle.audit = [e.model_copy(update={"seq": None, "case_id": None, "prev_hash": None, "hash": None})
                    for e in bundle.audit]  # e.g. read back from a store that keeps no seals
    _go(at, "Audit")
    _ok(at)
    assert f"{CASE} · not chained" in _text(at)
    at.button(key="audit_verify_chain").click().run()
    assert not at.error
    assert any("Not chained yet" in i.value and CASE in i.value for i in at.info)
