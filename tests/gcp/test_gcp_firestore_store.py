from __future__ import annotations

import os
from datetime import timedelta

import pytest
from gcp_fakes import FakeFirestore

from wapsi.adapters.firestore_store import (
    CHUNK_BYTES,
    ENCODING,
    FirestoreCaseStore,
    decode_bundle,
    encode_bundle,
)
from wapsi.adapters.llm_fake import FakeLLM
from wapsi.fixtures import showcase as sc
from wapsi.models import AuditEvent, CaseStatus, Draft, DraftKind
from wapsi.pipeline.run import process_case
from wapsi.ports import CaseStore
from wapsi.timeutil import utcnow


@pytest.fixture(scope="module")
def bundles():
    ledger = sc.showcase_ledger()
    return [process_case(ledger, exp["ncrp_ids"], llm=FakeLLM()) for exp in (sc.SHOWCASE_1, sc.SHOWCASE_2)]


def test_implements_protocol():
    assert isinstance(FirestoreCaseStore(client=FakeFirestore()), CaseStore)


def test_round_trip_is_lossless(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs)
    for b in bundles:
        store.put_bundle(b)
    for b in bundles:
        got = store.get_bundle(b.case.case_id)
        assert got == b
        assert got.model_dump(mode="json") == b.model_dump(mode="json")
    assert store.get_bundle("CASE-00000000000000") is None
    header = fs.docs[f"cases/{bundles[0].case.case_id}"]
    assert header["encoding"] == ENCODING and header["status"] == bundles[0].case.status.value
    assert header["chunk_count"] >= 1 and len(header["sha256"]) == 64
    assert "trace" not in header  # the bundle lives in chunks, the header stays small


def test_chunking_keeps_documents_small(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs, chunk_bytes=1024)
    b = bundles[0]
    store.put_bundle(b)
    cid = b.case.case_id
    header = fs.docs[f"cases/{cid}"]
    assert header["chunk_count"] > 1
    chunks = [p for p in fs.docs if p.startswith(f"cases/{cid}/chunks/")]
    assert len(chunks) == header["chunk_count"]
    assert all(len(fs.docs[p]["data"]) <= 1024 for p in chunks)
    assert store.get_bundle(cid) == b


def test_bundle_larger_than_one_mib(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs)
    b = bundles[0].model_copy(deep=True)
    noise = os.urandom(1_500_000).hex()  # incompressible, ~3 MiB of text
    b.drafts.append(Draft(draft_id="DRF-BIG", case_id=b.case.case_id, kind=DraftKind.STR, title="big",
                          body=noise, created_at=utcnow()))
    store.put_bundle(b)
    header = fs.docs[f"cases/{b.case.case_id}"]
    assert header["chunk_count"] >= 3
    assert fs.max_doc_bytes_seen < 1_048_576
    assert store.get_bundle(b.case.case_id) == b


def test_reput_replaces_generation_and_deletes_old_chunks(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs, chunk_bytes=2048)
    b = bundles[0].model_copy(deep=True)
    store.put_bundle(b)
    cid = b.case.case_id
    gen1 = fs.docs[f"cases/{cid}"]["generation"]
    b.case.status = CaseStatus.APPROVED
    store.put_bundle(b)
    header = fs.docs[f"cases/{cid}"]
    assert header["generation"] != gen1 and header["status"] == "APPROVED"
    chunks = [p for p in fs.docs if p.startswith(f"cases/{cid}/chunks/")]
    assert chunks and all(p.split("/")[-1].startswith(header["generation"]) for p in chunks)
    assert store.get_bundle(cid).case.status == CaseStatus.APPROVED


def test_audit_append_uses_subcollection_and_is_idempotent(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs)
    b = bundles[1]
    store.put_bundle(b)
    cid = b.case.case_id
    n = len(b.audit)
    assert len([p for p in fs.docs if p.startswith(f"cases/{cid}/audit/")]) == n
    later = max(e.at for e in b.audit) + timedelta(seconds=5)
    ev = AuditEvent(at=later, actor="ravi", role="supervisor", action="viewed", object_type="case",
                    object_id=cid, details={"page": "hold_plan"})
    store.append_audit(cid, ev)
    got = store.get_bundle(cid)
    assert len(got.audit) == n + 1 and got.audit[-1] == ev and got.audit[:n] == b.audit
    store.put_bundle(got)  # re-put with the appended event: no duplicates
    assert len(store.get_bundle(cid).audit) == n + 1
    with pytest.raises(KeyError):
        store.append_audit("CASE-00000000000000", ev)


def test_list_cases_sorted_newest_first(bundles):
    store = FirestoreCaseStore(client=FakeFirestore())
    older = bundles[0].model_copy(deep=True)
    older.case.opened_at = older.case.opened_at - timedelta(days=1)
    store.put_bundle(older)
    store.put_bundle(bundles[1])
    assert [c.case_id for c in store.list_cases()] == [bundles[1].case.case_id, older.case.case_id]


def test_corruption_is_detected(bundles):
    fs = FakeFirestore()
    store = FirestoreCaseStore(client=fs)
    b = bundles[0]
    store.put_bundle(b)
    cid = b.case.case_id
    gen = fs.docs[f"cases/{cid}"]["generation"]
    del fs.docs[f"cases/{cid}/chunks/{gen}-0000"]
    with pytest.raises(ValueError, match="missing"):
        store.get_bundle(cid)
    chunks, sha = encode_bundle(b)
    with pytest.raises(ValueError, match="checksum"):
        decode_bundle(chunks, "0" * 64)
    assert decode_bundle(chunks, sha)["case"]["case_id"] == cid


def test_validation_and_lazy_client(monkeypatch):
    with pytest.raises(ValueError):
        FirestoreCaseStore(client=FakeFirestore(), collection="a/b")
    with pytest.raises(ValueError):
        FirestoreCaseStore(client=FakeFirestore(), chunk_bytes=10 * 1024 * 1024)
    store = FirestoreCaseStore(client=FakeFirestore())
    with pytest.raises(ValueError):
        store.get_bundle("../../etc")
    assert CHUNK_BYTES < 1_048_576

    from google.cloud import firestore

    seen = {}

    class _Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(firestore, "Client", _Client)
    lazy = FirestoreCaseStore(project="wapsi-test", database="wapsi")
    assert not seen
    assert isinstance(lazy.client, _Client) and seen == {"project": "wapsi-test", "database": "wapsi"}
