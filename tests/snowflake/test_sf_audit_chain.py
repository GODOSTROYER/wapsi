"""The Snowflake case store keeps each case's audit log hash-chained and append-only, exactly like the in-memory
store: procedures seal every event through ``wapsi.audit``, AUDIT_LOG rows carry the seal and are INSERT-only, and
the log read back verifies with ``verify_chain`` against the AUDIT_ANCHOR checkpoint. The tamper drill edits,
deletes, reorders, truncates and re-hashes rows behind the store's back (as a DBA could) and every attack is caught."""

from __future__ import annotations

import json

import pytest
from wapsi_sf.casestore import SnowflakeCaseStore, stable_json

from wapsi.audit import GENESIS_HASH, verify_chain
from wapsi.audit.chain import CONTENT, HEAD, LINK, SEQUENCE, TRUNCATED, UNCHAINED, seal
from wapsi.fixtures.showcase import N1
from wapsi.models import AuditEvent
from wapsi.timeutil import utcnow

C1 = f"CASE-{N1}"


def _case(s, procs, *, drafts: bool = False) -> SnowflakeCaseStore:
    assert procs.trace_case_proc(s, N1)["ok"]
    assert procs.build_hold_plan_proc(s, C1)["ok"]
    if drafts:
        assert procs.draft_proc(s, C1, "ALL")["ok"]
    return SnowflakeCaseStore(s)


def _rows(s):
    return s.rows("SELECT seq, prev_hash, hash, action FROM CASEMGMT.AUDIT_LOG WHERE case_id = ? ORDER BY seq", [C1])


def test_every_event_the_procedures_write_is_sealed_and_verifies(sf_session, procs):
    store = _case(sf_session, procs, drafts=True)
    rows = _rows(sf_session)
    assert [r["seq"] for r in rows] == list(range(len(rows))) and len(rows) >= 5
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert all(r["hash"] and r["prev_hash"] == prev["hash"] for prev, r in zip(rows, rows[1:], strict=False))
    report = store.verify_audit(C1)
    assert report.ok and report.anchored and report.length == len(rows), report.summary()
    anchor = store.audit_anchor(C1)
    assert (anchor.length, anchor.head) == (len(rows), rows[-1]["hash"])
    # the bundle a procedure reads carries the same chain, and so does the stored JSON copy
    bundle = store.get_bundle(C1)
    assert [e.hash for e in bundle.audit] == [r["hash"] for r in rows]
    assert verify_chain(bundle.audit, case_id=C1).ok
    raw = sf_session.rows("SELECT bundle FROM CASEMGMT.CASE_BUNDLES WHERE case_id = ?", [C1])[0]["bundle"]
    assert [e["hash"] for e in json.loads(raw)["audit"]] == [r["hash"] for r in rows]
    assert procs.case_summary_proc(sf_session, C1)["audit_chain"] == {
        "status": "intact", "events": len(rows), "anchored": True}


def test_rerunning_appends_after_the_history(sf_session, procs):
    store = _case(sf_session, procs)
    before = _rows(sf_session)
    _case(sf_session, procs)  # trace + plan again
    after = _rows(sf_session)
    assert after[: len(before)] == before and len(after) > len(before)
    assert store.verify_audit(C1).ok


def test_put_bundle_never_drops_or_changes_recorded_events(sf_session, procs):
    """A write from an altered or stale copy cannot rewrite history: recorded events stay, the altered copy is
    recorded as a new, chained event (the in-memory store's extend_stored rule)."""
    store = _case(sf_session, procs)
    before = _rows(sf_session)
    copy = store.get_bundle(C1)
    copy.audit[1].details = {**copy.audit[1].details, "total_hold": 1.0}  # altered
    del copy.audit[-1]  # stale
    store.put_bundle(copy)
    after = _rows(sf_session)
    assert after[: len(before)] == before and len(after) == len(before) + 1
    assert store.verify_audit(C1).ok
    assert copy.audit[-1].seq == len(before)  # the caller's copy now carries the stored chain (its receipt)


def test_append_audit_seals_the_next_link_and_stamps_the_receipt(sf_session, procs):
    store = _case(sf_session, procs)
    n = len(_rows(sf_session))
    ev = AuditEvent(at=utcnow(), actor="auditor@hb01", role="auditor", action="viewed", object_type="page",
                    object_id="Hold Plan", details={"amount": 43200.0})
    store.append_audit(C1, ev)
    assert ev.seq == n and ev.case_id == C1 and ev.hash == _rows(sf_session)[-1]["hash"]
    assert ev.details == {"amount": 43200}  # stored JSON-stable
    report = store.verify_audit(C1)
    assert report.ok and report.length == n + 1 and store.audit_anchor(C1).length == n + 1


