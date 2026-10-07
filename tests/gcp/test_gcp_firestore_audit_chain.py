"""FirestoreCaseStore seals the hash-chained audit log exactly like InMemoryCaseStore: transactional appends against
an anchor kept outside the log (``audit_anchors/{case_id}``), reads in seq order, a tamper drill on the stored
documents, serialised concurrent appends, idempotent retries, and the GCP audit page's badge.

Runs on ``gcp_fakes.FakeFirestore``, whose transactions are driven by the real
``google.cloud.firestore.transactional`` decorator (optimistic concurrency: a commit whose reads went stale is
ABORTED and the decorator re-runs it), so the store's transaction code runs as it would against Firestore.
"""

from __future__ import annotations

import copy
import html
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from gcp_fakes import FakeFirestore

from wapsi.adapters import firestore_store
from wapsi.adapters.firestore_store import (
    UNREADABLE_ACTION,
    AuditChainError,
    FirestoreCaseStore,
    audit_doc_id,
)
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.adapters.memory import InMemoryCaseStore, RecordingActionSink
from wapsi.audit.chain import (
    ANCHOR,
    CONTENT,
    GENESIS_HASH,
    HEAD,
    LINK,
    SEQUENCE,
    TRUNCATED,
    ChainAnchor,
    link_hash,
    verify_chain,
)
from wapsi.audit.drill import scenarios
from wapsi.fixtures import showcase as sc
from wapsi.models import AuditEvent, CaseBundle, DraftKind
from wapsi.pipeline.run import approve, process_case
from wapsi.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def approved() -> CaseBundle:
    """The showcase case after a supervisor approved account A's hold instruction (as in the tamper drill)."""
    ledger = sc.showcase_ledger()
    bundle = process_case(ledger, [sc.N1], llm=FakeLLM())
    hold = next(d for d in bundle.drafts if d.kind == DraftKind.HOLD_INSTRUCTION and d.account_id == sc.A)
    approve(bundle, hold.draft_id, approver="ravi.supervisor", role="supervisor", reason_code="CHECKS_PASSED",
            sink=RecordingActionSink(), ledger=ledger)
    return bundle


def _store(**kwargs) -> tuple[FakeFirestore, FirestoreCaseStore]:
    fs = FakeFirestore()
    return fs, FirestoreCaseStore(client=fs, **kwargs)


def _event(action: str = "viewed", actor: str = "asha", **details) -> AuditEvent:
    return AuditEvent(at=utcnow(), actor=actor, role="analyst", action=action, object_type="page",
                      object_id="Hold Plan", details=details)


def _audit_paths(fs: FakeFirestore, cid: str) -> list[str]:
    return sorted(p for p in fs.docs if p.startswith(f"cases/{cid}/audit/"))


def _seals(events: list[AuditEvent]) -> list[tuple[int | None, str | None, str | None]]:
    return [(e.seq, e.prev_hash, e.hash) for e in events]


# --------------------------------------------------------------------------- sealing and parity


def test_fresh_case_is_sealed_and_anchored_outside_the_log(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)

    paths = _audit_paths(fs, cid)
    assert paths == [f"cases/{cid}/audit/{audit_doc_id(i)}" for i in range(n)]  # gap-free, id = seq
    for i, path in enumerate(paths):
        doc = fs.docs[path]
        assert doc["seq"] == doc["event"]["seq"] == i
        assert doc["hash"] == doc["event"]["hash"] and len(doc["hash"]) == 64
        assert doc["prev_hash"] == (fs.docs[paths[i - 1]]["hash"] if i else GENESIS_HASH)

    anchor = fs.docs[f"audit_anchors/{cid}"]  # outside cases/{cid}/audit
    assert anchor["length"] == n and anchor["head"] == fs.docs[paths[-1]]["hash"]
    assert anchor["case_id"] == cid and anchor["chain"] == "wapsi/audit-chain/v1"
    assert store.audit_anchor(cid) == ChainAnchor(n, anchor["head"])

    report = store.verify_audit(cid)
    assert report.ok and report.status == "intact" and report.anchored and report.length == n
    assert "matches its anchor" in report.summary()
    assert store.get_bundle(cid) == approved  # the pipeline's seals survive the round trip unchanged


