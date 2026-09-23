import json
import warnings

import pytest

from wapsi.models import Citation, SearchHit
from wapsi.ports import DocSearch
from wapsi.search.chunk import DocChunk
from wapsi.search.index import LocalDocSearch

CHUNKS = [
    {
        "chunk_id": "sop-0001",
        "doc_id": "sop-2026",
        "section": "SOP Para 5(ix)",
        "section_key": "5(ix)",
        "page": 15,
        "citation_id": "SOP-5-IX",
        "citation_ids": ["SOP-5-IX"],
        "text": "Unwarranted orders for freezing accounts shall be discouraged, and accountability measures shall be "
        "established.",
    },
    {
        "chunk_id": "sop-0002",
        "doc_id": "sop-2026",
        "section": "SOP Para 9.1(i)",
        "section_key": "9.1(i)",
        "page": 26,
        "citation_id": "SOP-5-X",
        "citation_ids": ["SOP-5-X", "SOP-9-1"],
        "text": "This type of hold shall be placed only in the case of first-layer accounts. Such holds shall not be "
        "applied to nodal, pool and escrow accounts.",
    },
    {
        "chunk_id": "bnss-0001",
        "doc_id": "bnss-2023",
        "section": "BNSS Section 106",
        "section_key": "106",
        "page": 47,
        "citation_id": "BNSS-106",
        "citation_ids": ["BNSS-106"],
        "text": "Any police officer may seize any property which may be alleged or suspected to have been stolen.",
    },
    {
        "chunk_id": "misc-0001",
        "doc_id": "sop-2026",
        "section": "SOP Annexure-IV",
        "text": "Global best practices.",
    },
]

CITATIONS = {
    "SOP-5-IX": Citation(
        citation_id="SOP-5-IX",
        source_title="SOP",
        section="Para 5(ix)",
        quote="Unwarranted orders for freezing accounts shall be discouraged",
        url="https://example.gov/sop.pdf",
        page=15,
    ),
    "BNSS-106": {
        "citation_id": "BNSS-106",
        "source_title": "BNSS",
        "section": "Section 106",
        "quote": "Any police officer may seize any property",
        "url": "",
        "page": 47,
        "extra_field_is_ignored": True,
    },
}


@pytest.fixture
def search() -> LocalDocSearch:
    return LocalDocSearch.from_chunks(CHUNKS, CITATIONS)


def test_implements_docsearch_protocol(search):
    assert isinstance(search, DocSearch)
    assert len(search) == 4


def test_search_returns_search_hits_with_citation_ids(search):
    hits = search.search("first layer accounts nodal pool escrow", k=2)
    assert hits and all(isinstance(h, SearchHit) for h in hits)
    top = hits[0]
    assert (top.chunk_id, top.doc_id, top.citation_id, top.section) == (
        "sop-0002",
        "sop-2026",
        "SOP-5-X",
        "SOP Para 9.1(i)",
    )
    assert top.score > 0
    assert search.search("u/s 106 seize property", k=1)[0].citation_id == "BNSS-106"
    assert search.search("unwarranted freezing", k=5)[0].chunk_id == "sop-0001"


def test_search_k_and_no_match(search):
    assert search.search("cryptocurrency exchange wallet", k=5) == []
    assert search.search("accounts", k=0) == []
    assert len(search.search("accounts", k=1)) == 1
    hits = search.search("global best practices")
    assert hits[0].citation_id is None


def test_search_doc_filter(search):
    hits = search.search("seize property accounts", k=5, doc_ids=["bnss-2023"])
    assert {h.doc_id for h in hits} == {"bnss-2023"}


def test_citation_lookup(search):
    c = search.citation("SOP-5-IX")
    assert isinstance(c, Citation)
    assert c.quote.startswith("Unwarranted orders")
    assert c.page == 15
    c.quote = "mutated"
    assert search.citation("SOP-5-IX").quote.startswith("Unwarranted")  # returns copies
    assert search.citation("BNSS-106").section == "Section 106"
    assert search.citation("SOP-5-X") is None  # not provided to this instance
    assert search.citation("NOT-A-CLAUSE") is None


def test_helpers(search):
    assert search.chunk("bnss-0001").page == 47
    assert [c.chunk_id for c in search.chunks_for_citation("SOP-9-1")] == ["sop-0002"]
    assert search.citation_ids == ["BNSS-106", "SOP-5-IX"]
    hits = search.search_chunks("escrow", k=1)
    assert isinstance(hits[0][0], DocChunk) and hits[0][0].page == 26


def test_load_reads_jsonl_and_restricts_citations_to_known_ids(workdir):
    index = workdir / "index"
    index.mkdir()
    (index / "chunks.jsonl").write_text(
        "\n".join(DocChunk.model_validate(c).model_dump_json() for c in CHUNKS) + "\n", encoding="utf-8"
    )
    (workdir / "citations.json").write_text(
        json.dumps(
            [
                {"citation_id": "SOP-5-IX", "source_title": "T", "section": "S", "quote": "local quote", "url": "u",
                 "page": 15},
                {"citation_id": "INVENTED-1", "source_title": "T", "section": "S", "quote": "q", "url": "u",
                 "page": None},
            ]
        ),
        encoding="utf-8",
    )  # fmt: skip
    s = LocalDocSearch.load(index)
    assert not s.fallback and len(s) == 4
    assert s.citation("SOP-5-IX").quote == "local quote"
    assert s.citation("INVENTED-1") is None
    assert s.citation("BNSS-94") is not None  # registry default for known IDs
    assert LocalDocSearch.load(index / "chunks.jsonl").search("escrow")[0].chunk_id == "sop-0002"


def test_load_without_index_falls_back_to_citation_quotes(workdir):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = LocalDocSearch.load(workdir / "missing-index")
    assert s.fallback
    assert any("not found" in str(w.message) for w in caught)
    assert all(c.chunk_id.startswith("citation-") for c in s.chunks)
    hits = s.search("unwarranted orders freezing accounts", k=3)
    assert hits and hits[0].citation_id == "SOP-5-IX"
    with pytest.raises(FileNotFoundError):
        LocalDocSearch.load(workdir / "missing-index", allow_fallback=False)
