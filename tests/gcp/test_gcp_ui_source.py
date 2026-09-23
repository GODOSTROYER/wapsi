from __future__ import annotations

from pathlib import Path

import pytest

from editions.gcp.app import tools
from editions.gcp.app.ui_source import SOURCE_CASE_STORE, CaseStoreDataSource
from ui.data_source import DataSource
from wapsi.fixtures import showcase as sc
from wapsi.models import ApprovalDecision, AuditEvent
from wapsi.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]


def test_seeds_demo_store_and_implements_protocol(demo_services):
    ds = CaseStoreDataSource(demo_services)
    assert isinstance(ds, DataSource)
    ids = {c.case_id for c in ds.list_cases()}
    assert {f"CASE-{sc.N1}", f"CASE-{sc.N2}"} <= ids
    assert {c.case_id for c in demo_services.store.list_cases()} == ids  # persisted
    assert ds.ledger() is demo_services.ledger


def test_reads_existing_cases_without_seeding(demo_services):
    tools.open_case([sc.N1])
    ds = CaseStoreDataSource(demo_services)
    assert ds.source == SOURCE_CASE_STORE
    assert [c.case_id for c in ds.list_cases()] == [f"CASE-{sc.N1}"]
    tools.open_case([sc.N2, sc.N3])  # created elsewhere (agent / Pub/Sub) after the UI started
    assert {c.case_id for c in ds.list_cases()} == {f"CASE-{sc.N1}", f"CASE-{sc.N2}"}


def test_writes_go_to_the_case_store(demo_services):
    ds = CaseStoreDataSource(demo_services)
    cid = f"CASE-{sc.N1}"
    event = AuditEvent(at=utcnow(), actor="asha", role="analyst", action="viewed", object_type="case",
                       object_id=cid)
    ds.append_audit(cid, event)
    assert demo_services.store.get_bundle(cid).audit[-1] == event

    refused = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.APPROVED, approver="asha",
                               role="analyst", reason_code="CHECKS_PASSED")
    assert not refused.ok

    out = ds.save_approval(cid, draft_id=None, decision=ApprovalDecision.CHANGES_REQUESTED, approver="ravi",
                           role="supervisor", reason_code="AMOUNT_QUERY")
    assert out.ok
    assert demo_services.store.get_bundle(cid).approvals[-1].reason_code == "AMOUNT_QUERY"

    assert not ds.run_pipeline(cid, actor="audrey", role="auditor").ok
    rerun = ds.run_pipeline(cid, actor="asha", role="analyst")
    assert rerun.ok and rerun.bundle is not None
    stored = demo_services.store.get_bundle(cid)
    assert stored.audit[-1].action == "pipeline_run"
    assert stored.approvals and stored.approvals[-1].reason_code == "AMOUNT_QUERY"  # history kept
    assert not ds.run_pipeline("CASE-00000000000000", actor="asha", role="analyst").ok


def test_streamlit_launcher_renders(demo_services):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "editions" / "gcp" / "ui_app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert isinstance(at.session_state["_wapsi_ds"], CaseStoreDataSource)
