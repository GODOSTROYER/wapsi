"""Hash-chained, append-only audit log: every case's audit events form a tamper-evident chain.

Each event is one link::

    payload_digest = SHA-256( canonical_json({object_type, object_id, details}) )
    hash           = SHA-256( canonical_json({seq, ts, actor, role, event_type, case_id, payload_digest,
                                              prev_hash}) )

``seq`` is the event's position in the case's log (0, 1, 2, ...), ``ts`` its time in UTC (ISO 8601),
``event_type`` its ``action`` and ``prev_hash`` the previous event's ``hash`` (``GENESIS_HASH`` for the first
one). Canonical JSON = sorted keys, no whitespace, UTF-8.

What ``verify_chain`` detects on its own: an edited event (its hash no longer matches its content), a deleted,
inserted or moved event (sequence gap or broken ``prev_hash`` link) and an event that was never sealed. What it
cannot detect on its own: events removed from the *end* of the log, or a log whose every hash was recomputed
after an edit. Both are caught against a ``ChainAnchor`` (event count + head hash) kept outside the log: stores
keep one per case (``InMemoryCaseStore.audit_anchor``, ``FirestoreCaseStore.audit_anchor`` in
``audit_anchors/{case_id}``); in production the anchor goes to a separate trust domain (WORM storage, a signed
receipt to the supervisor, a daily digest to the CISO).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_core import to_jsonable_python

from wapsi.models import AuditEvent
from wapsi.timeutil import ensure_utc

log = logging.getLogger("wapsi.audit.chain")

GENESIS_HASH = hashlib.sha256(b"wapsi/audit-chain/genesis/v1").hexdigest()

# ChainReport.code values
OK = "OK"
UNCHAINED = "UNCHAINED"  # no event carries a seal: the log was written by a store that does not chain yet
UNSEALED = "UNSEALED"  # an event carries no seal (never chained, or its seal was stripped)
SEQUENCE = "SEQUENCE"  # an event sits at the wrong position: one was deleted, inserted or moved
LINK = "LINK"  # prev_hash does not point at the event before it: that event was replaced or re-hashed
CONTENT = "CONTENT"  # the event's content no longer matches its hash: it was edited after it was recorded
CASE = "CASE"  # the event was sealed into another case's log
TRUNCATED = "TRUNCATED"  # fewer events than the anchor recorded: events were removed from the end
HEAD = "HEAD"  # the chain does not reach the anchored head hash: the log was rewritten and re-hashed
ANCHOR = "ANCHOR"  # a store's own anchor is missing, or events were written without advancing it (store-level check)


@dataclass(frozen=True)
class ChainAnchor:
    """A checkpoint of a log, kept outside it: event count and the hash of the last event."""

    length: int
    head: str


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    first_bad_seq: int | None = None  # position of the first event that fails (None when ok)
    reason: str = ""
    code: str = OK
    length: int = 0  # events checked
    head: str = GENESIS_HASH  # hash of the last event that verified
    anchored: bool = False  # the check also covered an anchor (truncation / re-hash detectable)

    @property
    def status(self) -> str:
        """'intact' | 'broken' | 'unchained' (a legacy log with no seals at all: nothing to verify)."""
        return "intact" if self.ok else ("unchained" if self.code == UNCHAINED else "broken")

    def summary(self) -> str:
        if self.ok:
            tail = " and matches its anchor" if self.anchored else ""
            return f"Chain intact: {self.length} event{'s' if self.length != 1 else ''} verified{tail}."
        if self.code == UNCHAINED:
            return f"Not chained: {self.reason}"
        return f"Chain broken at event #{self.first_bad_seq}: {self.reason}"


# --------------------------------------------------------------------------- hashing


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. Values are made JSON-native first (datetimes -> ISO strings, tuples ->
    lists), the same way pydantic serialises them, so a digest survives a JSON round trip through any store."""
    native = to_jsonable_python(obj, fallback=str)
    return json.dumps(native, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def payload_digest(event: AuditEvent) -> str:
    """SHA-256 of what the event is about: object type, object id and details."""
    return sha256_hex({"object_type": event.object_type, "object_id": event.object_id, "details": event.details})


def _ts(event: AuditEvent) -> str:
    return ensure_utc(event.at).isoformat()


def link_hash(event: AuditEvent, *, seq: int, prev_hash: str, case_id: str | None) -> str:
    """The hash ``event`` gets at position ``seq`` after ``prev_hash`` in ``case_id``'s log."""
    return sha256_hex({
        "seq": seq,
        "ts": _ts(event),
        "actor": event.actor,
        "role": event.role,
        "event_type": event.action,
        "case_id": case_id,
        "payload_digest": payload_digest(event),
        "prev_hash": prev_hash,
    })


def expected_hash(event: AuditEvent) -> str | None:
    """Recompute a sealed event's hash from its content and seal (None if it carries no seal)."""
    if event.seq is None or event.prev_hash is None:
        return None
    return link_hash(event, seq=event.seq, prev_hash=event.prev_hash, case_id=event.case_id)


def content_key(event: AuditEvent) -> tuple[str, str, str, str, str]:
    """Everything an event says, without its seal (two events with equal keys record the same thing)."""
    return (_ts(event), event.actor, event.role, event.action, payload_digest(event))


# --------------------------------------------------------------------------- writing


def is_sealed(event: AuditEvent) -> bool:
    return event.hash is not None and event.seq is not None and event.prev_hash is not None


def seal(event: AuditEvent, *, seq: int, prev_hash: str, case_id: str | None) -> AuditEvent:
    """Stamp ``event`` in place as link ``seq`` after ``prev_hash`` and return it."""
    event.seq = seq
    event.case_id = case_id
    event.prev_hash = prev_hash
    event.hash = link_hash(event, seq=seq, prev_hash=prev_hash, case_id=case_id)
    return event


def _adopt_tail(events: list[AuditEvent], case_id: str | None) -> None:
    """Seal events at the end of the log that were appended without a seal (by code that bypassed
    ``append_event``). Only the unsealed *tail* is adopted: an unsealed event between sealed ones stays
    unsealed, so ``verify_chain`` still reports an insertion."""
    start = len(events)
    while start > 0 and not is_sealed(events[start - 1]):
        start -= 1
    for i in range(start, len(events)):
        seal(events[i], seq=i, prev_hash=events[i - 1].hash if i else GENESIS_HASH, case_id=case_id)


def append_event(events: list[AuditEvent], event: AuditEvent, *, case_id: str | None) -> AuditEvent:
    """Append ``event`` to ``events`` as the next link of the chain. The event object itself is stamped
    (``seq``, ``case_id``, ``prev_hash``, ``hash``) and appended; the stamp doubles as the caller's receipt."""
    _adopt_tail(events, case_id)
    prev = events[-1].hash if events else GENESIS_HASH
    events.append(seal(event, seq=len(events), prev_hash=prev or GENESIS_HASH, case_id=case_id))
    return event


def chain_extend(events: Sequence[AuditEvent], new_events: Sequence[AuditEvent], *,
                 case_id: str | None) -> list[AuditEvent]:
    """A new log: ``events`` followed by copies of ``new_events`` re-sealed onto it (e.g. a pipeline re-run's
    events appended to the case's history). Neither input is modified."""
    out = [e.model_copy(deep=True) for e in events]
    for e in new_events:
        append_event(out, e.model_copy(deep=True), case_id=case_id)
    return out


def extend_stored(stored: Sequence[AuditEvent], incoming: Sequence[AuditEvent], *,
                  case_id: str | None) -> list[AuditEvent]:
    """The append-only rule for a store's ``put_bundle``: recorded events are never dropped, changed or
    reordered. The result is the stored log, unchanged, followed by copies of the incoming events it does not
    already hold (matched by content, so a caller's copy of the log still matches even if it was re-sealed
    elsewhere), sealed onto the stored chain in incoming order.

    A write from a stale copy (missing events another writer appended) keeps those events and adds its own after
    them. A write that leaves out or alters recorded events cannot remove or change them: the recorded events
    stay, and an altered copy is recorded as a new, chained event where the verifier and the officer can see it.
    """
    remaining = Counter(content_key(e) for e in stored)
    new: list[AuditEvent] = []
    for e in incoming:
        key = content_key(e)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            new.append(e)
    missing = sum(remaining.values())
    if missing:
        log.warning("audit log of %s is append-only: a write left out %d recorded event(s); they are kept",
                    case_id, missing)
    return chain_extend(stored, new, case_id=case_id)


def anchor_of(events: Sequence[AuditEvent]) -> ChainAnchor:
    return ChainAnchor(length=len(events), head=(events[-1].hash or GENESIS_HASH) if events else GENESIS_HASH)


# --------------------------------------------------------------------------- verifying

_UNSET: Any = object()


def verify_chain(events: Sequence[AuditEvent], *, case_id: str | None = _UNSET,
                 anchor: ChainAnchor | None = None) -> ChainReport:
    """Walk the log from the genesis hash and report the first event that breaks the chain.

    ``case_id``: the case the log belongs to (default: the first event's). ``anchor``: a checkpoint kept outside
    the log; with it, events removed from the end and a fully re-hashed log are detected too.
    """
    expected_case = case_id if case_id is not _UNSET else (events[0].case_id if events else None)
    prev = GENESIS_HASH
    n = len(events)

    def bad(i: int, code: str, reason: str) -> ChainReport:
        return ChainReport(ok=False, first_bad_seq=i, reason=reason, code=code, length=n, head=prev,
                           anchored=anchor is not None)

    if events and not any(is_sealed(e) for e in events):
        return bad(0, UNCHAINED, f"none of the {n} events carries a seal; this log was written by a store that "
                                 "does not chain events yet")
    for i, e in enumerate(events):
        what = f"{e.action} by {e.actor}"
        if not is_sealed(e):
            return bad(i, UNSEALED, f"event #{i} ({what}) carries no seal; it was never chained or its seal was "
                                    "removed")
        if e.seq != i:
            return bad(i, SEQUENCE, f"position #{i} holds event #{e.seq} ({what}): an event was deleted, "
                                    "inserted or moved")
        if e.case_id != expected_case:
            return bad(i, CASE, f"event #{i} ({what}) was sealed into case {e.case_id}, not {expected_case}")
        if expected_hash(e) != e.hash:
            return bad(i, CONTENT, f"event #{i} ({what}) was changed after it was recorded: its content no longer "
                                   "matches its hash")
        if e.prev_hash != prev:
            return bad(i, LINK, f"event #{i} ({what}) does not link to event #{i - 1}: that event was replaced "
                                "or re-hashed" if i else f"event #0 ({what}) does not start from the genesis hash")
        prev = e.hash  # type: ignore[assignment]

    if anchor is not None:
        if n < anchor.length:
            return bad(n, TRUNCATED, f"the log holds {n} events but {anchor.length} were recorded: events were "
                                     "removed from the end")
        at = events[anchor.length - 1].hash if anchor.length else GENESIS_HASH
        if at != anchor.head:
            return bad(max(anchor.length - 1, 0), HEAD, "the chain does not reach the anchored head hash: the "
                                                        "log was rewritten and re-hashed")
    return ChainReport(ok=True, length=n, head=prev, anchored=anchor is not None)
