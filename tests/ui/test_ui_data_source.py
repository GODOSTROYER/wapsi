from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import ui.data_source as data_source
from ui.data_source import (
    DataSource,
    FixtureDataSource,
    clock_status,
    engine_status,
    latest_approval,
    load_eval_report,
    now_for_ui,
    plan_reports,
)
from wapsi.models import ApprovalDecision, AuditEvent, Clock, ClockKind, ClockStatus
from wapsi.timeutil import IST, utcnow


@pytest.fixture
def ds(bundles):
    return FixtureDataSource(bundles=[b.model_copy(deep=True) for b in bundles])


@pytest.fixture
def no_engine(monkeypatch):
    """Simulate phase 1: engine modules not installed."""

    def _missing(module: str, attr: str):
        raise ImportError(f"No module named {module!r}")

    monkeypatch.setattr(data_source, "_optional", _missing)


def test_protocol_and_listing(ds):
    assert isinstance(ds, DataSource)
    cases = ds.list_cases()
    assert len(cases) == 2
    for c in cases:
        b = ds.get_bundle(c.case_id)
        assert b is not None and b.plan is not None and b.trace is not None
    assert ds.get_bundle("nope") is None
    assert ds.ledger() is not None


def test_default_constructor_uses_showcase_bundles():
    assert {c.case_id for c in FixtureDataSource().list_cases()} == {"CASE-31009260000101",
                                                                     "CASE-31109260000102"}


def test_only_supervisors_record_decisions(ds):
    cid = ds.list_cases()[0].case_id
    out = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.APPROVED, approver="a", role="analyst",
                           reason_code="CHECKS_PASSED")
    assert not out.ok and "supervisor" in out.message
    out = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.APPROVED, approver="x", role="auditor",
                           reason_code="CHECKS_PASSED")
    assert not out.ok
    assert ds.get_bundle(cid).approvals == []


def test_reason_code_required_and_unknown_case(ds):
    cid = ds.list_cases()[0].case_id
    assert not ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.APPROVED, approver="s",
                                role="supervisor").ok
    assert not ds.save_approval("CASE-X", draft_id=None, decision=ApprovalDecision.APPROVED, approver="s",
                                role="supervisor", reason_code="CHECKS_PASSED").ok


def test_plan_approval_writes_approval_and_audit(ds):
    cid = ds.list_cases()[1].case_id
    out = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.CHANGES_REQUESTED,
                           approver="supervisor@hb01", role="Supervisor", reason_code="AMOUNT_QUERY",
                           comment="check M")
    assert out.ok
    b = ds.get_bundle(cid)
    appr = latest_approval(b, None)
    assert appr.decision == ApprovalDecision.CHANGES_REQUESTED and appr.role == "supervisor"
    ev = b.audit[-1]
    assert (ev.action, ev.actor, ev.role, ev.object_type) == ("plan_changes_requested", "supervisor@hb01",
                                                              "supervisor", "plan")
    assert ev.details["reason_code"] == "AMOUNT_QUERY"
    assert ds.audit_events()[0][1] == ev


def test_unknown_draft_is_rejected(ds, no_engine):
    cid = ds.list_cases()[0].case_id
    out = ds.save_approval(cid, draft_id="DRF-missing", decision=ApprovalDecision.APPROVED, approver="s",
                           role="supervisor", reason_code="CHECKS_PASSED")
    assert not out.ok


def test_audit_events_global_and_per_case(ds):
    cid = ds.list_cases()[0].case_id
    ds.append_audit(None, AuditEvent(at=utcnow(), actor="a", role="analyst", action="role_switched",
                                     object_type="session", object_id="role"))
    ds.append_audit(cid, AuditEvent(at=utcnow() + timedelta(seconds=1), actor="a", role="analyst",
                                    action="viewed", object_type="page", object_id="Case Room"))
    events = ds.audit_events()
    assert events[0][0] == cid and events[0][1].action == "viewed"
    assert events[1][0] is None and events[1][1].action == "role_switched"


def test_pipeline_unavailable_is_friendly(ds, no_engine):
    cid = ds.list_cases()[0].case_id
    out = ds.run_pipeline(cid, actor="a", role="analyst")
    assert not out.ok and out.unavailable
    assert "isn't switched on" in out.message
    assert ds.get_bundle(cid).drafts == []
    assert ds.get_bundle(cid).audit[-1].action == "pipeline_unavailable"
    assert ds.clocks(ds.get_bundle(cid), utcnow()) == []


