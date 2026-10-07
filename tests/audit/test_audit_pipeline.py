"""The pipeline seals every write that matters into the case's hash chain, and the in-memory / file stores keep
the log append-only. Showcase numbers (AGENTS.md): holds total ₹1,42,848, recoverable ₹3,34,848."""

from __future__ import annotations

import json

import pytest

from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.memory import InMemoryCaseStore, RecordingActionSink
from wapsi.audit.chain import CONTENT, HEAD, TRUNCATED, link_hash, verify_chain
from wapsi.fixtures import showcase as sc
from wapsi.models import AccountLevelReason, ApprovalDecision, AuditEvent, DraftKind
from wapsi.money import eq
from wapsi.pipeline.run import approve, draft_fingerprint, process_case, request_account_level_hold
from wapsi.timeutil import utcnow

CASE1 = f"CASE-{sc.N1}"


@pytest.fixture(scope="module")
def processed(ledger):
    return process_case(ledger, [sc.N1], llm=FakeLLM())


@pytest.fixture
def bundle(processed):
    return processed.model_copy(deep=True)


def _hold_draft(bundle, account=sc.A):
    return next(d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == account)


def _approved(bundle, ledger):
    hold = _hold_draft(bundle)
    approve(bundle, hold.draft_id, approver="ravi", role="supervisor", reason_code="CHECKS_PASSED",
            sink=RecordingActionSink(), ledger=ledger)
    return hold


# --------------------------------------------------------------------------- pipeline events


def test_every_pipeline_write_is_sealed_into_one_chain(bundle):
    report = verify_chain(bundle.audit, case_id=CASE1)
    assert report.ok, report.summary()
    actions = [e.action for e in bundle.audit]
    assert actions[0] == "case_opened" and actions[-1] == "case_processed"
    for step in ("trace_built", "hold_plan_built", "draft_created", "draft_verified"):
        assert step in actions, step
    assert actions.index("trace_built") < actions.index("hold_plan_built") < actions.index("draft_created")
    assert all(e.case_id == CASE1 and e.hash for e in bundle.audit)


def test_the_hold_plan_event_records_the_showcase_totals_from_the_plan(bundle):
    ev = next(e for e in bundle.audit if e.action == "hold_plan_built")
    assert eq(ev.details["total_hold"], 142848.0) and eq(ev.details["total_recoverable"], 334848.0)
    assert eq(ev.details["total_hold"], bundle.plan.total_hold) and ev.details["passed"] is True
    assert len(ev.details["plan_sha256"]) == 64


def test_each_draft_is_recorded_with_its_fingerprint(bundle):
    created = {e.object_id: e for e in bundle.audit if e.action == "draft_created"}
    assert set(created) == {d.draft_id for d in bundle.drafts}
    for d in bundle.drafts:
        assert created[d.draft_id].details["draft_sha256"] == draft_fingerprint(d)
        assert created[d.draft_id].details["kind"] == d.kind.value


def test_the_approval_fingerprint_is_recorded_in_the_chain(bundle, ledger):
    hold = _approved(bundle, ledger)
    approval = bundle.approvals[-1]
    ev = next(e for e in bundle.audit if e.action == "draft_approved")
    assert ev.details["draft_sha256"] == approval.draft_sha256 == draft_fingerprint(hold)
    assert ev.details["approval_id"] == approval.approval_id and ev.actor == "ravi" and ev.role == "supervisor"
    emitted = [e for e in bundle.audit if e.action == "action_done"]
    assert emitted and all(e.details["draft_id"] == hold.draft_id for e in emitted)
    assert all(e.seq > ev.seq for e in emitted)
    assert verify_chain(bundle.audit, case_id=CASE1).ok

    # swapping the recorded fingerprint (to make the approval cover other content) breaks the chain there
    forged = [e.model_copy(deep=True) for e in bundle.audit]
    forged[ev.seq].details["draft_sha256"] = draft_fingerprint(_hold_draft(bundle, sc.B))
    r = verify_chain(forged, case_id=CASE1)
    assert not r.ok and r.code == CONTENT and r.first_bad_seq == ev.seq


def test_rejection_and_account_level_exception_stay_on_the_chain(bundle, ledger):
    b_hold = _hold_draft(bundle, sc.B)
    approve(bundle, b_hold.draft_id, approver="ravi", role="supervisor", reason_code="WRONG_ACCOUNT",
            decision=ApprovalDecision.REJECTED)
    req = request_account_level_hold(bundle, ledger, sc.A, [AccountLevelReason.FRAUDSTER_CONTROLLED_ACCOUNT],
                                     requested_by="asha", role="analyst")
    approve(bundle, req.draft_id, approver="ravi", role="supervisor", ledger=ledger, sink=RecordingActionSink())
    assert eq(bundle.plan.total_hold, 142848.0) and eq(bundle.plan.total_recoverable, 334848.0)
    actions = [(e.action, e.actor) for e in bundle.audit]
    assert ("draft_rejected", "ravi") in actions
    assert ("draft_created", "asha") in actions  # the officer's request: maker recorded in the chain
    assert ("account_level_hold_applied", "ravi") in actions
    redrafts = sorted(e.details.get("kind") for e in bundle.audit if e.action == "draft_created"
                      and e.details.get("reason") == "EXCEPTIONAL_ACCOUNT_LEVEL_HOLD")
    assert redrafts == ["CFCFRMS_UPDATE", "HOLD_INSTRUCTION"]  # the redrafted hold and the case's CFCFRMS update
    assert verify_chain(bundle.audit, case_id=CASE1).ok


