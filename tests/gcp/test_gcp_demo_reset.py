"""The shared judging deployment: every judge starts on a fresh showcase case.

- ``seed_cases(reset=True)`` (the deploy's smoke step, ``POST /demo/seed?reset=1``, the sidebar's **Reset demo**)
  restores the snapshot taken at seeding time: no decisions, plan and drafts as computed; the audit log stays
  append-only (every earlier event, then ``demo_reset``) and verifies against its anchor, in memory and on
  Firestore;
- a new UI session resets an idle demo that someone worked on;
- the seeding lease: two revisions seeding at once (a redeploy) process each case exactly once; a held lease makes
  the other side wait or report ``busy``; an expired lease is taken over;
- ``/healthz`` says whether the image serves the baked docs index or the citations-only corpus.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from gcp_fakes import FakeFirestore

from editions.gcp.app import seed as seed_mod
from editions.gcp.app.config import GcpSettings
from editions.gcp.app.server import create_app
from editions.gcp.app.services import build_services, set_services
from wapsi.adapters.firestore_store import FirestoreCaseStore
from wapsi.adapters.memory import RecordingActionSink
from wapsi.audit import verify_chain
from wapsi.models import ApprovalDecision
from wapsi.pipeline.run import approve

CASE1, CASE2 = seed_mod.SHOWCASE_CASE_IDS


def _approve_one(services, case_id: str, decision=ApprovalDecision.APPROVED) -> str:
    """A supervisor's decision on the first verified draft, stored the way the UI stores it."""
    b = services.store.get_bundle(case_id)
    draft = next(d for d in b.drafts if b.verifications[d.draft_id].passed)
    updated = approve(b, draft.draft_id, approver="ravi", role="supervisor", decision=decision,
                      reason_code="CHECKS_PASSED", ledger=services.ledger, sink=RecordingActionSink())
    services.store.put_bundle(updated)
    return draft.draft_id


def _firestore(services) -> FakeFirestore:
    fs = FakeFirestore()
    services.store = FirestoreCaseStore(client=fs)
    return fs


# --------------------------------------------------------------------------- reset


@pytest.mark.parametrize("backend", ["memory", "firestore"])
def test_reset_restores_the_freshly_seeded_case_and_keeps_the_audit_history(demo_services, backend):
    fs = _firestore(demo_services) if backend == "firestore" else None
    assert seed_mod.seed_cases(demo_services) == {CASE1: "seeded", CASE2: "seeded"}
    seeded = demo_services.store.get_bundle(CASE1)
    draft_id = _approve_one(demo_services, CASE1, ApprovalDecision.REJECTED)  # rejected drafts stay rejected...
    worked = demo_services.store.get_bundle(CASE1)
    assert worked.approvals and not seed_mod.is_fresh(worked, seed_mod._snapshot(demo_services, CASE1))

    out = seed_mod.seed_cases(demo_services, reset=True, actor="judge-2", role="analyst")
    assert out == {CASE1: "reset", CASE2: "fresh"}  # nobody touched CASE2: no reset event on it
    after = demo_services.store.get_bundle(CASE1)
    assert after.approvals == [] and after.case.status == seeded.case.status
    assert [d.draft_id for d in after.drafts] == [d.draft_id for d in seeded.drafts]
    assert after.plan.total_hold == seeded.plan.total_hold and after.case.opened_at == seeded.case.opened_at
    # append-only: the earlier log (with the decision) is a prefix, then the reset
    assert [e.hash for e in after.audit[:len(worked.audit)]] == [e.hash for e in worked.audit]
    assert "draft_rejected" in [e.action for e in after.audit]
    last = after.audit[-1]
    assert last.action == seed_mod.RESET_ACTION and last.actor == "judge-2"
    assert last.details["approvals_cleared"] == 1
    assert verify_chain(after.audit, case_id=CASE1).ok and demo_services.store.verify_audit(CASE1).ok
    assert seed_mod.is_fresh(after, seed_mod._snapshot(demo_services, CASE1))
    assert seed_mod.RESET_ACTION not in [e.action for e in demo_services.store.get_bundle(CASE2).audit]
    assert seed_mod.seed_cases(demo_services, reset=True) == {CASE1: "fresh", CASE2: "fresh"}  # idempotent
    # ...until the demo is reset: the next judge can decide afresh
    assert _approve_one(demo_services, CASE1) == draft_id
    if fs is not None:
        assert any(p.startswith(f"{seed_mod.SNAPSHOT_COLLECTION}/{CASE1}") for p in fs.docs)
        assert f"{seed_mod.LOCK_COLLECTION}/{seed_mod.LOCK_ID}" not in fs.docs  # the lease was released
        assert {c.case_id for c in demo_services.store.list_cases()} == {CASE1, CASE2}  # snapshots stay hidden


