"""Committed corpus data (citations.json, facts.json, sources.json) and the quote verifier."""

import json

import pytest

from wapsi.citations import KNOWN_IDS, RBI_FRM_DIR_URL, SOP_URL, registry
from wapsi.models import Citation
from wapsi.search import check_citations
from wapsi.search.extract import ExtractedDoc, Page
from wapsi.search.normalize import match_quote, normalize, quote_word_count
from wapsi.search.sources import (
    CASE_LAW_URLS,
    CITATIONS_JSON,
    DOCS_DIR,
    FACTS_JSON,
    SOURCES,
    SOURCES_JSON,
    load_sources_json,
    source_for_url,
    sources_by_id,
    sources_payload,
)

NEWS_REPORTED = {"CASE-NEELKANTH", "CASE-KARTIK-CHATUR", "CASE-SHREE-BALAJI"}


@pytest.fixture(scope="module")
def citations() -> list[dict]:
    return json.loads(CITATIONS_JSON.read_text(encoding="utf-8"))


def test_citations_cover_every_known_id_exactly_once(citations):
    ids = [c["citation_id"] for c in citations]
    assert sorted(ids) == sorted(KNOWN_IDS)
    assert len(ids) == len(set(ids))


def test_citation_entries_have_exactly_the_citation_fields(citations):
    for c in citations:
        assert set(c) == set(Citation.model_fields), c["citation_id"]
        Citation.model_validate(c)
        assert c["source_title"] and c["section"] and c["url"], c["citation_id"]


def test_quotes_are_at_most_25_words(citations):
    for c in citations:
        assert quote_word_count(c["quote"]) <= 25, c["citation_id"]
        assert len(c["quote"].split()) <= 25, c["citation_id"]


def test_quotes_present_and_sourced_from_fetched_documents(citations):
    for c in citations:
        cid = c["citation_id"]
        if cid in NEWS_REPORTED:
            assert c["quote"] == "" and c["page"] is None, cid
            assert c["url"] == CASE_LAW_URLS[cid]
            assert source_for_url(c["url"]) is None
            continue
        assert c["quote"], cid
        src = source_for_url(c["url"])
        assert src is not None, cid
        assert cid in src.citation_ids, cid
        if src.type == "pdf":
            assert isinstance(c["page"], int) and c["page"] > 0, cid
        else:
            assert c["page"] is None, cid


def test_registry_merges_committed_quotes():
    reg = registry()
    assert reg["SOP-ANNEX-I"].quote.startswith("Hold the amount equivalent to the Disputed amount")
    assert reg["SOP-5-IX"].page == 15
    assert reg["CASE-NEELKANTH"].quote == ""


def test_key_sop_phrases_are_quoted(citations):
    by_id = {c["citation_id"]: c for c in citations}
    assert "Unwarranted orders for freezing accounts shall be discouraged" in by_id["SOP-5-IX"]["quote"]
    assert "without the knowledge or connivance or consent" in by_id["SOP-5-XII"]["quote"]
    assert "pro-rata distribution" in by_id["SOP-5-XIII"]["quote"]
    assert "at any layer" in by_id["SOP-5-X"]["quote"]
    assert "first-layer accounts" in by_id["SOP-9-1-I"]["quote"]
    assert "nodal, pool and escrow accounts" in by_id["SOP-9-1-I"]["quote"]
    assert "shall hold the disputed amount" in by_id["SOP-9-1-III"]["quote"]
    assert "tip-off" in by_id["SOP-9-1-II"]["quote"]
    assert "90 calendar days" in by_id["SOP-10-1"]["quote"]


def test_rbi_frm_directions_2026_replace_the_repealed_master_directions(citations):
    by_id = {c["citation_id"]: c for c in citations}
    assert "RBI-FRM-MD-2024" not in by_id and "RBI-FRM-MD-2024" not in KNOWN_IDS  # repealed by para 67
    mule = by_id["RBI-FRM-DIR-2026"]
    assert mule["quote"] == (
        "The bank shall remain vigilant in monitoring transactions / unusual activities, specifically in the "
        "non-KYC compliant and money mule accounts"
    )
    assert mule["section"].startswith("Para 24:")
    assert mule["url"] == RBI_FRM_DIR_URL and mule["page"] is None
    ews = by_id["RBI-FRM-DIR-2026-EWS"]
    assert ews["quote"].startswith(
        "The bank shall have a framework for EWS and Red Flagging of Accounts (RFA)"
    )
    assert ews["section"].startswith("Para 12:")
    assert source_for_url(RBI_FRM_DIR_URL).id == "rbi-frm-dir-2026"
    assert registry()["RBI-FRM-DIR-2026"].quote == mule["quote"]


def test_facts_file_shape():
    facts = json.loads(FACTS_JSON.read_text(encoding="utf-8"))
    by_id = {f["fact_id"]: f for f in facts["facts"]}
    assert by_id["CFCFRMS_AMOUNT_SAVED"]["value"] == 7647
    assert by_id["CFCFRMS_AMOUNT_REPORTED"]["value"] == 52969
    assert by_id["CFCFRMS_AMOUNT_RESTORED"]["value"] == 167
    assert by_id["CFCFRMS_RESTORED_SHARE_OF_SAVED"]["value"] == 2.18
    assert round(167 / 7647 * 100, 2) == 2.18
    from wapsi.search.radar import radar_only_sources  # facts may cite a verified radar-only source

    known_docs = set(sources_by_id()) | set(radar_only_sources())
    for f in facts["facts"]:
        assert f["doc_id"] in known_docs, f["fact_id"]
        assert quote_word_count(f["quote"]) <= 25, f["fact_id"]
        if f["doc_id"] == "sop-2026":
            assert f["printed_page"] == f["pdf_page"] - 2, f["fact_id"]