# --------------------------------------------------------------------------- in-memory store


def test_store_round_trip_keeps_one_verified_anchored_chain(ledger):
    store = InMemoryCaseStore()
    b = process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    first = store.verify_audit(CASE1)
    assert first.ok and first.anchored and first.length == len(b.audit)
    head = [e.hash for e in store.get_bundle(CASE1).audit]

    work = store.get_bundle(CASE1)
    _approved(work, ledger)
    store.put_bundle(work)
    after = store.get_bundle(CASE1).audit
    assert [e.hash for e in after[: len(head)]] == head  # recorded events untouched
    assert len(after) == len(work.audit) and store.verify_audit(CASE1).ok


def test_store_append_audit_seals_and_keeps_its_own_copy(ledger):
    store = InMemoryCaseStore()
    process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    n = len(store.get_bundle(CASE1).audit)
    note = AuditEvent(at=utcnow(), actor="asha", role="analyst", action="viewed", object_type="page",
                      object_id="Hold Plan")
    store.append_audit(CASE1, note)
    assert note.seq == n and note.hash  # the caller's receipt
    assert store.get_bundle(CASE1).audit[-1] == note
    note.details["page"] = "edited after the fact"  # mutating the caller's object cannot touch the store
    assert store.verify_audit(CASE1).ok


def test_store_put_bundle_never_drops_or_rewrites_recorded_events(ledger):
    store = InMemoryCaseStore()
    process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    stale = store.get_bundle(CASE1)
    store.append_audit(CASE1, AuditEvent(at=utcnow(), actor="agent", role="system", action="clock_sweep",
                                         object_type="case", object_id=CASE1))
    recorded = [e.hash for e in store.get_bundle(CASE1).audit]

    stale.audit = stale.audit[:-3]  # a writer that tries to delete events ...
    stale.audit[1].details["accounts"] = 0  # ... and to rewrite one
    store.put_bundle(stale)
    got = store.get_bundle(CASE1).audit
    assert [e.hash for e in got[: len(recorded)]] == recorded  # every recorded event survives, unchanged
    assert got[-1].details["accounts"] == 0 and got[-1].seq == len(recorded)  # the altered copy is a new event
    assert store.verify_audit(CASE1).ok


def test_reprocessing_a_case_appends_to_its_history(ledger):
    store = InMemoryCaseStore()
    process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    n = len(store.get_bundle(CASE1).audit)
    process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    log = store.get_bundle(CASE1).audit
    assert len(log) > n and [e.action for e in log].count("case_opened") == 2
    assert store.verify_audit(CASE1).ok


def test_store_detects_tampering_of_its_own_rows(ledger):
    store = InMemoryCaseStore()
    b = process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    _approved(b, ledger)
    store.put_bundle(b)
    rows = store._bundles[CASE1].audit  # direct storage access, as an insider with database rights would have
    k = next(i for i, e in enumerate(rows) if e.action == "draft_approved")
    original = [e.model_copy(deep=True) for e in rows]

    rows[k].actor = "asha"
    r = store.verify_audit(CASE1)
    assert not r.ok and r.code == CONTENT and r.first_bad_seq == k

    rows[:] = [e.model_copy(deep=True) for e in original[:-1]]
    r = store.verify_audit(CASE1)
    assert not r.ok and r.code == TRUNCATED

    rows[:] = [e.model_copy(deep=True) for e in original]
    rows[k].details["reason_code"] = "BACKDATED"
    for i in range(k, len(rows)):
        rows[i].prev_hash = rows[i - 1].hash
        rows[i].hash = link_hash(rows[i], seq=i, prev_hash=rows[i].prev_hash, case_id=CASE1)
    assert verify_chain(rows, case_id=CASE1).ok  # consistent on its own ...
    r = store.verify_audit(CASE1)
    assert not r.ok and r.code == HEAD  # ... but not with the store's anchor


# --------------------------------------------------------------------------- file store (AMD edition, on-prem)


def test_file_store_chains_and_detects_an_edited_file(ledger, tmp_path):
    from editions.amd.app.file_store import FileCaseStore
    from wapsi.audit.__main__ import main

    store = FileCaseStore(tmp_path / "state")
    b = process_case(ledger, [sc.N1], llm=FakeLLM(), store=store)
    stale = store.get_bundle(CASE1)
    store.append_audit(CASE1, AuditEvent(at=utcnow(), actor="asha", role="analyst", action="viewed",
                                         object_type="page", object_id="Approvals"))
    _approved(stale, ledger)
    store.put_bundle(stale)  # a stale copy: the appended view event is kept
    log = store.get_bundle(CASE1).audit
    assert "viewed" in [e.action for e in log] and len(log) > len(b.audit)
    assert store.verify_audit(CASE1).ok
    path = tmp_path / "state" / "cases" / f"{CASE1}.json"
    assert main(["verify", str(path)]) == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    k = next(i for i, e in enumerate(data["audit"]) if e["action"] == "hold_plan_built")
    data["audit"][k]["details"]["total_hold"] = 100000.0  # someone edits the file on disk
    path.write_text(json.dumps(data), encoding="utf-8")
    r = store.verify_audit(CASE1)
    assert not r.ok and r.code == CONTENT and r.first_bad_seq == k
    assert main(["verify", str(path)]) == 1
