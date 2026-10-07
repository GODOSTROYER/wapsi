"""``FirestoreCaseStore``: ``wapsi.ports.CaseStore`` on Cloud Firestore (GCP edition).

Layout (one case = one header document, two subcollections and one anchor document outside them)::

    cases/{case_id}                      header: case fields (queryable: status, opened_at, priority_amount,
                                         assigned_to), counts, generation, chunk_count, sha256
    cases/{case_id}/chunks/{gen}-{nnnn}  the CaseBundle (minus audit) as zlib-compressed JSON, split into
                                         ≤ 512 KiB pieces so no document gets near Firestore's 1 MiB limit
    cases/{case_id}/audit/{seq:010d}     one document per AuditEvent, sealed into the case's hash chain
                                         (``wapsi.audit``: seq / prev_hash / hash); create-only, never updated
                                         or deleted; the document id is the event's position in the log
    audit_anchors/{case_id}              the chain anchor (event count + head hash), kept outside the log

The audit log is sealed exactly like ``InMemoryCaseStore`` does it:

- ``append_audit`` runs one Firestore transaction: it reads the case's head (length, head hash) from the anchor,
  seals the event as the next link (``seq = length``, ``prev_hash = head``), creates
  ``audit/{seq}`` and advances the anchor, all in one atomic commit. Two writers that read the same head
  conflict on the anchor; Firestore aborts one and ``google.cloud.firestore.transactional`` re-runs it on the new
  head, so concurrent appends serialise into one gap-free chain. The caller's event is stamped with its seal (its
  receipt, as in the in-memory store); re-sending the same stamped event after an error is recognised (its seq
  and hash are already recorded) and never recorded twice.
- ``put_bundle`` keeps recorded events and seals only the bundle's new ones onto the head
  (``wapsi.audit.chain.extend_stored``), in the same transaction that flips the header to the new generation.
- Reads (``get_bundle``, ``verify_audit``) return the log in seq order (document id order). A document that no
  longer parses as an ``AuditEvent`` (a field added, removed or retyped in the console) is read as an unsealed
  ``audit_record_unreadable`` placeholder at its position, so the case still loads and the chain reads as broken
  there instead of raising.
- ``verify_audit`` checks the chain against the anchor: an edited, deleted, inserted, swapped or unreadable event,
  events removed from the end, a re-hashed log, a deleted anchor and events written without advancing the anchor
  are all reported. New events are always placed at the anchor's head, never after whatever documents are left,
  so a truncation or deletion stays visible after later writes; if a document already sits at the head (written
  without advancing the anchor) the store refuses to write (``AuditChainError``).

Server client libraries bypass Firestore security rules; ``editions/gcp/deploy/firestore.rules`` and the edition
README describe the append-only rules and the IAM / external-anchor measures for production.

``put_bundle`` writes the new generation's chunks first, then in one transaction seals the new audit events,
advances the anchor and flips the header to the new generation, and finally deletes the replaced generation's
chunks. Readers only follow the generation named in the header, so a concurrent reader never mixes chunks from two
writes.

All Google imports are lazy; any client object with Firestore's ``collection().document()`` / ``transaction()``
surface works (``tests/gcp/gcp_fakes.FakeFirestore`` is an offline one).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import ValidationError

from wapsi.audit.chain import (
    ANCHOR,
    CONTENT,
    GENESIS_HASH,
    ChainAnchor,
    ChainReport,
    extend_stored,
    is_sealed,
    seal,
    verify_chain,
)
from wapsi.models import AuditEvent, Case, CaseBundle
from wapsi.timeutil import ensure_utc, utcnow

CHUNK_BYTES = 512 * 1024  # well below Firestore's 1 MiB document / 1,048,487-byte field limits
MAX_BATCH_OPS = 450  # Firestore allows 500 writes per batch / transaction; keep headroom
ENCODING = "zlib+json/v1"
CHAIN_ID = "wapsi/audit-chain/v1"
SEQ_WIDTH = 10  # audit/{seq:010d}: document id order == seq order
DEFAULT_ANCHOR_COLLECTION = "audit_anchors"
MAX_ATTEMPTS = 5  # transaction attempts (google.cloud.firestore's default)
VERIFY_ATTEMPTS = 3  # re-reads when an append lands while verify_audit is reading
UNREADABLE_ACTION = "audit_record_unreadable"  # placeholder for a stored audit document that no longer parses
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

T = TypeVar("T")


class AuditChainError(RuntimeError):
    """The case's audit log and its anchor disagree in a way a write would hide (the anchor is missing while the
    log holds events, or a document already sits at the anchor's head). Nothing was written; ``verify_audit``
    reports the finding."""


def audit_doc_id(seq: int) -> str:
    """Document id of the event at position ``seq`` (zero-padded, so id order is seq order)."""
    if seq < 0:
        raise ValueError(f"invalid audit seq {seq}")
    return f"{seq:0{SEQ_WIDTH}d}"


def encode_bundle(bundle: CaseBundle, chunk_bytes: int = CHUNK_BYTES) -> tuple[list[bytes], str]:
    """(compressed chunks, sha256 of the uncompressed JSON). Audit events are stored separately."""
    body = bundle.model_dump(mode="json", exclude={"audit"})
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = zlib.compress(raw, 6)
    chunks = [blob[i : i + chunk_bytes] for i in range(0, len(blob), chunk_bytes)] or [b""]
    return chunks, hashlib.sha256(raw).hexdigest()


def decode_bundle(chunks: list[bytes], sha256: str | None = None) -> dict[str, Any]:
    raw = zlib.decompress(b"".join(chunks))
    if sha256 and hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("case bundle checksum mismatch (partial or mixed write)")
    return json.loads(raw.decode("utf-8"))


def _valid_name(name: str, what: str) -> str:
    if not name or "/" in name:
        raise ValueError(f"invalid {what}: {name!r}")
    return name


class _Batcher:
    """Commits Firestore writes in batches of at most ``MAX_BATCH_OPS`` operations."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._batch = client.batch()
        self._ops = 0

    def set(self, ref: Any, data: dict[str, Any]) -> None:
        self._batch.set(ref, data)
        self._bump()

    def delete(self, ref: Any) -> None:
        self._batch.delete(ref)
        self._bump()

    def _bump(self) -> None:
        self._ops += 1
        if self._ops >= MAX_BATCH_OPS:
            self.commit()

    def commit(self) -> None:
        if self._ops:
            self._batch.commit()
        self._batch = self._client.batch()
        self._ops = 0