def test_append_audit_procedure_is_the_runners_sealed_path(sf_session, procs):
    store = _case(sf_session, procs)
    n = len(_rows(sf_session))
    res = procs.append_audit_proc(sf_session, C1, "action_executed", "action", "ACT-1", '{"kind": "JIRA_TICKET"}')
    assert res["ok"] and res["seq"] == n and store.verify_audit(C1).ok
    assert _rows(sf_session)[-1]["action"] == "action_executed"
    assert not procs.append_audit_proc(sf_session, C1, "DROP TABLE", "action", "ACT-1")["ok"]
    assert not procs.append_audit_proc(sf_session, "CASE-99999999", "viewed", "page", "x-1")["ok"]
    assert not procs.append_audit_proc(sf_session, C1, "viewed", "page", "x-1", "not json")["ok"]


def test_details_are_stored_json_stable():
    assert stable_json({"a": 142848.0, "b": [1.5, 2.0], "c": True, "d": {"e": 0.1}}) == {
        "a": 142848, "b": [1.5, 2], "c": True, "d": {"e": 0.1}}


def test_a_log_written_before_sealing_verifies_as_not_chained(sf_session):
    sf_session.sql("INSERT INTO CASEMGMT.AUDIT_LOG (event_id, case_id, event_at, actor, actor_role, action, "
                   "object_type, object_id, details) VALUES ('EV-old', 'CASE-1', '2026-09-20 10:00:00.000000', "
                   "'wapsi', 'system', 'case_opened', 'case', 'CASE-1', '{}')")
    report = SnowflakeCaseStore(sf_session).verify_audit("CASE-1")
    assert report.code == UNCHAINED and report.status == "unchained"


# --------------------------------------------------------------------------- tamper drill


def _sql(s, statement, params=()):
    """Tamper behind the store's back (the guard hook and RBAC stop this in Snowflake; a DBA could still try)."""
    s.conn.execute(statement, list(params))
    s.conn.commit()


def _edit(s, n):
    _sql(s, "UPDATE CASEMGMT.AUDIT_LOG SET details = ? WHERE case_id = ? AND seq = 2",
         [json.dumps({"total_hold": 1}), C1])


def _delete(s, n):
    _sql(s, "DELETE FROM CASEMGMT.AUDIT_LOG WHERE case_id = ? AND seq = 2", [C1])


def _reorder(s, n):
    _sql(s, "UPDATE CASEMGMT.AUDIT_LOG SET seq = CASE seq WHEN 1 THEN 2 ELSE 1 END WHERE case_id = ? "
            "AND seq IN (1, 2)", [C1])


def _truncate(s, n):
    _sql(s, "DELETE FROM CASEMGMT.AUDIT_LOG WHERE case_id = ? AND seq >= ?", [C1, n - 2])


def _rehash(s, n):
    """Edit event #2 and recompute every hash after it, so the log is self-consistent again."""
    store = SnowflakeCaseStore(s)
    events = store.audit_log(C1)
    events[2].details = {"total_hold": 1}
    prev = events[1].hash
    for i in range(2, len(events)):
        old = events[i].hash
        seal(events[i], seq=i, prev_hash=prev, case_id=C1)
        prev = events[i].hash
        _sql(s, "UPDATE CASEMGMT.AUDIT_LOG SET details = ?, prev_hash = ?, hash = ? WHERE hash = ?",
             [json.dumps(events[i].details), events[i].prev_hash, events[i].hash, old])
    assert verify_chain(events, case_id=C1).ok  # without the anchor the rewrite is invisible


@pytest.mark.parametrize(("attack", "codes", "first_bad"), [
    (_edit, {CONTENT}, 2),
    (_delete, {SEQUENCE}, 2),
    (_reorder, {CONTENT, LINK}, 1),
    (_truncate, {TRUNCATED}, None),
    (_rehash, {HEAD}, None),
])
def test_tamper_drill_on_the_snowflake_store(sf_session, procs, attack, codes, first_bad):
    store = _case(sf_session, procs)
    for page in ("Inbox", "Hold Plan", "Audit"):  # a longer log: the attacks hit the middle, not the end
        store.append_audit(C1, AuditEvent(at=utcnow(), actor="auditor@hb01", role="auditor", action="viewed",
                                          object_type="page", object_id=page))
    n = len(_rows(sf_session))
    assert n >= 6 and store.verify_audit(C1).ok
    attack(sf_session, n)
    report = store.verify_audit(C1)
    assert not report.ok and report.status == "broken" and report.code in codes, report.summary()
    if first_bad is not None:
        assert report.first_bad_seq == first_bad
    summary = procs.case_summary_proc(sf_session, C1)["audit_chain"]
    assert summary["status"] == "broken" and summary["code"] == report.code