def test_reset_without_a_snapshot_processes_the_case_again(demo_services):
    seed_mod.seed_cases(demo_services)
    _approve_one(demo_services, CASE2)
    demo_services._demo_snapshots = None  # e.g. seeded by an older revision that kept no snapshot
    before = demo_services.store.get_bundle(CASE2)
    assert seed_mod.seed_cases(demo_services, reset=True)[CASE2] == "reset"
    after = demo_services.store.get_bundle(CASE2)
    assert after.approvals == [] and after.audit[-1].details["restored"] == "freshly processed"
    assert len(after.audit) > len(before.audit) + 1  # the new run's events, then the reset
    assert demo_services.store.verify_audit(CASE2).ok
    assert seed_mod._snapshot(demo_services, CASE2) is not None  # kept for the next reset


def test_reset_of_an_empty_store_seeds(demo_services):
    assert seed_mod.seed_cases(demo_services, reset=True) == {CASE1: "seeded", CASE2: "seeded"}


# --------------------------------------------------------------------------- idle reset


def test_a_new_session_resets_an_idle_demo_someone_worked_on(demo_services):
    seed_mod.seed_cases(demo_services)
    assert seed_mod.reset_if_idle(demo_services, 60) is None  # fresh cases: nothing to reset
    _approve_one(demo_services, CASE1)
    latest = seed_mod.last_activity(demo_services.store.get_bundle(CASE1))
    assert seed_mod.reset_if_idle(demo_services, 1800, now=latest + timedelta(minutes=5)) is None  # in use
    assert seed_mod.reset_if_idle(demo_services, 0, now=latest + timedelta(days=1)) is None  # turned off
    out = seed_mod.reset_if_idle(demo_services, 1800, now=latest + timedelta(minutes=31))
    assert out == {CASE1: "reset", CASE2: "fresh"}
    after = demo_services.store.get_bundle(CASE1)
    assert after.approvals == [] and "idle for 30+ minutes" in after.audit[-1].details["reason"]


def test_idle_reset_minutes_come_from_the_environment(monkeypatch):
    monkeypatch.delenv(seed_mod.IDLE_RESET_ENV, raising=False)
    assert seed_mod.idle_reset_seconds() == 30 * 60
    monkeypatch.setenv(seed_mod.IDLE_RESET_ENV, "0")
    assert seed_mod.idle_reset_seconds() == 0
    monkeypatch.setenv(seed_mod.IDLE_RESET_ENV, "soon")
    assert seed_mod.idle_reset_seconds() == 30 * 60


# --------------------------------------------------------------------------- the seeding lease


def _slow_pipeline(monkeypatch, seconds: float = 0.3) -> None:
    """Widen the window between "not in the store" and "stored", where two unlocked seeders would both write."""
    import wapsi.pipeline.run as run

    real = run.process_case

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(run, "process_case", slow)


