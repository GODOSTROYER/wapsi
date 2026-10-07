"""Hash chain: definition, append-only semantics and tamper detection (edit, delete, insert, reorder, truncate,
re-hash) on synthetic events."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from wapsi.audit.chain import (
    CASE,
    CONTENT,
    GENESIS_HASH,
    HEAD,
    LINK,
    OK,
    SEQUENCE,
    TRUNCATED,
    UNCHAINED,
    UNSEALED,
    anchor_of,
    append_event,
    canonical_json,
    chain_extend,
    extend_stored,
    link_hash,
    payload_digest,
    verify_chain,
)
from wapsi.models import AuditEvent
from wapsi.timeutil import IST

CID = "CASE-31009260000101"
T0 = datetime(2026, 9, 16, 5, 30, tzinfo=UTC)


def ev(i: int, action: str = "viewed", actor: str = "asha", **details) -> AuditEvent:
    return AuditEvent(at=T0 + timedelta(seconds=i), actor=actor, role="analyst", action=action,
                      object_type="case", object_id=CID, details=details or {"n": i})


def chain(n: int = 6, case_id: str | None = CID) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    for i in range(n):
        append_event(events, ev(i), case_id=case_id)
    return events


def copy(events: list[AuditEvent]) -> list[AuditEvent]:
    return [e.model_copy(deep=True) for e in events]


# --------------------------------------------------------------------------- definition


def test_genesis_is_a_fixed_constant():
    assert GENESIS_HASH == hashlib.sha256(b"wapsi/audit-chain/genesis/v1").hexdigest()
    assert GENESIS_HASH == "06bdc5a5721477db1106e72c5319648974fcc44839787796d4ae076ea5f73299"  # never changes


def test_hash_is_sha256_over_canonical_json_of_the_eight_fields():
    e = append_event([], ev(0, action="draft_approved", draft_sha256="ab" * 32), case_id=CID)
    payload = json.dumps({"object_type": "case", "object_id": CID, "details": {"draft_sha256": "ab" * 32}},
                         sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    link = {"seq": 0, "ts": "2026-09-16T05:30:00+00:00", "actor": "asha", "role": "analyst",
            "event_type": "draft_approved", "case_id": CID, "payload_digest": digest, "prev_hash": GENESIS_HASH}
    want = hashlib.sha256(json.dumps(link, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload_digest(e) == digest
    assert (e.seq, e.case_id, e.prev_hash, e.hash) == (0, CID, GENESIS_HASH, want)


def test_canonical_json_is_order_independent_and_keeps_unicode():
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": (1, 2), "b": 1}) == b'{"a":[1,2],"b":1}'
    assert canonical_json({"amount": "₹1,42,848"}).decode("utf-8") == '{"amount":"₹1,42,848"}'


def test_append_links_every_event_to_the_one_before():
    events = chain(5)
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]
    assert events[0].prev_hash == GENESIS_HASH
    assert all(events[i].prev_hash == events[i - 1].hash for i in range(1, 5))
    assert len({e.hash for e in events}) == 5
    report = verify_chain(events)
    assert report.ok and report.code == OK and report.length == 5 and report.head == events[-1].hash
    assert report.status == "intact" and "5 events verified" in report.summary()


def test_empty_log_is_intact():
    assert verify_chain([]).ok and verify_chain([], anchor=anchor_of([])).ok


def test_append_stamps_the_callers_event_as_its_receipt():
    events = chain(2)
    mine = ev(9)
    got = append_event(events, mine, case_id=CID)
    assert got is mine and events[-1] is mine and mine.seq == 2 and mine.prev_hash == events[1].hash


def test_hash_survives_a_json_round_trip_and_timezone_changes():
    events = chain(4)
    append_event(events, ev(4, when=T0, amounts=(43200.0, 27648.0)), case_id=CID)
    raw = json.dumps([e.model_dump(mode="json") for e in events])
    back = [AuditEvent.model_validate(d) for d in json.loads(raw)]
    assert verify_chain(back, case_id=CID).ok
    ist = [e.model_copy(update={"at": e.at.astimezone(IST)}) for e in events]  # same instant, other zone
    assert verify_chain(ist, case_id=CID).ok


# --------------------------------------------------------------------------- tamper detection


def _edit_details(ev_: list[AuditEvent]) -> None:
    ev_[3].details["n"] = 999


def _edit_actor(ev_: list[AuditEvent]) -> None:
    ev_[3].actor = "someone.else"


def _edit_time(ev_: list[AuditEvent]) -> None:
    ev_[3].at = ev_[3].at - timedelta(days=1)


def _edit_action(ev_: list[AuditEvent]) -> None:
    ev_[3].action = "draft_rejected"


def _edit_prev_hash(ev_: list[AuditEvent]) -> None:
    ev_[3].prev_hash = "0" * 64


@pytest.mark.parametrize("tamper", [_edit_details, _edit_actor, _edit_time, _edit_action, _edit_prev_hash])
def test_an_edited_event_is_detected_where_it_was_edited(tamper):
    events = chain(6)
    tamper(events)
    r = verify_chain(events)
    assert not r.ok and r.code == CONTENT and r.first_bad_seq == 3 and r.status == "broken"
    assert "changed after it was recorded" in r.reason and "#3" in r.summary()


def test_an_edit_with_its_hash_recomputed_breaks_the_next_link():
    events = chain(6)
    events[3].details["n"] = 999
    events[3].hash = link_hash(events[3], seq=3, prev_hash=events[3].prev_hash, case_id=CID)
    r = verify_chain(events)
    assert not r.ok and r.code == LINK and r.first_bad_seq == 4


def test_a_deleted_event_is_detected():
    events = chain(6)
    del events[2]
    r = verify_chain(events)
    assert not r.ok and r.code == SEQUENCE and r.first_bad_seq == 2 and "deleted" in r.reason


def test_a_deleted_first_event_is_detected():
    events = chain(4)
    del events[0]
    r = verify_chain(events, case_id=CID)
    assert not r.ok and r.first_bad_seq == 0


def test_reordered_events_are_detected():
    events = chain(6)
    events[2], events[3] = events[3], events[2]
    r = verify_chain(events)
    assert not r.ok and r.code == SEQUENCE and r.first_bad_seq == 2


def test_reordered_and_renumbered_events_are_detected():
    events = chain(6)
    events[2], events[3] = events[3], events[2]
    events[2].seq, events[3].seq = 2, 3
    r = verify_chain(events)
    assert not r.ok and r.code in (CONTENT, LINK) and r.first_bad_seq == 2


def test_an_inserted_forged_event_is_detected():
    events = chain(6)
    forged = append_event(copy(events[:3]), ev(50, action="draft_approved"), case_id=CID)
    events.insert(3, forged)
    r = verify_chain(events)
    assert not r.ok and r.code == SEQUENCE and r.first_bad_seq == 4


def test_an_event_moved_in_from_another_case_is_detected():
    events = chain(4)
    other = chain(4, case_id="CASE-OTHER")
    events[2] = other[2]
    r = verify_chain(events, case_id=CID)
    assert not r.ok and r.code == CASE and r.first_bad_seq == 2


def test_a_stripped_seal_is_detected():
    events = chain(5)
    events[1].hash = None
    r = verify_chain(events)
    assert not r.ok and r.code == UNSEALED and r.first_bad_seq == 1


def test_truncation_and_full_rehash_need_the_anchor():
    events = chain(6)
    anchor = anchor_of(events)
    assert verify_chain(events, anchor=anchor).anchored

    cut = copy(events)[:4]
    assert verify_chain(cut).ok  # the log alone cannot tell
    r = verify_chain(cut, anchor=anchor)
    assert not r.ok and r.code == TRUNCATED and r.first_bad_seq == 4

    forged = copy(events)
    forged[2].details["n"] = 999
    for i in range(2, 6):  # a careful forger re-hashes everything after the edit
        forged[i].prev_hash = forged[i - 1].hash
        forged[i].hash = link_hash(forged[i], seq=i, prev_hash=forged[i].prev_hash, case_id=CID)
    assert verify_chain(forged).ok  # the log alone cannot tell
    r = verify_chain(forged, anchor=anchor)
    assert not r.ok and r.code == HEAD


def test_an_old_anchor_still_verifies_a_log_that_grew():
    events = chain(3)
    anchor = anchor_of(events)
    for i in range(3, 6):
        append_event(events, ev(i), case_id=CID)
    assert verify_chain(events, anchor=anchor).ok


# --------------------------------------------------------------------------- legacy logs and merging


def test_a_log_without_any_seal_is_reported_as_unchained_not_broken():
    legacy = [ev(i) for i in range(3)]
    r = verify_chain(legacy, case_id=CID)
    assert not r.ok and r.code == UNCHAINED and r.status == "unchained" and r.summary().startswith("Not chained")
    append_event(legacy, ev(3), case_id=CID)  # the first chained append adopts the unsealed tail
    assert verify_chain(legacy, case_id=CID).ok


def test_only_the_unsealed_tail_is_adopted_never_an_insertion():
    events = chain(4)
    events.insert(2, ev(40))  # unsealed event slipped between sealed ones
    append_event(events, ev(5), case_id=CID)
    r = verify_chain(events)
    assert not r.ok and r.code == UNSEALED and r.first_bad_seq == 2


def test_chain_extend_reseals_new_events_without_touching_its_inputs():
    old, fresh = chain(3), chain(2, case_id=CID)
    before = [e.model_dump() for e in old + fresh]
    merged = chain_extend(old, fresh, case_id=CID)
    assert [e.model_dump() for e in old + fresh] == before
    assert len(merged) == 5 and verify_chain(merged, case_id=CID).ok
    assert [e.hash for e in merged[:3]] == [e.hash for e in old]


def test_extend_stored_is_append_only():
    stored = chain(4)
    head = [e.hash for e in stored]

    grown = extend_stored(stored, [*copy(stored), ev(10)], case_id=CID)
    assert [e.hash for e in grown[:4]] == head and len(grown) == 5 and verify_chain(grown, case_id=CID).ok

    shorter = extend_stored(stored, copy(stored)[:2], case_id=CID)  # a write cannot delete recorded events
    assert [e.hash for e in shorter] == head

    reordered = copy(stored)
    reordered[1], reordered[2] = reordered[2], reordered[1]
    assert [e.hash for e in extend_stored(stored, reordered, case_id=CID)] == head

    edited = copy(stored)
    edited[1].details["n"] = 999
    kept = extend_stored(stored, edited, case_id=CID)  # the recorded event stays; the altered copy is new
    assert [e.hash for e in kept[:4]] == head and kept[4].details["n"] == 999 and kept[4].seq == 4
    assert verify_chain(kept, case_id=CID).ok

    stale = extend_stored(stored, [*copy(stored)[:3], ev(11)], case_id=CID)  # stale writer + another's event
    assert [e.hash for e in stale[:4]] == head and stale[4].details == {"n": 11}
