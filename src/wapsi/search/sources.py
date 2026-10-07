"""Registry of the public legal / regulatory documents behind Wapsi's citations.

``SOURCES`` is the source of truth; ``data/docs/sources.json`` is a committed copy for humans and other
editions (regenerate with ``uv run python -m wapsi.search.sources``; a test keeps the two in sync).

Raw documents and extracted text are never committed (see ``data/docs/.gitignore``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wapsi.citations import FIU_FAQ_URL, RBI_FRM_DIR_URL, SOP_URL

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = Path(os.environ.get("WAPSI_DOCS_DIR", REPO_ROOT / "data" / "docs"))
RAW_DIR = DOCS_DIR / "raw"
INDEX_DIR = DOCS_DIR / "index"
SOURCES_JSON = DOCS_DIR / "sources.json"
CITATIONS_JSON = DOCS_DIR / "citations.json"
FACTS_JSON = DOCS_DIR / "facts.json"
MANIFEST_MD = DOCS_DIR / "MANIFEST.md"
FETCH_LOG = RAW_DIR / "_fetch.json"
CHUNKS_JSONL = INDEX_DIR / "chunks.jsonl"
BUILD_REPORT = INDEX_DIR / "build_report.json"

Profile = Literal["sop", "bnss", "pmlr", "rbi_dir", "faq"]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    publisher: str
    type: Literal["pdf", "html"]
    url: str
    mirrors: list[str] = Field(default_factory=list)
    licence: str
    profile: Profile  # which section detector to use (wapsi.search.sections)
    citation_ids: list[str] = Field(default_factory=list)  # citations whose quote comes from this document
    # --- extraction hints
    expect_text: str = ""  # must appear in a downloaded HTML page (guards against bot-challenge pages)
    start_marker: str = ""  # HTML: drop everything before the first occurrence
    end_marker: str = ""  # HTML: drop everything from the first occurrence after start_marker
    drop_regex: list[str] = Field(default_factory=list)  # removed from extracted text (multiline, dotall)
    skip_pages: list[int] = Field(default_factory=list)  # PDF pages kept out of the index (1-based)
    front_matter: str = "Front matter"  # section label before the first detected heading
    headings_from_page: int = 1  # PDF: body headings are detected only from this page on
    index_sections: list[str] = Field(default_factory=list)  # if set, only these top-level keys are indexed
    expect_sections: list[str] = Field(default_factory=list)  # build warns if a key is not detected
    section_citations: dict[str, str] = Field(default_factory=dict)  # section key (or parent key) -> id
    notes: str = ""

    def all_urls(self) -> list[str]:
        return [self.url, *self.mirrors]


_SOP_SECTIONS = {
    "5(ix)": "SOP-5-IX",
    "5(x)": "SOP-5-X",
    "5(xii)": "SOP-5-XII",
    "5(xiii)": "SOP-5-XIII",
    "12(x)": "SOP-DEF-FIRST-LAYER",
    "6": "SOP-6",
    "9.1": "SOP-9-1",
    "9.1(i)": "SOP-9-1-I",
    "9.1(ii)": "SOP-9-1-II",
    "9.1(iii)": "SOP-9-1-III",
    "9.2": "SOP-9-2",
    "9.3": "SOP-9-3",
    "9.5": "SOP-9-5",
    "10.1": "SOP-10-1",
    "11 Process 1": "SOP-11-P1",
    "11 Process 2": "SOP-11-P2",
    "11 Process 3": "SOP-11-P3",
    "11 Process 4": "SOP-11-P4",
    "11 Process 5": "SOP-11-P5",
    "Annexure-I": "SOP-ANNEX-I",
    "Annexure-V": "SOP-ANNEX-V",
}

BNSS_PDF_URL = (
    "https://indiacode.gov.in/server/api/core/bitstreams/73f93470-5f6a-4f0f-bc23-1309e32b9554/content"
)
PMLR_PDF_URL = "https://fiuindia.gov.in/pdfs/AML_legislation/PMLA_2005.pdf"

# Chapter III of the RBI FRM Directions, 2026: paras 12-22 (EWS / RFA governance and credit accounts) and
# 23-25 (EWS for other banking / non-credit transactions, incl. money mule accounts in para 24).
_RBI_FRM_DIR_SECTIONS = {
    **{str(n): "RBI-FRM-DIR-2026-EWS" for n in range(12, 23)},
    **{str(n): "RBI-FRM-DIR-2026" for n in range(23, 26)},
}

SOURCES: list[Source] = [
    Source(
        id="sop-2026",
        title=(
            "MHA/I4C Standard Operating Procedure (SOP) for NCRP-CFCFRMS, Custody, Restoration of money and "
            "Grievance Redressal, 02.01.2026 (annexed to High Court of Uttarakhand C.L. No. 21/I.T./XXXIV/NCL/"
            "2025-26 dated 18.06.2026)"
        ),
        publisher="Ministry of Home Affairs / I4C (circulated by the High Court of Uttarakhand)",
        type="pdf",
        url=SOP_URL,
        mirrors=["https://highcourt.cg.gov.in/other/other_noti/2026/endt.8686_05052026.pdf"],
        licence=(
            "Government of India publication circulated by a High Court. No open licence stated; the PDF is "
            "not redistributed here. Only short verbatim quotes (<= 25 words) are committed for citation."
        ),
        profile="sop",
        citation_ids=[
            "SOP-5-IX",
            "SOP-5-X",
            "SOP-5-XII",
            "SOP-5-XIII",
            "SOP-DEF-FIRST-LAYER",
            "SOP-6",
            "SOP-9-1",
            "SOP-9-1-I",
            "SOP-9-1-II",
            "SOP-9-1-III",
            "SOP-9-2",
            "SOP-9-3",
            "SOP-9-5",
            "SOP-10-1",
            "SOP-11-P1",
            "SOP-11-P2",
            "SOP-11-P3",
            "SOP-11-P4",
            "SOP-11-P5",
            "SOP-ANNEX-I",
            "SOP-ANNEX-V",
            "FIU-CIRC-2025-06-02",
            "CASE-SAJEER",
            "CASE-SAIFULLAH",
        ],
        skip_pages=[3, 4, 5],  # table of contents (headings there would be mistaken for body headings)
        front_matter="Uttarakhand HC circular / cover",
        headings_from_page=9,  # pages 1-8: HC circular (its own numbered list), cover, abbreviations
        expect_sections=[
            "2",
            "5(ix)",
            "5(x)",
            "5(xii)",
            "5(xiii)",
            "6.1",
            "9.1(i)",
            "9.1(ii)",
            "9.1(iii)",
            "9.2",
            "9.3",
            "9.5",
            "10.1",
            "11 Process 1",
            "11 Process 2",
            "11 Process 3",
            "11 Process 4",
            "11 Process 5",
            "12(x)",
            "Annexure-I",
            "Annexure-III",
            "Annexure-V",
        ],
        section_citations=_SOP_SECTIONS,
        notes=(
            "Page numbers in citations/facts are physical PDF pages; the SOP's own 'Page N of 98' label is "
            "PDF page - 2. The mirror (Chhattisgarh HC endorsement) currently fails TLS verification "
            "(incomplete certificate chain); verification is never disabled."
        ),
    ),
    Source(
        id="bnss-2023",
        title="The Bharatiya Nagarik Suraksha Sanhita, 2023 (Act No. 46 of 2023), India Code consolidated text",
        publisher="Legislative Department, Ministry of Law and Justice (India Code)",
        type="pdf",
        url=BNSS_PDF_URL,
        mirrors=["https://www.indiacode.nic.in/bitstream/123456789/20099/1/A202346.pdf"],
        licence=(
            "Central Act published on India Code. The PDF is fetched from India Code and not redistributed; "
            "short verbatim quotes only."
        ),
        profile="bnss",
        citation_ids=["BNSS-94", "BNSS-106", "BNSS-107", "BNSS-168"],
        # sections cited by the SOP (holds, seizure, attachment, disposal of property)
        index_sections=["94", "106", "107", "168", "173", "497", "498", "503", "504"],
        expect_sections=["94", "106", "107", "168"],
        section_citations={"94": "BNSS-94", "106": "BNSS-106", "107": "BNSS-107", "168": "BNSS-168"},
        notes=(
            "India Code item handle 123456789/496550 (DSpace 9 REST bitstream 'A2023-46.pdf', consolidated "
            "'as on the 6th October, 2025'). The legacy indiacode.nic.in URL returns 403 to scripts."
        ),
    ),
    Source(
        id="pmlr-2005",
        title="Prevention of Money-laundering (Maintenance of Records) Rules, 2005 (consolidated, FIU-IND)",
        publisher="Financial Intelligence Unit - India, Ministry of Finance",
        type="pdf",
        url=PMLR_PDF_URL,
        mirrors=["https://fiuindia.gov.in/files/AML_Legislation/notification.html"],
        licence="Government rules published by FIU-IND; not redistributed; short verbatim quotes only.",
        profile="pmlr",
        citation_ids=["PMLR-2-1-G"],
        expect_text="Suspicious transaction",
        expect_sections=["2", "2(1)(g)", "3", "8"],
        section_citations={"2(1)(g)": "PMLR-2-1-G", "8": "PMLR-8"},
        notes=(
            "Consolidated Rule 8(2) in this text says STRs are furnished 'promptly'; the 'not later than seven "
            "working days' limit is stated in the FIU-IND FAQ (source fiu-faq)."
        ),
    ),
    Source(
        id="fiu-faq",
        title="FIU-IND Frequently Asked Questions (PMLA / PML Rules)",
        publisher="Financial Intelligence Unit - India, Ministry of Finance",
        type="html",
        url=FIU_FAQ_URL,
        licence="FIU-IND website content; not redistributed; short verbatim quotes only.",
        profile="faq",
        citation_ids=["PMLR-8"],
        expect_text="seven working days",
        start_marker="What is money laundering?",
        section_citations={
            "Are there any time limits prescribed for furnishing information to Director, FIU-IND?": "PMLR-8",
            "What constitutes a suspicious transaction under PMLA?": "PMLR-2-1-G",
        },
    ),
    Source(
        id="rbi-frm-dir-2026",
        title=(
            "Reserve Bank of India (Commercial Banks - Fraud Risk Management) Directions, 2026, "
            "RBI/DoS/2026-27/412, DoS.CO.FMG.6/23.04.001/2026-27, 31.07.2026"
        ),
        publisher="Reserve Bank of India",
        type="html",
        url=RBI_FRM_DIR_URL,
        licence="RBI website content (see rbi.org.in Disclaimer); not redistributed; short verbatim quotes only.",
        profile="rbi_dir",
        citation_ids=["RBI-FRM-DIR-2026", "RBI-FRM-DIR-2026-EWS"],
        expect_text="money mule accounts",
        start_marker="RBI/DoS/2026-27/412",
        end_marker="All Months",
        drop_regex=[r"^Table of Contents\s*$.*?(?=^Introduction\s*$)"],
        front_matter="Header",
        expect_sections=["Introduction", "1", "12", "23", "24", "25", "67", "70", "Footnotes"],
        section_citations=_RBI_FRM_DIR_SECTIONS,
        notes=(
            "Para 67 repeals the earlier fraud risk management instructions for Commercial Banks, listed in "
            "circular DoS.CO.PPG.66/11.01.005/2026-27 of 31.07.2026; this replaces the RBI Master Directions on "
            "Fraud Risk Management of 15.07.2024 (RBI/DOS/2024-25/118, NotificationUser.aspx?Id=12702, formerly "
            "source rbi-frm-md-2024 / citation RBI-FRM-MD-2024), which RBI lists among the circulars withdrawn by "
            "the Department of Supervision. Applies to Commercial Banks only (not SFBs, Payments Banks or LABs; "
            "the 2024 MD also covered RRBs and AIFIs). The HTML notification page carries the full text; the "
            "PDF (rbidocs.rbi.org.in/rdocs/notification/PDFs/412MD5B6CA712E156413BACDEA9DD5CF5D1EF.PDF) is not "
            "used because citations are pinned to the HTML text (page null)."
        ),
    ),
]

# Court judgments are cited only by URL (news/blog reports); no text is fetched or stored.
CASE_LAW_URLS: dict[str, str] = {
    "CASE-NEELKANTH": (
        "https://www.scconline.com/blog/post/2025/03/01/"
        "delhi-high-court-freezing-bank-accounts-cyber-crime-policy-reform-legal-news/"
    ),
    "CASE-KARTIK-CHATUR": (
        "https://www.scconline.com/blog/post/2025/12/01/"
        "investigating-agency-cannot-debit-freeze-account-u-s-106-bnss-bom-hc/"
    ),
    "CASE-SHREE-BALAJI": (
        "https://www.pinkcitypost.com/lien-on-disputed-amount-not-blanket-freeze-rajasthan-high-court-"
        "issues-statewide-cyber-fraud-banking-directions/"
    ),
}


def sources_by_id() -> dict[str, Source]:
    return {s.id: s for s in SOURCES}


def source_for_url(url: str) -> Source | None:
    for s in SOURCES:
        if url and url in s.all_urls():
            return s
    return None


def sources_payload() -> dict:
    return {
        "note": "Generated from wapsi.search.sources.SOURCES (uv run python -m wapsi.search.sources).",
        "sources": [s.model_dump() for s in SOURCES],
        "case_law_urls": CASE_LAW_URLS,
    }


def write_sources_json(path: Path = SOURCES_JSON) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sources_payload(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_sources_json(path: Path = SOURCES_JSON) -> list[Source]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Source.model_validate(s) for s in data["sources"]]


def main() -> None:
    print(f"wrote {write_sources_json()}")


if __name__ == "__main__":
    main()