def test_chain_matches_the_in_memory_store_link_for_link(approved):
    fs, store = _store()
    mem = InMemoryCaseStore()
    cid = approved.case.case_id
    stale = approved.model_copy(deep=True)
    stale.audit = stale.audit[:-3]  # a caller whose copy is missing recorded events
    stale.audit[2].details["tampered"] = True  # ...and has an altered copy of one
    viewed = _event(n=1)
    for s in (store, mem):
        s.put_bundle(approved)
        s.append_audit(cid, viewed.model_copy(deep=True))
        s.put_bundle(stale)  # append-only: nothing dropped or changed; the altered copy becomes a new event
    events = store.get_bundle(cid).audit
    assert len(events) == len(approved.audit) + 2
    assert _seals(events) == _seals(mem.get_bundle(cid).audit)
    assert [e.model_dump() for e in events] == [e.model_dump() for e in mem.get_bundle(cid).audit]
    assert store.audit_anchor(cid) == mem.audit_anchor(cid)
    assert store.verify_audit(cid).ok and mem.verify_audit(cid).ok


def test_append_stamps_the_receipt_and_reseals_onto_the_stored_head(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    head = store.audit_anchor(cid).head

    ev = _event()
    store.append_audit(cid, ev)
    assert (ev.seq, ev.case_id, ev.prev_hash) == (n, cid, head)  # the caller's receipt
    assert ev.hash == link_hash(ev, seq=n, prev_hash=head, case_id=cid)
    assert fs.docs[f"cases/{cid}/audit/{audit_doc_id(n)}"]["event"] == ev.model_dump(mode="json")

    # an event carrying someone else's seal (another case, a stale session position) is re-sealed at the head
    foreign = _event(action="exported")
    foreign.seq, foreign.case_id, foreign.prev_hash, foreign.hash = 3, "CASE-OTHER", GENESIS_HASH, "f" * 64
    store.append_audit(cid, foreign)
    assert (foreign.seq, foreign.case_id, foreign.prev_hash) == (n + 1, cid, ev.hash)
    assert store.verify_audit(cid).ok and store.audit_anchor(cid).length == n + 2


def test_reads_follow_seq_not_time(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    earliest = min(e.at for e in approved.audit) - timedelta(days=3)
    late_but_backdated = AuditEvent(at=earliest, actor="ravi", role="supervisor", action="note_added",
                                    object_type="case", object_id=cid)
    store.append_audit(cid, late_but_backdated)
    events = store.get_bundle(cid).audit
    assert [e.seq for e in events] == list(range(len(events)))  # 0..n, across the 9 -> 10 id boundary
    assert events[-1].action == "note_added"
    assert store.verify_audit(cid).ok


def test_reput_and_readback_are_idempotent(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    store.append_audit(cid, _event())
    before = dict(fs.docs)
    again = store.get_bundle(cid)
    store.put_bundle(again)
    store.put_bundle(approved)  # an older copy: nothing to add
    assert _audit_paths(fs, cid) == sorted(p for p in before if p.startswith(f"cases/{cid}/audit/"))
    assert store.verify_audit(cid).ok and store.audit_anchor(cid).length == len(approved.audit) + 1


def test_many_new_events_span_several_transactions(approved, monkeypatch):
    monkeypatch.setattr(firestore_store, "MAX_BATCH_OPS", 7)  # 5 audit writes per transaction
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    assert fs.txn_commits >= -(-len(approved.audit) // 5)
    assert store.get_bundle(cid) == approved
    assert store.verify_audit(cid).ok


# --------------------------------------------------------------------------- tamper drill


def _rewrite(fs: FakeFirestore, cid: str, events: list[AuditEvent]) -> None:
    """An insider with database access replaces the stored log (document i holds events[i])."""
    for p in _audit_paths(fs, cid):
        del fs.docs[p]
    for i, e in enumerate(events):
        fs.docs[f"cases/{cid}/audit/{audit_doc_id(i)}"] = FirestoreCaseStore._audit_doc(e)


@pytest.mark.parametrize("name", ["edit", "edit+rehash", "delete", "reorder", "truncate", "rechain"])
def test_tamper_drill_on_stored_documents(approved, name):
    """The six tampers of ``python -m wapsi.audit drill``, applied to the Firestore documents themselves."""
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    events = store.get_bundle(cid).audit
    k = next(i for i, e in enumerate(events) if e.action == "draft_approved")
    tamper = {n: fn for n, _, fn in scenarios(k)}[name]
    forged = [e.model_copy(deep=True) for e in events]
    tamper(forged)
    _rewrite(fs, cid, forged)

    report = store.verify_audit(cid)
    expected = {"edit": (CONTENT, k), "edit+rehash": (LINK, k + 1), "delete": (SEQUENCE, k),
                "reorder": (SEQUENCE, k - 1), "truncate": (TRUNCATED, len(events) - 2),
                "rechain": (HEAD, len(events) - 1)}[name]
    assert not report.ok and report.status == "broken"
    assert (report.code, report.first_bad_seq) == expected, report.summary()


def test_swapping_two_documents_is_detected(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    a, b = (f"cases/{cid}/audit/{audit_doc_id(i)}" for i in (4, 5))
    fs.docs[a], fs.docs[b] = fs.docs[b], fs.docs[a]
    report = store.verify_audit(cid)
    assert (report.ok, report.code, report.first_bad_seq) == (False, SEQUENCE, 4)


def test_deleted_anchor_and_forged_append_are_detected(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    anchor_doc = fs.docs[f"audit_anchors/{cid}"]

    # an event written straight into the log, bypassing the anchor (validly sealed onto the head)
    forged = _event(action="draft_approved", actor="mallory")
    head = store.audit_anchor(cid).head
    forged.seq, forged.case_id, forged.prev_hash = n, cid, head
    forged.hash = link_hash(forged, seq=n, prev_hash=head, case_id=cid)
    fs.docs[f"cases/{cid}/audit/{audit_doc_id(n)}"] = FirestoreCaseStore._audit_doc(forged)
    report = store.verify_audit(cid)
    assert (report.ok, report.code, report.first_bad_seq) == (False, ANCHOR, n)
    assert verify_chain(store.get_bundle(cid).audit, case_id=cid).ok  # the log alone looks fine

    # truncate the log and delete the anchor that would give it away
    del fs.docs[f"cases/{cid}/audit/{audit_doc_id(n)}"]
    del fs.docs[f"cases/{cid}/audit/{audit_doc_id(n - 1)}"]
    del fs.docs[f"audit_anchors/{cid}"]
    report = store.verify_audit(cid)
    assert (report.ok, report.code) == (False, ANCHOR) and "anchor" in report.reason
    # ...and the store refuses to write on top of it instead of starting a fresh anchor that would hide it
    before = dict(fs.docs)
    with pytest.raises(AuditChainError, match="anchor"):
        store.append_audit(cid, _event())
    with pytest.raises(AuditChainError):
        store.put_bundle(approved)
    assert fs.docs == before  # nothing written (the failed put's chunks were cleaned up too)

    fs.docs[f"audit_anchors/{cid}"] = anchor_doc  # the anchor is back: the truncation shows
    assert store.verify_audit(cid).code == TRUNCATED


def test_forged_document_at_the_head_blocks_writes_with_a_chain_error(approved):
    """A document written at the anchor's head without advancing the anchor makes the next create collide (409
    ALREADY_EXISTS); the store refuses with ``AuditChainError`` instead of a raw Google error, writes nothing and
    keeps reporting the forgery."""
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    forged = _event(action="draft_approved", actor="mallory")
    head = store.audit_anchor(cid).head
    forged.seq, forged.case_id, forged.prev_hash = n, cid, head
    forged.hash = link_hash(forged, seq=n, prev_hash=head, case_id=cid)
    fs.docs[f"cases/{cid}/audit/{audit_doc_id(n)}"] = FirestoreCaseStore._audit_doc(forged)
    before = copy.deepcopy(fs.docs)

    with pytest.raises(AuditChainError, match="without advancing the anchor"):
        store.append_audit(cid, _event())
    grown = approved.model_copy(deep=True)
    grown.audit.append(_event(action="note_added"))
    with pytest.raises(AuditChainError):
        store.put_bundle(grown)
    assert fs.docs == before  # nothing written; the failed put's chunks were cleaned up
    assert (store.verify_audit(cid).code, store.verify_audit(cid).first_bad_seq) == (ANCHOR, n)


@pytest.mark.parametrize("edit", ["extra_field", "retyped_field", "removed_field", "no_event_map"])
def test_unreadable_audit_document_is_reported_at_its_position(approved, edit):
    """An audit document edited in the console so that it no longer parses (Wapsi's models forbid extra fields)
    must not crash reads or hide behind the session's copy: the case still loads with a placeholder at that
    position, ``verify_audit`` reports the chain broken there, and writes carry on at the anchor's head."""
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n, k = len(approved.audit), 3
    doc = fs.docs[f"cases/{cid}/audit/{audit_doc_id(k)}"]
    {"extra_field": lambda: doc["event"].update(note="added in the console"),
     "retyped_field": lambda: doc["event"].update(actor=7),
     "removed_field": lambda: doc["event"].pop("at"),
     "no_event_map": lambda: doc.pop("event")}[edit]()

    report = store.verify_audit(cid)
    assert (report.ok, report.code, report.first_bad_seq) == (False, CONTENT, k), report.summary()
    assert "can no longer be read" in report.reason and report.length == n and report.anchored

    loaded = store.get_bundle(cid)  # no exception: the UI keeps working
    assert len(loaded.audit) == n and loaded.audit[k].action == UNREADABLE_ACTION
    assert loaded.audit[k].object_id == f"audit/{audit_doc_id(k)}" and loaded.audit[k].hash is None
    session = verify_chain(loaded.audit, case_id=cid)  # the session's copy shows the break too
    assert (session.ok, session.first_bad_seq) == (False, k)

    store.put_bundle(loaded)  # the placeholder is matched, never written back
    assert len(_audit_paths(fs, cid)) == n
    store.append_audit(cid, _event())
    assert store.audit_anchor(cid).length == n + 1 and len(_audit_paths(fs, cid)) == n + 1
    assert (store.verify_audit(cid).code, store.verify_audit(cid).first_bad_seq) == (CONTENT, k)


def test_later_writes_never_paper_over_a_truncation(approved):
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    del fs.docs[f"cases/{cid}/audit/{audit_doc_id(n - 1)}"]
    store.append_audit(cid, _event())  # goes to the anchor's head (seq n), not after the remaining documents
    store.put_bundle(approved)  # re-records the removed event's content as a new event, after the head
    events = store.get_bundle(cid).audit
    assert events[-2].seq == n and events[-1].action == approved.audit[-1].action
    report = store.verify_audit(cid)
    assert (report.ok, report.code, report.first_bad_seq) == (False, SEQUENCE, n - 1)


def test_full_insider_rewrite_needs_an_external_anchor(approved):
    """Rewriting the log *and* its anchor in the same database is not detectable from inside it; an anchor kept in
    another trust domain (exported receipt, WORM bucket) still catches it."""
    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    exported = store.audit_anchor(cid)  # e.g. the daily digest sent to the CISO
    events = store.get_bundle(cid).audit
    k = next(i for i, e in enumerate(events) if e.action == "draft_approved")
    forged = [e.model_copy(deep=True) for e in events]
    {n: fn for n, _, fn in scenarios(k)}["rechain"](forged)
    _rewrite(fs, cid, forged)
    fs.docs[f"audit_anchors/{cid}"].update(length=len(forged), head=forged[-1].hash)
    assert store.verify_audit(cid).ok
    outside = verify_chain(store.get_bundle(cid).audit, case_id=cid, anchor=exported)
    assert (outside.ok, outside.code) == (False, HEAD)


# --------------------------------------------------------------------------- concurrency and retries


def test_contended_append_is_aborted_and_retried_on_the_new_head(approved):
    fs, store = _store()
    other = FirestoreCaseStore(client=fs)  # a second Cloud Run instance
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    mine, theirs = _event(actor="asha"), _event(actor="ravi")
    fired: list[bool] = []

    def competitor(txn) -> None:
        if not fired:  # between our read of the head and our commit, the other instance appends
            fired.append(True)
            other.append_audit(cid, theirs)

    fs.before_commit = competitor
    aborts = fs.aborts
    store.append_audit(cid, mine)
    fs.before_commit = None

    assert fs.aborts == aborts + 1  # our first attempt read a stale head and was aborted, then re-run
    assert (theirs.seq, mine.seq) == (n, n + 1) and mine.prev_hash == theirs.hash
    events = store.get_bundle(cid).audit
    assert [e.actor for e in events[n:]] == ["ravi", "asha"]  # one copy each, no double append
    assert store.verify_audit(cid).ok and store.audit_anchor(cid).length == n + 2


def test_concurrent_appends_serialise_into_one_chain(approved):
    fs, store = _store(max_attempts=100)
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)
    threads, per_thread = 6, 8
    start = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker(t: int) -> None:
        instance = FirestoreCaseStore(client=fs, max_attempts=100)
        start.wait()
        try:
            for i in range(per_thread):
                instance.append_audit(cid, _event(actor=f"officer-{t}", i=i))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    pool = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # interleave threads mid-transaction: commits go stale, abort and are re-run
    try:
        for th in pool:
            th.start()
        for th in pool:
            th.join(timeout=120)
    finally:
        sys.setswitchinterval(switch)
    assert not errors, errors

    events = store.get_bundle(cid).audit
    added = events[n:]
    assert len(added) == threads * per_thread
    assert {(e.actor, e.details["i"]) for e in added} == {(f"officer-{t}", i) for t in range(threads)
                                                          for i in range(per_thread)}
    for t in range(threads):  # each writer's events keep their order
        assert [e.details["i"] for e in added if e.actor == f"officer-{t}"] == list(range(per_thread))
    assert [e.seq for e in events] == list(range(len(events)))
    report = store.verify_audit(cid)
    assert report.ok and report.length == n + threads * per_thread


def test_put_bundle_and_appends_interleave_safely(approved):
    fs, store = _store()
    cid = approved.case.case_id
    base = approved.model_copy(deep=True)
    base.audit = base.audit[:10]
    store.put_bundle(base)
    fired: list[bool] = []

    def competitor(txn) -> None:
        if not fired:  # an agent tool call is audited while the UI persists the case
            fired.append(True)
            FirestoreCaseStore(client=fs).append_audit(cid, _event(action="agent_tool:hold_plan"))

    fs.before_commit = competitor
    store.put_bundle(approved)
    fs.before_commit = None
    events = store.get_bundle(cid).audit
    assert len(events) == len(approved.audit) + 1
    assert events[10].action == "agent_tool:hold_plan"  # landed first; the bundle's new events follow it
    assert [e.action for e in events[11:]] == [e.action for e in approved.audit[10:]]
    assert store.verify_audit(cid).ok


def test_retry_after_a_lost_commit_response_does_not_double_append(approved):
    from google.api_core import exceptions

    fs, store = _store()
    store.put_bundle(approved)
    cid = approved.case.case_id
    n = len(approved.audit)

    def lose_response(txn) -> None:
        fs.after_commit = None
        raise exceptions.ServiceUnavailable("connection reset after commit")

    ev = _event(action="draft_approved", actor="ravi")
    fs.after_commit = lose_response
    with pytest.raises(exceptions.ServiceUnavailable):
        store.append_audit(cid, ev)  # the write went through; the caller never heard back
    store.append_audit(cid, _event(actor="someone.else"))  # another writer appends meanwhile
    store.append_audit(cid, ev)  # the caller retries with the same (stamped) event
    events = store.get_bundle(cid).audit
    assert [e.action for e in events[n:]].count("draft_approved") == 1
    assert len(events) == n + 2 and store.verify_audit(cid).ok

    store.append_audit(cid, ev)  # and again, later: still recorded once
    assert len(store.get_bundle(cid).audit) == n + 2


# --------------------------------------------------------------------------- configuration


def test_anchor_collection_naming_and_validation():
    fs = FakeFirestore()
    assert FirestoreCaseStore(client=fs).anchor_collection == "audit_anchors"
    assert FirestoreCaseStore(client=fs, collection="cases_dev").anchor_collection == "cases_dev_audit_anchors"
    assert FirestoreCaseStore(client=fs, anchor_collection="worm_anchors").anchor_collection == "worm_anchors"
    for bad in ({"anchor_collection": "cases"}, {"anchor_collection": "a/b"}, {"max_attempts": 0}):
        with pytest.raises(ValueError):
            FirestoreCaseStore(client=fs, **bad)
    store = FirestoreCaseStore(client=fs)
    with pytest.raises(KeyError):
        store.verify_audit("CASE-00000000000000")
    assert store.audit_anchor("CASE-00000000000000") is None
    with pytest.raises(ValueError):
        audit_doc_id(-1)


def test_security_rules_make_audit_documents_create_only():
    text = (ROOT / "editions" / "gcp" / "deploy" / "firestore.rules").read_text(encoding="utf-8")
    assert text.startswith("rules_version = '2';") and text.count("{") == text.count("}")
    default = text[text.index("match /{document=**}"):text.index("match /cases/{caseId}/audit/{seq}")]
    assert "allow read, write: if false;" in default  # no client access unless a rule below grants it
    audit = text[text.index("match /cases/{caseId}/audit/{seq}"):text.index("match /audit_anchors/{caseId}")]
    assert "allow create:" in audit and "allow update, delete: if false;" in audit
    assert "anchorAfter(caseId).head == request.resource.data.hash" in audit  # sealed together with the anchor
    anchors = text[text.index("match /audit_anchors/{caseId}"):]
    assert "request.resource.data.length > resource.data.length" in anchors and "allow delete: if false;" in anchors
    readme = (ROOT / "editions" / "gcp" / "README.md").read_text(encoding="utf-8")
    assert "## Audit log on Firestore" in readme and "deploy/firestore.rules" in readme and "bypass rules" in readme


# --------------------------------------------------------------------------- the GCP audit page


def _firestore_ds(demo_services):
    from editions.gcp.app.ui_source import CaseStoreDataSource

    fs = FakeFirestore()
    demo_services.store = FirestoreCaseStore(client=fs)
    return fs, CaseStoreDataSource(demo_services)  # seeds the showcase cases into Firestore


def test_ui_badge_reports_the_stored_anchored_chain(demo_services):
    from ui.views.audit import chain_chip

    fs, ds = _firestore_ds(demo_services)
    cid = f"CASE-{sc.N1}"
    report = ds.verify_audit(cid)
    assert report.ok and report.anchored  # the store's chain and anchor, not the session's copy
    chip = chain_chip(cid, report)
    assert f"{cid} · chain intact · {report.length} events" in chip and "matches its anchor" in chip

    ds.append_audit(cid, _event())
    assert ds.verify_audit(cid).ok and ds.verify_audit(cid).length == report.length + 1

    fs.docs[f"cases/{cid}/audit/{audit_doc_id(1)}"]["event"]["actor"] = "someone.else"  # edited in Firestore
    broken = ds.verify_audit(cid)
    assert (broken.ok, broken.code, broken.first_bad_seq) == (False, CONTENT, 1)
    assert verify_chain(ds.get_bundle(cid).audit, case_id=cid).ok  # the session's copy alone would hide it


def test_ui_catches_up_when_the_store_holds_events_the_session_has_not_seen(demo_services):
    fs, ds = _firestore_ds(demo_services)
    cid = f"CASE-{sc.N1}"
    demo_services.store.append_audit(cid, _event(action="agent_tool:trace_money", actor="wapsi_officer"))
    ds.append_audit(cid, _event())  # sealed by the store after the agent's event
    actions = [e.action for e in ds.get_bundle(cid).audit]
    assert actions[-2:] == ["agent_tool:trace_money", "viewed"]
    assert _seals(ds.get_bundle(cid).audit) == _seals(demo_services.store.get_bundle(cid).audit)
    assert ds.verify_audit(cid).ok


def test_audit_page_shows_chain_intact_for_a_fresh_firestore_case(demo_services, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    fs, ds = _firestore_ds(demo_services)
    cid = f"CASE-{sc.N1}"
    at = AppTest.from_file(str(ROOT / "editions" / "gcp" / "ui_app.py"), default_timeout=120)
    at.session_state["_wapsi_ds"] = ds
    at.session_state["case_id"] = cid
    at.session_state["role"] = "auditor"
    at.session_state["page"] = "Audit"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = "\n".join(html.unescape(m.value) for m in at.markdown)
    assert f"{cid} · chain intact" in text and "matches its anchor" in text
    assert f"CASE-{sc.N2} · chain intact" in text

    at.button(key="audit_verify_chain").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("every hash and link checks out" in s.value for s in at.success) and not at.error
    assert ds.verify_audit(cid).ok

    fs.docs[f"cases/{cid}/audit/{audit_doc_id(0)}"]["event"]["details"]["ncrp_ids"] = ["0"]
    at.run()
    text = "\n".join(html.unescape(m.value) for m in at.markdown)
    assert f"{cid} · chain broken at event #0" in text


def test_audit_page_survives_an_unreadable_firestore_document(demo_services, monkeypatch):
    """A console edit that breaks an audit document's schema shows as a broken chain on the page, not a crash and
    not the session's (intact) copy."""
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    fs, ds = _firestore_ds(demo_services)
    cid = f"CASE-{sc.N1}"
    fs.docs[f"cases/{cid}/audit/{audit_doc_id(2)}"]["event"]["note"] = "added in the console"
    at = AppTest.from_file(str(ROOT / "editions" / "gcp" / "ui_app.py"), default_timeout=120)
    at.session_state["_wapsi_ds"] = ds
    at.session_state["case_id"] = cid
    at.session_state["role"] = "auditor"
    at.session_state["page"] = "Audit"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    text = "\n".join(html.unescape(m.value) for m in at.markdown)
    assert f"{cid} · chain broken at event #2" in text and "can no longer be read" in text
    assert f"CASE-{sc.N2} · chain intact" in text


# --------------------------------------------------------------------------- the clock sweep


def test_clock_sweep_keeps_going_when_one_case_refuses_audit_writes(demo_services):
    """A case whose anchor was deleted refuses audit writes (``AuditChainError``); the twice-daily sweep must still
    alert on, and record, every other case."""
    from editions.gcp.app import workflow as wf

    fs, _ = _firestore_ds(demo_services)
    broken, fine = f"CASE-{sc.N1}", f"CASE-{sc.N2}"
    del fs.docs[f"audit_anchors/{broken}"]
    result = wf.sweep_clocks({})
    assert result["outcome"] == "clocks_swept" and result["cases"] == 2
    alerted = {a["case_id"] for a in result["alerts"]}
    assert result["audit_failed"] == ([broken] if broken in alerted else [])
    if fine in alerted:
        assert demo_services.store.get_bundle(fine).audit[-1].action == "clock_sweep"
        assert demo_services.store.verify_audit(fine).ok