def test_sources_json_in_sync_with_sources_py():
    assert load_sources_json() == SOURCES
    assert json.loads(SOURCES_JSON.read_text(encoding="utf-8")) == json.loads(json.dumps(sources_payload()))
    ids = [s.id for s in SOURCES]
    assert ids == ["sop-2026", "bnss-2023", "pmlr-2005", "fiu-faq", "rbi-frm-dir-2026"]
    assert sources_by_id()["sop-2026"].url == SOP_URL
    covered = {cid for s in SOURCES for cid in s.citation_ids} | set(CASE_LAW_URLS)
    assert covered == set(KNOWN_IDS)


def test_raw_and_index_are_gitignored():
    lines = (DOCS_DIR / ".gitignore").read_text(encoding="utf-8").split()
    assert "raw/" in lines and "index/" in lines


# --------------------------------------------------------------------------- normalisation / matching


def test_match_quote_normalises_whitespace_typography_and_pdf_splits():
    text = "the bank\n will not   be expected. Such holds shall not be applied to nodal, pool and escrow accounts"
    assert match_quote("will not be expected", text) == "exact"
    assert match_quote("LEA's address", "the LEA’s  address") == "exact"
    assert match_quote("dated 2nd June 2025", "circular dated 2 nd June \n2025") == "despaced"
    assert match_quote("de-frauded money", "receives the de-\nfrauded money") == "despaced"
    assert match_quote("not expected", text) is None
    assert normalize("a—b “c”") == 'a-b "c"'


def test_match_quote_segments_in_order_and_close():
    text = "Alpha beta gamma. " + "filler " * 20 + "Delta epsilon."
    assert match_quote("Alpha beta … Delta epsilon.", text) == "exact"
    assert match_quote("Delta epsilon. … Alpha beta", text) is None  # order matters
    far = "Alpha beta " + "x" * 700 + " Delta epsilon"
    assert match_quote("Alpha beta … Delta epsilon", far) is None  # too far apart
    assert match_quote("", text) is None


def test_quote_word_count_is_strict():
    assert quote_word_count("first-layer accounts") == 3
    assert quote_word_count("Rs. 50,000 held") == 4
    assert quote_word_count("alpha … beta") == 2
    assert quote_word_count("the LEA’s address") == 3


# --------------------------------------------------------------------------- verifier (no raw files needed)


class _FakeDocs:
    def __init__(self, doc: ExtractedDoc) -> None:
        self.doc = doc

    def get(self, source):
        return self.doc


def _entry(**kw):
    base = {
        "citation_id": "SOP-5-IX",
        "source_title": "SOP",
        "section": "Para 5(ix)",
        "quote": "Unwarranted orders for freezing accounts shall be discouraged",
        "url": SOP_URL,
        "page": 2,
    }
    base.update(kw)
    return base


SOP_DOC = ExtractedDoc(
    doc_id="sop-2026",
    kind="pdf",
    pages=[
        Page(1, "Guiding principles"),
        Page(2, "ix) Officers of LEAs. Unwarranted orders for\nfreezing accounts shall be discouraged, and"),
        Page(3, "accountability measures shall be established."),
    ],
)


def test_verifier_accepts_verbatim_quote_on_stated_page():
    row = check_citations.check_citation(_entry(), _FakeDocs(SOP_DOC))
    assert row.ok, row.detail
    row = check_citations.check_citation(
        _entry(quote="shall be discouraged, and accountability measures shall be established."),
        _FakeDocs(SOP_DOC),
    )
    assert row.ok, row.detail  # crosses the page break into page 3


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"page": 1}, "found on pages [2]"),
        ({"quote": "Warranted orders for freezing all accounts"}, "NOT FOUND"),
        ({"page": None}, "PDF quote without page"),
        ({"quote": " ".join(["word"] * 26)}, "26 words"),
        ({"citation_id": "SOP-99"}, "unknown citation_id"),
        ({"url": ""}, "missing url"),
    ],
)
def test_verifier_rejects_bad_entries(kw, message):
    row = check_citations.check_citation(_entry(**kw), _FakeDocs(SOP_DOC))
    assert not row.ok
    assert message in row.detail


def test_verifier_rules_for_news_reported_judgments_and_fields():
    news = _entry(citation_id="CASE-NEELKANTH", url=CASE_LAW_URLS["CASE-NEELKANTH"], page=None)
    row = check_citations.check_citation(news, _FakeDocs(SOP_DOC))
    assert not row.ok and "must not store quote text" in row.detail
    row = check_citations.check_citation(dict(news, quote=""), _FakeDocs(SOP_DOC))
    assert row.ok
    extra = dict(_entry(), note="x")
    assert "differ from Citation" in check_citations.check_citation(extra, _FakeDocs(SOP_DOC)).detail


def test_verifier_html_sources_have_no_page():
    doc = ExtractedDoc(doc_id="fiu-faq", kind="html", pages=[Page(1, "not later than seven working days on")])
    faq_url = sources_by_id()["fiu-faq"].url
    entry = _entry(citation_id="PMLR-8", quote="not later than seven working days", url=faq_url, page=None)
    assert check_citations.check_citation(entry, _FakeDocs(doc)).ok
    assert not check_citations.check_citation(dict(entry, page=3), _FakeDocs(doc)).ok


def test_verifier_reports_missing_file(workdir):
    rows = check_citations.run(workdir / "nope.json", workdir / "nofacts.json", workdir)
    assert len(rows) == 1 and not rows[0].ok
