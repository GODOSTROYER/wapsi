"""Canonical citation IDs. Code references these IDs; `data/docs/citations.json` supplies verbatim quotes.

Never invent a clause: if an ID is not in REGISTRY, the verifier fails the draft.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

from wapsi.models import Citation

SOP_TITLE = (
    "Standard Operating Procedure (SOP) for NCRP-CFCFRMS, Custody, Restoration of money and Grievance "
    "Redressal (MHA / I4C, 02.01.2026)"
)
SOP_URL = (
    "https://cdnbbsr.s3waas.gov.in/s3bc7f621451b4f5df308a8e098112185d/uploads/2026/06/202606181689326167.pdf"
)
PML_TITLE = "Prevention of Money-laundering (Maintenance of Records) Rules, 2005"
FIU_FAQ_URL = "https://fiuindia.gov.in/files/FAQs/faqs.html"
BNSS_URL = "https://indiacode.gov.in/server/api/core/bitstreams/73f93470-5f6a-4f0f-bc23-1309e32b9554/content"
RBI_FRM_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12702&Mode=0"

# id -> (source_title, section, url)
_BASE: dict[str, tuple[str, str, str]] = {
    "SOP-5-IX": (SOP_TITLE, "Para 5(ix): unwarranted freezing of accounts discouraged", SOP_URL),
    "SOP-5-X": (SOP_TITLE, "Para 5(x): money held at any layer can be released to the victim", SOP_URL),
    "SOP-5-XII": (SOP_TITLE, "Para 5(xii): accounts operated without knowledge, connivance or consent", SOP_URL),
    "SOP-5-XIII": (SOP_TITLE, "Para 5(xiii): attribution to victim / equitable pro-rata distribution", SOP_URL),
    "SOP-DEF-FIRST-LAYER": (SOP_TITLE, "Definitions: first-layer account", SOP_URL),
    "SOP-6": (SOP_TITLE, "Para 6: reporting and pushing complaints to CFCFRMS", SOP_URL),
    "SOP-9-1": (SOP_TITLE, "Para 9.1: bank nodal officer to put the amount on hold (s.168 r/w s.94 BNSS)", SOP_URL),
    "SOP-9-1-I": (SOP_TITLE, "Para 9.1(i): forward-looking hold only on first-layer accounts; not applied to "
                  "nodal, pool and escrow accounts", SOP_URL),
    "SOP-9-1-III": (SOP_TITLE, "Para 9.1(iii): nodal, escrow or pool account: hold the disputed amount; LEAs to "
                    "normally refrain from suspension or seizure", SOP_URL),
    "SOP-9-1-II": (SOP_TITLE, "Para 9.1(ii): informing the account holder is not tipping-off", SOP_URL),
    "SOP-9-2": (SOP_TITLE, "Para 9.2: merchants and e-commerce entities", SOP_URL),
    "SOP-9-3": (SOP_TITLE, "Para 9.3: payment system operators (PPI / CBDC wallet issuers)", SOP_URL),
    "SOP-9-5": (SOP_TITLE, "Para 9.5: TPAPs, payment aggregators / gateways and other financial intermediaries", SOP_URL),
    "SOP-10-1": (SOP_TITLE, "Para 10.1: grievance redressal, EDD, timelines, 90-day lapse of hold", SOP_URL),
    "SOP-11-P1": (SOP_TITLE, "Para 11, Process 1: s.106 BNSS, single victim", SOP_URL),
    "SOP-11-P2": (SOP_TITLE, "Para 11, Process 2: multiple victims / competing claims", SOP_URL),
    "SOP-11-P3": (SOP_TITLE, "Para 11, Process 3: competent court, s.497/498/503 BNSS", SOP_URL),
    "SOP-11-P4": (SOP_TITLE, "Para 11, Process 4: s.107 BNSS attachment/forfeiture", SOP_URL),
    "SOP-11-P5": (SOP_TITLE, "Para 11, Process 5: process directed by jurisdictional court", SOP_URL),
    "SOP-ANNEX-I": (SOP_TITLE, "Annexure-I: notice under s.168 read with s.94 BNSS", SOP_URL),
    "SOP-ANNEX-V": (SOP_TITLE, "Annexure-V: pro-rata illustrations", SOP_URL),
    "BNSS-94": ("Bharatiya Nagarik Suraksha Sanhita, 2023", "Section 94", BNSS_URL),
    "BNSS-106": ("Bharatiya Nagarik Suraksha Sanhita, 2023", "Section 106", BNSS_URL),
    "BNSS-107": ("Bharatiya Nagarik Suraksha Sanhita, 2023", "Section 107", BNSS_URL),
    "BNSS-168": ("Bharatiya Nagarik Suraksha Sanhita, 2023", "Section 168", BNSS_URL),
    "PMLR-2-1-G": (PML_TITLE, "Rule 2(1)(g): suspicious transaction", FIU_FAQ_URL),
    "PMLR-8": (PML_TITLE, "Rule 8: STR within seven working days", FIU_FAQ_URL),
    "FIU-CIRC-2025-06-02": ("FIU-IND circular dated 02.06.2025", "Informing account holder is not tipping-off", SOP_URL),
    "RBI-FRM-MD-2024": ("RBI Master Directions on Fraud Risk Management (15.07.2024)", "Early warning signals", RBI_FRM_URL),
    "CASE-NEELKANTH": ("Neelkanth Pharma Logistics Pvt Ltd v. Union of India, Delhi HC, 20.02.2025",
                       "Lien on disputed amount should be explored", ""),
    "CASE-KARTIK-CHATUR": ("Kartik Yogeshwar Chatur v. Union of India, Bombay HC, 20.11.2025",
                           "Banks may only place the disputed amount under lien; no debit freeze under s.106", ""),
    "CASE-SHREE-BALAJI": ("Shree Balaji Enterprises v. RBI, Rajasthan HC, 20.08.2026",
                          "Preserve the identifiable disputed amount by lien, not whole-account freeze", ""),
    "CASE-SAJEER": ("Dr Sajeer v. RBI, Kerala HC, 2023", "Freeze confined to the specific amount", ""),
    "CASE-SAIFULLAH": ("Mohammed Saifullah v. RBI, Madras HC, W.P. 25631/2024", "Hold limited to disputed sum", ""),
}

CITATIONS_JSON = Path(__file__).resolve().parents[2] / "data" / "docs" / "citations.json"
PACKAGED_CITATIONS = "_data/citations.json"  # copy shipped inside the package (Snowpark zip imports, wheels)


def _load_quotes() -> dict[str, dict]:
    """Quotes from $WAPSI_CITATIONS_JSON, the repo's data/docs/citations.json, or the packaged copy."""
    env = os.environ.get("WAPSI_CITATIONS_JSON")
    for path in (Path(env) if env else None, CITATIONS_JSON):
        if path is not None and path.exists():
            return {c["citation_id"]: c for c in json.loads(path.read_text(encoding="utf-8"))}
    try:
        text = resources.files("wapsi").joinpath(PACKAGED_CITATIONS).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    return {c["citation_id"]: c for c in json.loads(text)}


def registry() -> dict[str, Citation]:
    quotes = _load_quotes()
    out: dict[str, Citation] = {}
    for cid, (title, section, url) in _BASE.items():
        q = quotes.get(cid, {})
        out[cid] = Citation(
            citation_id=cid,
            source_title=q.get("source_title", title),
            section=q.get("section", section),
            quote=q.get("quote", ""),
            url=q.get("url", url),
            page=q.get("page"),
        )
    return out


def is_known(citation_id: str) -> bool:
    return citation_id in _BASE


KNOWN_IDS = frozenset(_BASE)
