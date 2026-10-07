"""Smoke tests against the real index (skipped unless ``uv run python -m wapsi.search.build`` has been run)."""

import pytest

from wapsi.citations import KNOWN_IDS
from wapsi.search.index import LocalDocSearch
from wapsi.search.sources import CHUNKS_JSONL

pytestmark = pytest.mark.skipif(not CHUNKS_JSONL.exists(), reason="docs index not built")


@pytest.fixture(scope="module")
def search() -> LocalDocSearch:
    return LocalDocSearch.load()


def test_hold_disputed_amount_first_layer_hits_the_sop(search):
    hits = search.search("hold disputed amount first layer", k=3)
    assert any(h.doc_id == "sop-2026" for h in hits)
    assert any(h.citation_id in {"SOP-9-1-I", "SOP-DEF-FIRST-LAYER", "SOP-9-1"} for h in hits)


@pytest.mark.parametrize(
    ("query", "doc_id", "citation_ids"),
    [
        ("nodal pool escrow account hold", "sop-2026", {"SOP-9-1", "SOP-9-1-I", "SOP-9-1-III"}),
        ("s. 106 BNSS seize property", "bnss-2023", {"BNSS-106"}),
        ("STR seven working days suspicious transaction", "fiu-faq", {"PMLR-8"}),
        ("early warning signals red flagged accounts", "rbi-frm-dir-2026", {"RBI-FRM-DIR-2026-EWS"}),
        ("non-KYC compliant money mule accounts monitoring", "rbi-frm-dir-2026", {"RBI-FRM-DIR-2026"}),
        ("90 calendar days hold grievance", "sop-2026", {"SOP-10-1"}),
        ("informing the account holder is not a tip-off", "sop-2026", {"SOP-9-1-II", "FIU-CIRC-2025-06-02"}),
    ],
)
def test_topical_queries(search, query, doc_id, citation_ids):
    hits = search.search(query, k=3)
    assert any(h.doc_id == doc_id and h.citation_id in citation_ids for h in hits), [
        (h.doc_id, h.section, h.citation_id) for h in hits
    ]


def test_every_known_citation_resolves_with_metadata(search):
    for cid in KNOWN_IDS:
        c = search.citation(cid)
        assert c is not None and c.url, cid
        if not cid.startswith(("CASE-NEELKANTH", "CASE-KARTIK", "CASE-SHREE")):
            assert c.quote, cid
            assert search.chunks_for_citation(cid), cid


def test_chunks_are_bounded_and_paged(search):
    for c in search.chunks:
        assert len(c.text) <= 900
        if c.doc_id in {"sop-2026", "bnss-2023", "pmlr-2005"}:
            assert c.page is not None and c.page_end >= c.page
        else:
            assert c.page is None
