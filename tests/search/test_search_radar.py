"""Radar document fetch and quote verification (mock transport and in-memory documents; no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from wapsi.domain.regradar import load_radar
from wapsi.search import check_citations, radar
from wapsi.search.extract import ExtractedDoc, Page
from wapsi.search.sources import SOURCES

DRAFT_ID = "rbi-kyc-mule-draft-2026"
PDF = b"%PDF-1.7\n" + b"0" * 5000


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(radar.time, "sleep", lambda _s: None)


class _Docs:
    """DocCache stand-in: source id -> ExtractedDoc."""

    def __init__(self, docs: dict[str, ExtractedDoc]) -> None:
        self.docs = docs

    def get(self, source):
        if source.id not in self.docs:
            raise FileNotFoundError(source.id)
        return self.docs[source.id]


def _pdf(doc_id: str, *pages: str) -> ExtractedDoc:
    return ExtractedDoc(doc_id=doc_id, kind="pdf", pages=[Page(i + 1, t) for i, t in enumerate(pages)])


def _html(doc_id: str, text: str) -> ExtractedDoc:
    return ExtractedDoc(doc_id=doc_id, kind="html", pages=[Page(1, text)])


def _mini_registry(tmp_path, quote_new: str = "is 60 days from the date of temporary debit hold") -> tuple:
    """A two-source, one-row registry plus matching in-memory documents."""
    data = {
        "title": "t", "note": "n", "as_of": "2026-09-28", "baseline_source": "sop-2026",
        "sources": [
            {"id": "sop-2026", "short": "SOP", "status": "in-force", "corpus": True},
            {"id": DRAFT_ID, "short": "Draft", "status": "draft", "title": "Draft", "type": "pdf",
             "url": "https://rbidocs.example/draft.pdf", "fetch_headers": "browser",
             "evidence": [{"source_id": DRAFT_ID, "para": "3(2)", "page": 1, "quote": "come into effect from April 1, 2027"}]},
        ],
        "clauses": [{
            "id": "RR-X", "topic": "hold_duration", "title": "Max", "change": "stricter",
            "new": [{"source_id": DRAFT_ID, "para": "5", "page": 2, "quote": quote_new}],
            "baseline": [{"source_id": "sop-2026", "para": "10.1", "page": 3, "quote": "up to 90 additional calendar days"}],
            "applies_to": "a", "impact": "b", "suggested_action": "c",
            "check": {"kind": "hold_age_vs_max", "days": 60},
            "wapsi_status": "open",
            "review": {"curated_by": "test", "curated_on": "2026-09-28"},
            "verification": {"status": "verified"},
        }],
    }
    path = tmp_path / "reg_radar.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    docs = _Docs({
        DRAFT_ID: _pdf(DRAFT_ID, "shall come into effect from April 1, 2027, or earlier",
                       "Maximum duration ... is 60 days from the date of temporary debit hold (up to 30 days"),
        "sop-2026": _pdf("sop-2026", "p1", "p2", "they may ask for an extension for continuation for up to 90\n"
                                                 "additional calendar days."),
    })
    return path, docs


# --------------------------------------------------------------------------- registry wiring


def test_radar_only_sources_are_not_in_the_indexed_corpus():
    reg = load_radar()
    only = radar.radar_only_sources(reg)
    corpus = {s.id for s in SOURCES}
    assert DRAFT_ID in only and "mha-rs-1349-2026" in only and "rbi-frm-md-2024" in only
    assert not set(only) & corpus  # drafts and repealed texts are never indexed for STR / letter grounding
    assert {s.id for s in reg.sources if s.corpus} <= corpus
    src = only[DRAFT_ID]
    assert src.type == "pdf" and src.url.startswith("https://rbidocs.rbi.org.in/") and src.expect_text


def test_browser_headers_carry_the_sites_referer():
    headers = radar.browser_headers("https://rbidocs.rbi.org.in/rdocs/x.PDF")
    assert headers["Referer"] == "https://rbidocs.rbi.org.in/"
    assert headers["User-Agent"].startswith("Mozilla/5.0")


# --------------------------------------------------------------------------- verification


def test_verify_side_pdf_page_and_html_rules():
    reg = load_radar()
    sources = radar.radar_sources(reg)
    side = reg.clause("RR-02").new[0].model_copy()
    docs = _Docs({DRAFT_ID: _pdf(DRAFT_ID, "x", "y", "z", "w", "v",
                                 "Maximum duration of a Temporary Debit hold, in the absence of any contrary "
                                 "instruction from an LEA or Competent Authority is 60 days from the date of "
                                 "temporary debit hold (up to 30 days")})
    ok, detail = radar.verify_side(side, sources, docs)
    assert ok, detail
    assert not radar.verify_side(side.model_copy(update={"page": 2}), sources, docs)[0]
    assert "PDF quote without page" in radar.verify_side(side.model_copy(update={"page": None}), sources, docs)[1]
    ok, detail = radar.verify_side(side.model_copy(update={"quote": "is 45 days"}), sources, docs)
    assert not ok and "NOT FOUND" in detail
    html_side = reg.clause("RR-11").new[0].model_copy()
    html_docs = _Docs({"rbi-frm-dir-2026": _html("rbi-frm-dir-2026", "67. With the issue of these Directions, "
                       "the existing directions, instructions, and guidelines relating to Fraud Risk Management as "
                       "applicable to Commercial Banks stand repealed, as communicated vide circular")})
    assert radar.verify_side(html_side, sources, html_docs)[0]
    assert "page null" in radar.verify_side(html_side.model_copy(update={"page": 3}), sources, html_docs)[1]
    missing = radar.verify_side(side, sources, _Docs({}))
    assert not missing[0] and "radar fetch" in missing[1]
    assert "unknown source" in radar.verify_side(side.model_copy(update={"source_id": "zz"}), sources, docs)[1]


def test_check_rows_pass_then_fail_on_a_changed_quote(tmp_path):
    path, docs = _mini_registry(tmp_path)
    rows = radar.check_rows(docs, path)
    assert rows and all(r.ok for r in rows), [(r.item, r.detail) for r in rows if not r.ok]
    assert rows[-1].item == "radar:registry" and "1 rows, 2 sources" in rows[-1].detail

    path, docs = _mini_registry(tmp_path, quote_new="is 60 days from the date of the LEA's reference")
    bad = [r for r in radar.check_rows(docs, path) if not r.ok]
    items = {r.item for r in bad}
    assert "radar:RR-X:new[0]" in items and "radar:RR-X" in items  # marked verified but no longer matches


def test_check_rows_reports_an_invalid_registry(tmp_path):
    path, docs = _mini_registry(tmp_path, quote_new="is 45 days from the date of temporary debit hold")
    bad = [r for r in radar.check_rows(docs, path) if not r.ok]
    assert any(r.item == "radar:registry" and "check.days=60" in r.detail for r in bad)
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    rows = radar.check_rows(docs, broken)
    assert len(rows) == 1 and not rows[0].ok and "cannot load" in rows[0].detail
    assert radar.check_rows(docs, tmp_path / "absent.json") == []


def test_write_verification_stamps_only_the_verification_field(tmp_path):
    path, docs = _mini_registry(tmp_path, quote_new="is 45 days")
    before = json.loads(path.read_text(encoding="utf-8"))
    per_clause, _ = radar.verify(load_radar(path), docs)
    from datetime import date

    assert radar.write_verification(path, per_clause, date(2026, 9, 28)) == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    row = after["clauses"][0]
    assert row["verification"]["status"] == "unverified" and row["verification"]["checked_on"] == "2026-09-28"
    assert any("NOT FOUND" in d for d in row["verification"]["detail"])
    for key in ("new", "baseline", "check", "review", "impact"):
        assert row[key] == before["clauses"][0][key]
    assert load_radar(path).visible() == []  # an unverified row drops out of the UI by default


def test_check_citations_runs_the_radar_rows(tmp_path):
    path, docs = _mini_registry(tmp_path)
    cites = tmp_path / "citations.json"
    cites.write_text("[]", encoding="utf-8")
    rows = check_citations.run(cites, tmp_path / "nofacts.json", tmp_path, radar_path=path)
    assert any(r.item.startswith("radar:") for r in rows)  # radar rows are part of the citation check


def test_a_fact_may_cite_a_radar_only_source():
    fact = {"fact_id": "F", "doc_id": "mha-rs-1349-2026", "pdf_page": 1, "printed_page": None,
            "quote": "financial amount of more than Rs. 8,189 Crore has been saved"}
    docs = _Docs({"mha-rs-1349-2026": _pdf("mha-rs-1349-2026", "Till 31.12.2025, financial amount of more than "
                                           "Rs. 8,189 Crore\nhas been saved in more than 23.61 lakh complaints")})
    row = check_citations.check_fact(fact, docs)
    assert row.ok, row.detail
    assert not check_citations.check_fact(dict(fact, doc_id="nowhere"), docs).ok


# --------------------------------------------------------------------------- fetch


def test_fetch_uses_browser_headers_only_where_asked(tmp_path):
    reg = load_radar()
    seen: dict[str, str] = {}
    html = (b"<!DOCTYPE html><html><body>Money Mules Date : Sep 11, 2026 Circulars Withdrawn by the Department of "
            b"Supervision hereby repealed Early Warning Signals" + b" " * 3000 + b"</body></html>")

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers["user-agent"]
        if request.url.path.lower().endswith(".pdf"):
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    log_path = tmp_path / "_fetch.json"
    failures = radar.fetch(reg, [DRAFT_ID, "rbi-c2r-505"], raw_dir=tmp_path, log_path=log_path,
                           transport=httpx.MockTransport(handler))
    assert failures == 0
    assert seen["rbidocs.rbi.org.in"].startswith("Mozilla/5.0")  # browser headers for the bot-challenged host
    assert seen["www.rbi.org.in"].startswith("WapsiDocsFetcher/")  # polite identifying UA elsewhere
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log[DRAFT_ID]["status"] == "downloaded" and log[DRAFT_ID]["fetch_headers"] == "browser"
    assert log["rbi-c2r-505"]["fetch_headers"] == "polite"
    assert (tmp_path / f"{DRAFT_ID}.pdf").read_bytes() == PDF


def test_fetch_keeps_the_last_good_copy_on_failure(tmp_path):
    reg = load_radar()
    log_path = tmp_path / "_fetch.json"
    log_path.write_text(json.dumps({DRAFT_ID: {"status": "downloaded", "filename": "gone.pdf"}}), encoding="utf-8")
    challenge = b"<!DOCTYPE html><html><script>bobcmn</script>" + b" " * 3000 + b"</html>"
    failures = radar.fetch(reg, [DRAFT_ID], raw_dir=tmp_path, log_path=log_path, force=True,
                           transport=httpx.MockTransport(lambda r: httpx.Response(200, content=challenge)))
    assert failures == 1
    entry = json.loads(log_path.read_text(encoding="utf-8"))[DRAFT_ID]
    assert entry["status"] == "downloaded" and entry["last_failure"]["status"] == "failed"


def test_manifest_lists_radar_sources_without_indexing_them():
    from wapsi.search.manifest import render_manifest

    only = [s for s in load_radar().sources if not s.corpus]
    log = {DRAFT_ID: {"url": "https://rbidocs.example/d.pdf", "bytes": 10, "sha256": "abc", "fetch_headers": "browser",
                      "retrieved_at": "2026-09-28T00:00:00+00:00"}}
    md = render_manifest(log, {}, only)
    assert "## Regulatory radar sources (fetched and quote-checked, not indexed)" in md
    assert f"| `{DRAFT_ID}` | draft |" in md and "(browser headers)" in md and "`abc`" in md
    assert "| `mha-rs-1349-2026` | reference |" in md and "not fetched" in md
    assert "Regulatory radar sources" not in render_manifest(log, {})  # corpus-only manifest unchanged
