"""``FirestoreCaseStore``: ``wapsi.ports.CaseStore`` on Cloud Firestore (GCP edition).

Layout (one case = one header document plus two subcollections)::

    cases/{case_id}                      header: case fields (queryable: status, opened_at, priority_amount,
                                         assigned_to), counts, generation, chunk_count, sha256
    cases/{case_id}/chunks/{gen}-{nnnn}  the CaseBundle (minus audit) as zlib-compressed JSON, split into
                                         ≤ 512 KiB pieces so no document gets near Firestore's 1 MiB limit
    cases/{case_id}/audit/{event_id}     one document per AuditEvent (append-only; never deleted)

``put_bundle`` writes the new generation's chunks and audit events, then flips the header to the new generation
in the same atomic batch, and finally deletes the previous generation's chunks. Readers only follow the
generation named in the header, so a concurrent reader never mixes chunks from two writes. Audit event ids are
content hashes, so re-putting a bundle is idempotent and ``append_audit`` never rewrites the bundle.

All Google imports are lazy; any client object with Firestore's ``collection().document()`` surface works.

TODO(audit chain): events sealed by the pipeline (``wapsi.audit``: ``seq`` / ``prev_hash`` / ``hash``) are stored
as they are, but this store does not seal on its own yet: ``append_audit`` should read the head event and write
the next link in a Firestore transaction (``wapsi.audit.chain.append_event``), ``put_bundle`` should keep
recorded events and seal only new ones (``extend_stored``), reads should order by ``event.seq``, and the chain
anchor (count + head hash) should live outside ``cases/{case_id}/audit``. Until then verify with
``wapsi.audit.verify_chain`` on the bundle read back.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from collections.abc import Iterator
from typing import Any

from wapsi.models import AuditEvent, Case, CaseBundle
from wapsi.timeutil import ensure_utc, utcnow

CHUNK_BYTES = 512 * 1024  # well below Firestore's 1 MiB document / 1,048,487-byte field limits
MAX_BATCH_OPS = 450  # Firestore allows 500 writes per batch; keep headroom
ENCODING = "zlib+json/v1"


def _event_id(event: AuditEvent) -> str:
    raw = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
    """Implements ``wapsi.ports.CaseStore``."""

    def __init__(
        self,
        client: Any | None = None,
        collection: str = "cases",
        *,
        project: str | None = None,
        database: str | None = None,
        chunk_bytes: int = CHUNK_BYTES,
    ) -> None:
        if not collection or "/" in collection:
            raise ValueError(f"invalid collection name: {collection!r}")
        if not 1024 <= chunk_bytes <= 900 * 1024:
            raise ValueError("chunk_bytes must be between 1 KiB and 900 KiB")
        self._client = client
        self.collection_name = collection
        self.project = project
        self.database = database
        self.chunk_bytes = chunk_bytes

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
        if not case_id or "/" in case_id:
            raise ValueError(f"invalid case id: {case_id!r}")
        return self.client.collection(self.collection_name).document(case_id)

    # ------------------------------------------------------------------ CaseStore protocol
    def put_bundle(self, bundle: CaseBundle) -> None:
        doc = self._doc(bundle.case.case_id)
        old = doc.get()
        old_header = old.to_dict() if getattr(old, "exists", False) else None
        generation = uuid.uuid4().hex[:12]
        chunks, sha = encode_bundle(bundle, self.chunk_bytes)

        batch = _Batcher(self.client)
        chunk_col = doc.collection("chunks")
        for i, data in enumerate(chunks):
            batch.set(chunk_col.document(f"{generation}-{i:04d}"),
                      {"generation": generation, "index": i, "data": data})
        audit_col = doc.collection("audit")
        for seq, event in enumerate(bundle.audit):
            batch.set(audit_col.document(_event_id(event)), self._audit_doc(event, seq))
        header = self._header(bundle, generation, len(chunks), sha)
        batch.set(doc, header)
        batch.commit()

        if old_header and old_header.get("generation") and old_header["generation"] != generation:
            prev = old_header["generation"]
            for i in range(int(old_header.get("chunk_count") or 0)):
                batch.delete(chunk_col.document(f"{prev}-{i:04d}"))
            batch.commit()

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
        body["audit"] = [e.model_dump(mode="json") for e in self._audit_events(doc)]
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
        doc = self._doc(case_id)
        if not getattr(doc.get(), "exists", False):
            raise KeyError(case_id)
        doc.collection("audit").document(_event_id(event)).set(self._audit_doc(event, None))

    # ------------------------------------------------------------------ helpers
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

    @staticmethod
    def _audit_doc(event: AuditEvent, seq: int | None) -> dict[str, Any]:
        return {
            "at": ensure_utc(event.at),
            "seq": seq if seq is not None else 1_000_000_000,
            "event": event.model_dump(mode="json"),
        }

    @staticmethod
    def _audit_events(doc: Any) -> Iterator[AuditEvent]:
        rows = []
        for snap in doc.collection("audit").stream():
            data = snap.to_dict() or {}
            if "event" in data:
                rows.append((ensure_utc(data["at"]), int(data.get("seq", 0)), snap.id, data["event"]))
        # bundle order first (seq = list position at put time), then appended events by time
        for _, _, _, event in sorted(rows, key=lambda r: (r[1], r[0], r[2])):
            yield AuditEvent.model_validate(event)