class FirestoreCaseStore:
    """Implements ``wapsi.ports.CaseStore``; the audit log of every case is hash-chained and anchored."""

    def __init__(
        self,
        client: Any | None = None,
        collection: str = "cases",
        *,
        project: str | None = None,
        database: str | None = None,
        chunk_bytes: int = CHUNK_BYTES,
        anchor_collection: str | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        _valid_name(collection, "collection name")
        if not 1024 <= chunk_bytes <= 900 * 1024:
            raise ValueError("chunk_bytes must be between 1 KiB and 900 KiB")
        if anchor_collection is None:  # two stores in one database never share anchors
            anchor_collection = (DEFAULT_ANCHOR_COLLECTION if collection == "cases"
                                 else f"{collection}_{DEFAULT_ANCHOR_COLLECTION}")
        _valid_name(anchor_collection, "anchor collection name")
        if anchor_collection == collection:
            raise ValueError("the audit anchors must live outside the case collection")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self.collection_name = collection
        self.anchor_collection = anchor_collection
        self.project = project
        self.database = database
        self.chunk_bytes = chunk_bytes
        self.max_attempts = max_attempts

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import firestore  # lazy

            kwargs: dict[str, Any] = {}
            if self.project:
                kwargs["project"] = self.project
            if self.database:
                kwargs["database"] = self.database
            self._client = firestore.Client(**kwargs)
        return self._client

    def _doc(self, case_id: str) -> Any:
        return self.client.collection(self.collection_name).document(_valid_name(case_id, "case id"))

    def _anchor_ref(self, case_id: str) -> Any:
        return self.client.collection(self.anchor_collection).document(_valid_name(case_id, "case id"))

    def _transact(self, case_id: str, fn: Callable[[Any], T]) -> T:
        """Run ``fn(transaction)`` in a Firestore transaction; contention (ABORTED) re-runs it from scratch.

        The only ``create`` writes are audit documents at the anchor's head, so ALREADY_EXISTS means a document was
        written there without advancing the anchor: refuse (``AuditChainError``) rather than surface a raw 409."""
        from google.api_core.exceptions import AlreadyExists  # lazy
        from google.cloud.firestore import transactional

        try:
            return transactional(fn)(self.client.transaction(max_attempts=self.max_attempts))
        except AlreadyExists as exc:
            raise AuditChainError(
                f"case {case_id}: an audit document already sits at the head recorded by "
                f"{self.anchor_collection}/{case_id} (written without advancing the anchor); refusing to write "
                "(verify_audit reports the log)") from exc

    # ------------------------------------------------------------------ CaseStore protocol
    def put_bundle(self, bundle: CaseBundle) -> None:
        case_id = bundle.case.case_id
        doc = self._doc(case_id)
        generation = uuid.uuid4().hex[:12]
        chunks, sha = encode_bundle(bundle, self.chunk_bytes)

        # 1. the new generation's chunks; invisible until the header names their generation
        batch = _Batcher(self.client)
        chunk_col = doc.collection("chunks")
        for i, data in enumerate(chunks):
            batch.set(chunk_col.document(f"{generation}-{i:04d}"),
                      {"generation": generation, "index": i, "data": data})
        batch.commit()

        # 2. seal the bundle's new audit events onto the chain, advance the anchor and flip the header: one commit
        #    (or a few, for a bundle with more new events than one transaction may write)
        header = self._header(bundle, generation, len(chunks), sha)
        audit_col = doc.collection("audit")
        anchor_ref = self._anchor_ref(case_id)
        incoming = list(bundle.audit)
        replaced: dict[str, Any] = {}

        def write(txn: Any) -> int:
            old = doc.get(transaction=txn)
            stored = self._read_events(audit_col, txn)
            head = self._head(case_id, self._read_anchor(anchor_ref, txn), bool(stored))
            new = extend_stored(stored, incoming, case_id=case_id)[len(stored):]
            room = MAX_BATCH_OPS - 2  # the anchor and the header
            now, rest = new[:room], new[room:]
            seq, prev = head.length, head.head
            for event in now:
                seal(event, seq=seq, prev_hash=prev, case_id=case_id)
                txn.create(audit_col.document(audit_doc_id(seq)), self._audit_doc(event))
                seq, prev = seq + 1, event.hash or GENESIS_HASH
            if now:
                txn.set(anchor_ref, self._anchor_doc(case_id, seq, prev))
            if not rest:
                txn.set(doc, header)
                replaced["header"] = old.to_dict() if getattr(old, "exists", False) else None
            return len(rest)

        try:
            while self._transact(case_id, write):
                pass
        except BaseException:
            self._drop_orphan_chunks(doc, generation, len(chunks))
            raise

        # 3. drop the generation the header no longer names
        old_header = replaced.get("header")
        if old_header and old_header.get("generation") and old_header["generation"] != generation:
            prev_gen = old_header["generation"]
            for i in range(int(old_header.get("chunk_count") or 0)):
                batch.delete(chunk_col.document(f"{prev_gen}-{i:04d}"))
            batch.commit()

    def _drop_orphan_chunks(self, doc: Any, generation: str, count: int) -> None:
        """After a failed put, delete the chunks it wrote, unless the header names them after all (a commit whose
        response was lost). Best effort: an orphaned chunk is garbage, never read."""
        try:
            snap = doc.get()
            if getattr(snap, "exists", False) and (snap.to_dict() or {}).get("generation") == generation:
                return
            batch = _Batcher(self.client)
            for i in range(count):
                batch.delete(doc.collection("chunks").document(f"{generation}-{i:04d}"))
            batch.commit()
        except Exception:  # noqa: BLE001 - never mask the original error
            pass

    def get_bundle(self, case_id: str) -> CaseBundle | None:
        doc = self._doc(case_id)
        snap = doc.get()
        if not getattr(snap, "exists", False):
            return None
        header = snap.to_dict() or {}
        if header.get("encoding") != ENCODING:
            raise ValueError(f"unsupported case encoding {header.get('encoding')!r} for {case_id}")
        generation = header["generation"]
        chunk_col = doc.collection("chunks")
        chunks: list[bytes] = []
        for i in range(int(header["chunk_count"])):
            part = chunk_col.document(f"{generation}-{i:04d}").get()
            if not getattr(part, "exists", False):
                raise ValueError(f"case {case_id}: chunk {i} of generation {generation} is missing")
            chunks.append(bytes((part.to_dict() or {})["data"]))
        body = decode_bundle(chunks, header.get("sha256"))
        body["audit"] = [e.model_dump(mode="json") for e in self._read_events(doc.collection("audit"))]
        return CaseBundle.model_validate(body)

    def list_cases(self) -> list[Case]:
        out: list[Case] = []
        for snap in self.client.collection(self.collection_name).stream():
            data = snap.to_dict() or {}
            case = data.get("case")
            if isinstance(case, dict):
                out.append(Case.model_validate(case))
        return sorted(out, key=lambda c: ensure_utc(c.opened_at), reverse=True)

    def append_audit(self, case_id: str, event: AuditEvent) -> None:
        """Seal ``event`` as the next link of the case's chain (one transaction). The event is stamped in place
        with its seal (``seq``, ``case_id``, ``prev_hash``, ``hash``): the caller's receipt and retry token."""
        doc = self._doc(case_id)
        if not getattr(doc.get(), "exists", False):
            raise KeyError(case_id)
        audit_col = doc.collection("audit")
        anchor_ref = self._anchor_ref(case_id)

        def append(txn: Any) -> bool:
            anchor = self._read_anchor(anchor_ref, txn)
            if anchor is None:  # the case's first event, unless the anchor went missing
                head = self._head(case_id, None, bool(list(audit_col.stream(transaction=txn))))
            else:
                head = anchor
            if (is_sealed(event) and event.case_id == case_id and event.seq is not None
                    and 0 <= event.seq < head.length):
                # a retry of an append that already committed: the store holds this exact link
                snap = audit_col.document(audit_doc_id(event.seq)).get(transaction=txn)
                stored = (snap.to_dict() or {}).get("event") if getattr(snap, "exists", False) else None
                if isinstance(stored, dict) and stored.get("hash") == event.hash:
                    return False
            seal(event, seq=head.length, prev_hash=head.head, case_id=case_id)
            txn.create(audit_col.document(audit_doc_id(head.length)), self._audit_doc(event))
            txn.set(anchor_ref, self._anchor_doc(case_id, head.length + 1, event.hash or GENESIS_HASH))
            return True

        self._transact(case_id, append)

    # ------------------------------------------------------------------ audit chain (not part of the protocol)
    def audit_anchor(self, case_id: str) -> ChainAnchor | None:
        """The case's anchor (event count + head hash), read from ``audit_anchors/{case_id}``."""
        return self._read_anchor(self._anchor_ref(case_id))

    def verify_audit(self, case_id: str) -> ChainReport:
        """Verify the stored audit chain of ``case_id`` against its anchor (``KeyError`` for an unknown case)."""
        doc = self._doc(case_id)
        if not getattr(doc.get(), "exists", False):
            raise KeyError(case_id)
        audit_col = doc.collection("audit")
        anchor_ref = self._anchor_ref(case_id)
        # read anchor, log, anchor: equal anchors mean no append landed in between (every append moves it)
        anchor = self._read_anchor(anchor_ref)
        for _ in range(VERIFY_ATTEMPTS):
            rows = self._read_rows(audit_col)
            after = self._read_anchor(anchor_ref)
            if after == anchor:
                break
            anchor = after
        events: list[AuditEvent] = []
        unreadable: tuple[int, str, str] | None = None
        for i, (doc_id, data) in enumerate(rows):
            event, problem = self._parse_row(doc_id, data)
            events.append(event)
            if problem and unreadable is None:
                unreadable = (i, doc_id, problem)
        if unreadable is not None:
            return self._unreadable(case_id, events, anchor, *unreadable)
        return self._verify(case_id, events, anchor)

    @staticmethod
    def _unreadable(case_id: str, events: list[AuditEvent], anchor: ChainAnchor | None, k: int, doc_id: str,
                    problem: str) -> ChainReport:
        """The first unreadable document breaks the chain at its position (unless an earlier event already does)."""
        before = verify_chain(events[:k], case_id=case_id)
        if not before.ok:
            return dataclasses.replace(before, length=len(events), anchored=anchor is not None)
        return ChainReport(ok=False, first_bad_seq=k, code=CONTENT, length=len(events), head=before.head,
                           anchored=anchor is not None,
                           reason=f"event #{k} (document audit/{doc_id}) can no longer be read as an audit event "
                                  f"({problem}): its stored record was altered")

    @staticmethod
    def _verify(case_id: str, events: list[AuditEvent], anchor: ChainAnchor | None) -> ChainReport:
        report = verify_chain(events, case_id=case_id, anchor=anchor)
        n = len(events)
        if not report.ok:  # includes UNCHAINED: a log no store sealed
            return report
        if anchor is None and n:
            return ChainReport(ok=False, first_bad_seq=n, code=ANCHOR, length=n, head=report.head,
                               reason=f"the anchor of this {n}-event log is missing: events removed from the end or "
                                      "a re-hashed log can no longer be ruled out")
        if anchor is not None and n > anchor.length:
            return ChainReport(ok=False, first_bad_seq=anchor.length, code=ANCHOR, length=n, head=report.head,
                               anchored=True,
                               reason=f"event #{anchor.length} and later were written without advancing the anchor "
                                      f"(it records {anchor.length} events, the log holds {n})")
        return report

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _read_anchor(ref: Any, txn: Any = None) -> ChainAnchor | None:
        snap = ref.get(transaction=txn)
        if not getattr(snap, "exists", False):
            return None
        data = snap.to_dict() or {}
        return ChainAnchor(length=int(data.get("length", 0)), head=str(data.get("head") or GENESIS_HASH))

    def _head(self, case_id: str, anchor: ChainAnchor | None, has_events: bool) -> ChainAnchor:
        """Where the next event goes: the anchor's head (never after whatever documents are left, so a truncated
        or punched log stays visibly broken), or the genesis hash for a case with no events yet."""
        if anchor is not None:
            return anchor
        if has_events:
            raise AuditChainError(f"case {case_id}: the audit log holds events but its anchor "
                                  f"{self.anchor_collection}/{case_id} is missing; refusing to write "
                                  "(verify_audit reports the log)")
        return ChainAnchor(length=0, head=GENESIS_HASH)

    @staticmethod
    def _read_rows(audit_col: Any, txn: Any = None) -> list[tuple[str, dict[str, Any]]]:
        """Every stored audit document as (id, data), in id order (id = zero-padded seq = storage position)."""
        rows = [(snap.id, snap.to_dict() or {}) for snap in audit_col.stream(transaction=txn)]
        rows.sort(key=lambda r: r[0])
        return rows

    @staticmethod
    def _parse_row(doc_id: str, data: dict[str, Any]) -> tuple[AuditEvent, str | None]:
        """(event, None), or (an unsealed placeholder, what is wrong) for a document that no longer parses. The
        placeholder is deterministic (same document, same placeholder), so a caller's copy of the log that carries
        it is matched by ``extend_stored`` instead of being recorded again; it is never written back as is."""
        raw = data.get("event")
        try:
            if not isinstance(raw, dict):
                raise TypeError("the 'event' map is missing or not a map")
            return AuditEvent.model_validate(raw), None
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in err.get("loc", ())) or "event" for err in exc.errors()})
            problem = f"invalid field(s): {', '.join(fields)}"
        except (TypeError, ValueError) as exc:
            problem = str(exc)
        problem = problem[:200]  # field names come from the stored document: keep the reason short
        at = data.get("at")
        placeholder = AuditEvent(
            at=ensure_utc(at) if isinstance(at, datetime) else _EPOCH, actor="wapsi.case_store", role="system",
            action=UNREADABLE_ACTION, object_type="audit_record", object_id=f"audit/{doc_id}",
            details={"problem": problem})
        return placeholder, problem

    @classmethod
    def _read_events(cls, audit_col: Any, txn: Any = None) -> list[AuditEvent]:
        """The audit log in seq order; an unreadable document becomes a placeholder at its position."""
        return [cls._parse_row(doc_id, data)[0] for doc_id, data in cls._read_rows(audit_col, txn)]

    def _anchor_doc(self, case_id: str, length: int, head: str) -> dict[str, Any]:
        return {"case_id": case_id, "collection": self.collection_name, "length": length, "head": head,
                "chain": CHAIN_ID, "updated_at": utcnow()}

    @staticmethod
    def _audit_doc(event: AuditEvent) -> dict[str, Any]:
        """``event`` is the record verified; the top-level fields are copies for the console and queries."""
        return {
            "seq": event.seq,
            "at": ensure_utc(event.at),
            "action": event.action,
            "actor": event.actor,
            "prev_hash": event.prev_hash,
            "hash": event.hash,
            "event": event.model_dump(mode="json"),
        }

    @staticmethod
    def _header(bundle: CaseBundle, generation: str, chunk_count: int, sha: str) -> dict[str, Any]:
        c = bundle.case
        return {
            "case": c.model_dump(mode="json"),
            # denormalised, queryable fields (Firestore indexes top-level scalars)
            "status": c.status.value,
            "opened_at": ensure_utc(c.opened_at),
            "priority_amount": float(c.priority_amount),
            "assigned_to": c.assigned_to,
            "ncrp_ids": list(c.ncrp_ids),
            "drafts": len(bundle.drafts),
            "approvals": len(bundle.approvals),
            "encoding": ENCODING,
            "generation": generation,
            "chunk_count": chunk_count,
            "sha256": sha,
            "updated_at": utcnow(),
        }