def test_two_revisions_seeding_at_once_process_each_case_exactly_once(monkeypatch):
    """A redeploy: the old and the new revision start seeding the same Firestore at the same moment."""
    _slow_pipeline(monkeypatch)
    fs = FakeFirestore()
    revisions = []
    for _ in range(2):
        svc = build_services(GcpSettings(demo_mode=True))
        svc.store = FirestoreCaseStore(client=fs)
        revisions.append(svc)
    results: list[dict[str, str]] = []
    threads = [threading.Thread(target=lambda s=s: results.append(seed_mod.seed_cases(s))) for s in revisions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 2
    for case_id in (CASE1, CASE2):
        assert sorted(r[case_id] for r in results) == ["exists", "seeded"], results
        audit = revisions[0].store.get_bundle(case_id).audit
        assert [e.action for e in audit].count("case_opened") == 1  # one run, not two runs' events
        assert revisions[0].store.verify_audit(case_id).ok
    assert f"{seed_mod.LOCK_COLLECTION}/{seed_mod.LOCK_ID}" not in fs.docs


def test_a_held_lease_makes_others_wait_and_an_expired_one_is_taken_over(demo_services):
    fs = _firestore(demo_services)
    other = seed_mod.SeedLease(demo_services, holder="old-revision", ttl_s=60)
    assert other.acquire(0)
    assert fs.docs[f"{seed_mod.LOCK_COLLECTION}/{seed_mod.LOCK_ID}"]["holder"] == "old-revision"
    assert seed_mod.seed_cases(demo_services, wait_s=0.3) == {CASE1: "busy", CASE2: "busy"}
    assert demo_services.store.list_cases() == []
    other.release()
    assert seed_mod.seed_cases(demo_services, wait_s=0.3) == {CASE1: "seeded", CASE2: "seeded"}

    _approve_one(demo_services, CASE2)
    dead = seed_mod.SeedLease(demo_services, holder="stopped-instance", ttl_s=0.01)  # died without releasing
    assert dead.acquire(0)
    time.sleep(0.05)
    assert seed_mod.seed_cases(demo_services, reset=True, wait_s=0.3) == {CASE1: "fresh", CASE2: "reset"}


def test_the_lease_fails_open_when_firestore_is_unreachable(demo_services, monkeypatch):
    _firestore(demo_services)

    def down(self):
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(seed_mod.SeedLease, "_take", down)
    assert seed_mod.seed_cases(demo_services) == {CASE1: "seeded", CASE2: "seeded"}


def test_in_process_seeders_share_one_lock(demo_services):
    lease = seed_mod.SeedLease(demo_services)
    assert lease.acquire(0)
    try:
        assert set(seed_mod.seed_cases(demo_services, wait_s=0.1).values()) == {"busy"}
    finally:
        lease.release()
    assert set(seed_mod.seed_cases(demo_services, wait_s=0.1).values()) == {"seeded"}


# --------------------------------------------------------------------------- routes


def _client(monkeypatch, services):
    monkeypatch.setenv(seed_mod.SEED_ENV, "showcase")
    return TestClient(create_app(GcpSettings(serve_adk_web=False, demo_mode=True), services=services))


def test_smoke_seed_with_reset_gives_the_next_judge_a_fresh_case(monkeypatch):
    services = build_services(GcpSettings(demo_mode=True))
    try:
        with _client(monkeypatch, services) as c:
            assert c.post("/demo/seed?wait_s=120").json()["seed"]["state"] == "done"
            _approve_one(services, CASE1)
            assert services.store.get_bundle(CASE1).approvals
            r = c.post("/demo/seed?wait_s=120&redo_fallback=1&reset=1").json()
            assert r["seed"]["results"] == {CASE1: "reset", CASE2: "fresh"}, r
            assert services.store.get_bundle(CASE1).approvals == []
            assert c.get(f"/demo/cases/{CASE1}").json()["drafts_verified"] > 0
    finally:
        set_services(None)


def test_a_reset_request_waits_for_the_startup_seeding_then_resets(monkeypatch):
    services = build_services(GcpSettings(demo_mode=True))
    seed_mod.seed_cases(services)
    _approve_one(services, CASE1)
    _slow_pipeline(monkeypatch, 0.0)
    gate = threading.Event()
    real = seed_mod.seed_cases

    def slow_first(*args, **kwargs):  # the start-up run (no reset) is still going when the smoke step arrives
        if not kwargs.get("reset"):
            gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(seed_mod, "seed_cases", slow_first)
    try:
        with _client(monkeypatch, services) as c:
            threading.Timer(0.3, gate.set).start()
            r = c.post("/demo/seed?wait_s=60&reset=1").json()
            assert r["seed"]["results"] == {CASE1: "reset", CASE2: "fresh"}, r
            assert services.store.get_bundle(CASE1).approvals == []
    finally:
        set_services(None)


def test_healthz_reports_whether_the_docs_index_was_baked(monkeypatch):
    from wapsi.search.index import LocalDocSearch, citation_chunks, load_citations

    services = build_services(GcpSettings(demo_mode=True))
    try:
        with _client(monkeypatch, services) as c:
            services.search = LocalDocSearch(citation_chunks(load_citations()), load_citations(), fallback=True)
            h = c.get("/healthz").json()
            assert h["docs_index"] is True and h["docs_fallback"] is True and h["docs_chunks"] > 0
            services.search = LocalDocSearch(citation_chunks(load_citations()), load_citations())  # a baked index
            h = c.get("/healthz").json()
            assert h["docs_fallback"] is False and h["docs_chunks"] == len(services.search)
            services.search = None
            h = c.get("/healthz").json()
            assert h["docs_index"] is False and h["docs_fallback"] is True and h["docs_chunks"] == 0
    finally:
        set_services(None)


# --------------------------------------------------------------------------- the UI


def test_ui_data_source_resets_the_shared_demo(demo_services, monkeypatch):
    from editions.gcp.app.ui_source import CaseStoreDataSource

    monkeypatch.setenv(seed_mod.SEED_ENV, "showcase")
    ds = CaseStoreDataSource(demo_services)  # demo mode: seeds the showcase cases into the store
    assert ds.demo_reset_enabled and ds.demo_decisions() == 0 and ds.demo_notice is None
    assert ds.save_approval(CASE1, draft_id=None, decision=ApprovalDecision.CHANGES_REQUESTED, approver="ravi",
                            role="supervisor", reason_code="AMOUNT_QUERY").ok
    assert ds.demo_decisions() == 1 and demo_services.store.get_bundle(CASE1).approvals

    out = ds.reset_demo(actor="judge-2", role="analyst")
    assert out.ok and "freshly seeded" in out.message, out.message
    assert ds.demo_decisions() == 0 and ds.get_bundle(CASE1).approvals == []
    stored = demo_services.store.get_bundle(CASE1)
    assert stored.approvals == [] and stored.audit[-1].action == seed_mod.RESET_ACTION
    assert "plan_changes_requested" in [e.action for e in stored.audit]  # the decision stays on record
    assert ds.verify_audit(CASE1).ok
    again = ds.reset_demo(actor="judge-2", role="analyst")
    assert again.ok and "already fresh" in again.message


def test_ui_reset_is_offered_only_on_the_shared_demo(demo_services, monkeypatch):
    import dataclasses

    from editions.gcp.app.ui_source import CaseStoreDataSource

    monkeypatch.delenv(seed_mod.SEED_ENV, raising=False)
    private = dataclasses.replace(demo_services, settings=GcpSettings(demo_mode=False, project="p",
                                                                      otel_to_cloud=False))
    ds = CaseStoreDataSource(private)
    assert not ds.demo_reset_enabled
    assert not ds.reset_demo(actor="x", role="supervisor").ok


def test_ui_new_session_on_an_idle_worked_demo_starts_fresh(demo_services, monkeypatch):
    from editions.gcp.app.ui_source import CaseStoreDataSource

    monkeypatch.setenv(seed_mod.SEED_ENV, "showcase")
    seed_mod.seed_cases(demo_services)
    _approve_one(demo_services, CASE1)
    monkeypatch.setenv(seed_mod.IDLE_RESET_ENV, "30")
    assert CaseStoreDataSource(demo_services).demo_notice is None  # worked on a moment ago: left alone
    assert demo_services.store.get_bundle(CASE1).approvals
    monkeypatch.setenv(seed_mod.IDLE_RESET_ENV, "0.0001")  # 6 ms: the earlier visit is now "idle"
    time.sleep(0.05)
    ds = CaseStoreDataSource(demo_services)
    assert ds.demo_notice and "reset for you" in ds.demo_notice
    assert ds.get_bundle(CASE1).approvals == [] and ds.demo_decisions() == 0


def test_sidebar_shows_the_shared_demo_and_resets_it(demo_services, monkeypatch):
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from editions.gcp.app.ui_source import CaseStoreDataSource

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv(seed_mod.SEED_ENV, "showcase")
    ds = CaseStoreDataSource(demo_services)
    assert ds.save_approval(CASE1, draft_id=None, decision=ApprovalDecision.CHANGES_REQUESTED, approver="ravi",
                            role="supervisor", reason_code="AMOUNT_QUERY").ok
    ds.demo_notice = "The shared demo had been idle, so the showcase cases were reset for you."
    at = AppTest.from_file(str(root / "editions" / "gcp" / "ui_app.py"), default_timeout=120)
    at.session_state["_wapsi_ds"] = ds
    at.session_state["case_id"] = CASE1
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    side = "\n".join(m.value for m in at.sidebar.markdown)
    assert "Shared demo." in side and "1 decision recorded on the showcase cases so far." in side
    assert any("reset for you" in i.value for i in at.info)  # the idle-reset notice, once
    at.button(key="demo_reset").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Demo reset" in s.value for s in at.success), [s.value for s in at.success]
    side = "\n".join(m.value for m in at.sidebar.markdown)
    assert "No decisions recorded on the showcase cases yet." in side
    assert not any("reset for you" in i.value for i in at.info)
    assert demo_services.store.get_bundle(CASE1).approvals == []