def test_pipeline_refuses_auditors_and_unknown_cases(ds):
    assert not ds.run_pipeline(ds.list_cases()[0].case_id, actor="x", role="auditor").ok
    assert not ds.run_pipeline("CASE-X", actor="x", role="analyst").ok


def test_pipeline_errors_are_reported(ds, monkeypatch):
    def fake_optional(module, attr):
        if attr == "process_case":
            def boom(*a, **k):
                raise RuntimeError("ledger offline")
            return boom
        if attr == "FakeLLM":
            return lambda: object()
        raise ImportError(module)

    monkeypatch.setattr(data_source, "_optional", fake_optional)
    cid = ds.list_cases()[0].case_id
    out = ds.run_pipeline(cid, actor="a", role="analyst")
    assert not out.ok and not out.unavailable and "ledger offline" in out.message
    assert ds.get_bundle(cid).audit[-1].action == "pipeline_failed"


def test_pipeline_keeps_history_when_engine_present(ds):
    if not engine_status().pipeline_ready:
        pytest.skip("engine modules not installed yet")
    cid = ds.list_cases()[0].case_id
    ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.APPROVED, approver="s", role="supervisor",
                     reason_code="CHECKS_PASSED")
    opened = ds.get_bundle(cid).case.opened_at
    out = ds.run_pipeline(cid, actor="nodal.officer@hb01", role="analyst")
    if not out.ok:
        pytest.skip(f"engine present but not runnable yet: {out.message}")
    b = ds.get_bundle(cid)
    assert b.drafts, "pipeline produced no drafts"
    assert b.case.opened_at == opened
    assert latest_approval(b, None) is not None  # earlier plan decision survives
    assert b.audit[-1].action == "pipeline_run"
    assert all(r.passed in (True, False) for r in plan_reports(b))
    draft = next(d for d in b.drafts if b.verifications.get(d.draft_id) and b.verifications[d.draft_id].passed)
    res = ds.save_approval(cid, draft_id=draft.draft_id, decision=ApprovalDecision.APPROVED,
                           approver="supervisor@hb01", role="supervisor", reason_code="CHECKS_PASSED")
    assert res.ok, res.message
    assert latest_approval(ds.get_bundle(cid), draft.draft_id).decision == ApprovalDecision.APPROVED


def test_clock_status_derivation():
    start = datetime(2026, 9, 10, tzinfo=IST)
    clock = Clock(clock_id="c", case_id="C", kind=ClockKind.HOLD_LAPSE, start=start, due=start + timedelta(days=90),
                  warn_at=start + timedelta(days=75))
    assert clock_status(clock, start + timedelta(days=1)) == ClockStatus.OPEN
    assert clock_status(clock, start + timedelta(days=80)) == ClockStatus.WARNING
    assert clock_status(clock, start + timedelta(days=91)) == ClockStatus.OVERDUE
    done = clock.model_copy(update={"status": ClockStatus.DONE})
    assert clock_status(done, start + timedelta(days=91)) == ClockStatus.DONE


def test_load_eval_report(workdir, sample_report, monkeypatch):
    monkeypatch.delenv("WAPSI_EVAL_REPORT", raising=False)
    assert load_eval_report(workdir / "missing.json") == (None, None)
    bad = workdir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    report, err = load_eval_report(bad)
    assert report is None and "bad.json" in err
    arr = workdir / "arr.json"
    arr.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert load_eval_report(arr)[1]
    report, err = load_eval_report(sample_report)
    assert err is None and report["world"] == "demo"
    monkeypatch.setenv("WAPSI_EVAL_REPORT", str(sample_report))
    assert load_eval_report()[0]["cases"] == 40


def test_now_for_ui(monkeypatch):
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    assert now_for_ui() == datetime(2026, 9, 16, 11, 0, tzinfo=IST)
    monkeypatch.setenv("WAPSI_NOW", "garbage")
    assert abs((now_for_ui() - utcnow()).total_seconds()) < 5


def test_engine_status_shape():
    status = engine_status()
    assert set(status.modules) == set(data_source.ENGINE_MODULES)
    assert set(status.available) | set(status.missing) == set(status.modules)
