"""Section detection and chunking on synthetic text shaped like the SOP / BNSS / PML Rules / RBI MD."""

from wapsi.search.chunk import MAX_CHARS, chunk_document, order_citations, window_section
from wapsi.search.extract import ExtractedDoc, Page, clean_pdf_pages, html_to_text, sniff
from wapsi.search.sections import (
    ROMANS,
    Line,
    Section,
    candidate_keys,
    detect_sections,
    mapped_citations,
    render_key,
)
from wapsi.search.sources import sources_by_id

SOP = sources_by_id()["sop-2026"].model_copy(update={"skip_pages": [], "headings_from_page": 2})


def _doc(pages: list[str], doc_id: str = "sop-2026", kind: str = "pdf") -> ExtractedDoc:
    return ExtractedDoc(
        doc_id=doc_id, kind=kind, pages=[Page(number=i, text=t) for i, t in enumerate(pages, 1)]
    )


def _roman_items(n: int, texts: dict[int, str] | None = None) -> str:
    texts = texts or {}
    return "\n".join(
        f"{ROMANS[i]}) {texts.get(i + 1, f'Principle number {i + 1} applies.')}" for i in range(n)
    )


SOP_PAGES = [
    # page 1: front matter with its own numbered list (must not become top-level headings)
    "From, Registrar General\n1. All the District and Sessions Judges,\n2. Principal Judges",
    "SECTION-I\n1. Introduction to National Cybercrime Reporting Portal\nThe portal was launched in 2019.\n"
    "2. Need for a Standard Operating Procedure (SOP)\nCFCFRMS has prevented crime proceeds.\n"
    "3. Objectives of the Standard Operating Procedure (SOP)\nFair system.\n"
    "4. Scope of the Standard Operating Procedure (SOP)\n" + _roman_items(3),
    "5. Guiding Principles for the Standard Operating Procedure (SOP)\n"
    + _roman_items(
        13,
        {
            9: "Officers of LEAs shall ensure judicious use.\nUnwarranted orders for freezing accounts shall be "
            "discouraged, and accountability measures shall be established.",
            10: "Money lost in CEFCs and held with the banks and FIs, at any layer, can be released\n"
            "a) Orders issued under Sections 106(3) BNSS\nb) Orders issued by competent courts\n"
            "c) Any process prescribed by jurisdictional High Courts.",
            12: "An IO must take into account the possibility of reported accounts being operated without "
            "the knowledge or connivance or consent of the account holder",
            13: "While deciding\na) Whenever the amount can be attributed\nb) Whenever such attribution is not "
            "possible, the principle of equitable or pro-rata distribution will be adopted.",
        },
    ),
    "6. Process of Complaint Registration and Actioning\n6.1 Process of Complaints Registration on NCRP\n"
    + "\n".join(f"{c}) Step {c} of the NCRP process." for c in "abcdefghij")
    + "\n6.2 Process of Complaints Registration on 1930\na) Call 1930.",
    "7. Stakeholders of the NCRP-CFCFRMS\na) Banks\n8. Development, Maintenance, and Security\nNIC.\n"
    "Section-II\n9. Processes for holding the amount\n9.1 Measures to be taken by Banks:\n"
    "i. When CFCFRMS notifies a bank the bank nodal officer shall put the amount on hold.\n"
    "This type of hold shall be placed only in the case of first-layer accounts.\n"
    "ii. On being approached by the account holder, the Bank may inform her.\n"
    "iii. In a case, where CFCFRMS notifies for action against a nodal, escrow, or pool account\n"
    "9.2 Measures to be Taken by Merchant/e-Commerce companies\ni. In case reported transactions\n"
    "a. It will cancel the order if delivery of goods or services is not made\nb. Pool account.",
    "10. Grievance Redressal Mechanism\nGrievances.\n10.1 In case of grievances related to the amount put on hold\n"
    "a) A person affected\nb) The grievance will be assigned.\n"
    "11. Processes for giving interim custody\nfive alternative processes\n"
    "Process 1: Interim Process where there is Single Victim:\na) Interim custody of amount below Rs. 50,000\n"
    "b) above Rs. 50,000\ni. The IO shall issue a notice within 7 calendar days.\nii. Up to 15 calendar days.\n"
    "Process 2: Process where there are Multiple Victims\na) notice\nb) days\nc) record\nd) analyse\ne) agree\n"
    "f) directions\ng) examine\nh) status\ni) difference of opinion\n"
    "11.1 Disposal of Unclaimed Amount\nsection 503 or 504 BNSS",
    "12. General Definitions:\n"
    + "\n".join(
        f"{ROMANS[i]}. "
        + (
            "First-layer accounts: A bank account which receives the de-\nfrauded money directly from the bank"
            if i == 9
            else f"Term {i + 1}."
        )
        for i in range(11)
    ),
    "ANNEXURE-I\nNOTICE UNDER SECTION 168 BNSS READ WITH 94 BNSS\n1. Hold the amount equivalent to the Disputed "
    "amount received in A/c No.\n2. Update the beneficiary details\nattached as\nAnnexure - I.",
    "Annexure-V\nIllustrations\nIllustration 1: Multiple Complaints - Attribution Possible\ntext\n"
    "Illustration 2: Pro-rata restoration\nreleased on a pro rata basis.",
]


def _sop_sections():
    return {s.key: s for s in detect_sections(_doc(SOP_PAGES), SOP)}


def test_render_and_candidate_keys():
    assert render_key(("5", "ix")) == "5(ix)"
    assert render_key(("9", "9.1", "ii")) == "9.1(ii)"
    assert render_key(("11", "Process 1", "iv")) == "11 Process 1(iv)"
    assert render_key(("2", "1", "g")) == "2(1)(g)"
    assert render_key(("Annexure-V", "Illustration 2")) == "Annexure-V Illustration 2"
    assert candidate_keys(("9", "9.1", "ii")) == ["9.1(ii)", "9.1", "9"]
    assert mapped_citations(("9", "9.1", "ii"), {"9.1(ii)": "A", "9.1": "B"}) == ["A", "B"]
    assert mapped_citations(("5", "xii"), {"5(x)": "X"}) == []  # no false prefix match


def test_sop_structure_detected():
    secs = _sop_sections()
    for key in [
        "1", "2", "3", "4(iii)", "5(ix)", "5(x)", "5(xi)", "5(xii)", "5(xiii)", "6.1", "6.2", "7", "8", "9.1(i)",
        "9.1(ii)", "9.1(iii)", "9.2(i)", "10.1", "11", "11 Process 1", "11 Process 1(i)", "11 Process 1(ii)",
        "11 Process 2", "11.1", "12(x)", "12(xi)", "Annexure-I", "Annexure-V Illustration 1",
        "Annexure-V Illustration 2",
    ]:  # fmt: skip
        assert key in secs, key
    assert "Unwarranted orders" in secs["5(ix)"].text()
    assert "first-layer accounts" in secs["9.1(i)"].text()
    assert secs["5(ix)"].label == "SOP Para 5(ix)"
    assert secs["Annexure-I"].label == "SOP Annexure-I"
    assert secs["5(ix)"].title.startswith("Guiding Principles")
    assert secs["5(x)"].page == 3


def test_front_matter_numbering_is_not_a_heading():
    first = detect_sections(_doc(SOP_PAGES), SOP)[0]
    assert first.key == SOP.front_matter
    assert "Principal Judges" in first.text()


def test_letters_h_i_are_not_roman_numerals():
    secs = _sop_sections()
    # "i) Step i" follows "h)" inside 6.1 -> a letter, so no 6.1(i) section
    assert "6.1(i)" not in secs
    assert "Step i of the NCRP process" in secs["6.1"].text()
    # same inside Process 2
    assert "11 Process 2(i)" not in secs
    assert "difference of opinion" in secs["11 Process 2"].text()
    # but after letters a), b) a new roman list starts at i.
    assert "within 7 calendar days" in secs["11 Process 1(i)"].text()


def test_inline_annexure_reference_is_not_a_heading():
    sections = detect_sections(_doc(SOP_PAGES), SOP)
    annex_one = [s for s in sections if s.key == "Annexure-I"]
    assert len(annex_one) == 1
    assert "Annexure - I." in annex_one[0].text()  # stays inside, no new section


def test_annexure_list_items_do_not_restart_numbering():
    secs = _sop_sections()
    assert "Hold the amount equivalent to the Disputed amount" in secs["Annexure-I"].text()
    assert "2" in secs and "Need for" in secs["2"].title  # not overwritten by the annexure list


def test_chunks_carry_section_page_and_citations():
    quotes = {
        "SOP-5-IX": "Unwarranted orders for freezing accounts shall be discouraged",
        "SOP-9-1-I": "This type of hold shall be placed only in the case of first-layer accounts",
        "SOP-DEF-FIRST-LAYER": "A bank account which receives the de-frauded money directly",
    }
    chunks = chunk_document(_doc(SOP_PAGES), SOP, quotes)
    by_key: dict[str, list] = {}
    for c in chunks:
        by_key.setdefault(c.section_key, []).append(c)
    assert by_key["5(ix)"][0].citation_id == "SOP-5-IX"
    assert by_key["5(ix)"][0].page == 3
    assert by_key["5(ix)"][0].section == "SOP Para 5(ix)"
    # the sub-paragraph mapping (9.1(i) -> SOP-9-1-I) and quote containment agree
    assert by_key["9.1(i)"][0].citation_id == "SOP-9-1-I"
    assert "SOP-9-1-I" in by_key["9.1(i)"][0].citation_ids
    assert by_key["9.1(ii)"][0].citation_id == "SOP-9-1-II"
    assert by_key["9.2(i)"][0].citation_id == "SOP-9-2"
    assert by_key["11 Process 1(ii)"][0].citation_id == "SOP-11-P1"
    assert by_key["Annexure-V Illustration 2"][0].citation_id == "SOP-ANNEX-V"
    # hyphenated line break "de-\nfrauded" is matched by the whitespace-insensitive comparison
    assert by_key["12(x)"][0].citation_id == "SOP-DEF-FIRST-LAYER"
    assert "12(xi)" in by_key and by_key["12(xi)"][0].citation_id is None
    assert all(c.doc_id == "sop-2026" for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_order_citations():
    assert order_citations(["B", "C"], ["A", "B"]) == ["B", "C", "A"]
    assert order_citations([], ["A"]) == ["A"]


def test_window_section_limits_size_overlaps_and_tracks_pages():
    words = [f"w{i:03d}." if i % 15 == 14 else f"w{i:03d}" for i in range(600)]
    lines = [Line(page=1 + i // 200, text=" ".join(words[i : i + 10])) for i in range(0, 600, 10)]
    sec = Section(path=("5", "x"), title="t", label_prefix="SOP Para", lines=lines)
    wins = window_section(sec, max_chars=300, overlap=60)
    assert len(wins) > 1
    assert all(len(w.text) <= 300 for w in wins)
    for a, b in zip(wins, wins[1:], strict=False):
        assert a.text.split()[-1] in b.text.split()[:15]  # overlap carried over
    assert wins[0].page == 1 and wins[-1].page_end == 3
    joined = " ".join(w.text for w in wins).split()
    assert set(joined) == set(words)  # nothing lost
    assert MAX_CHARS == 900


def test_bnss_body_headings_ignore_arrangement_and_join_wrapped_titles():
    src = sources_by_id()["bnss-2023"].model_copy(
        update={
            "index_sections": ["2", "4", "5"],
            "section_citations": {"2": "BNSS-94", "4": "BNSS-106", "5": "BNSS-107"},
        }
    )
    dash = "—"
    pages = [
        "ARRANGEMENT OF SECTIONS\n1. Short title.\n2. Definitions.\nA.—Summons\n3. Summons to produce document.\n"
        "B.—Warrant of arrest\n4. Power of police officer.",
        f"1. Short title, extent and commencement.{dash}(1) This Sanhita.\nCHAPTER VII\n"
        f"2. Summons to produce document or other thing .{dash}(1) Whenever any Court or any officer\n"
        "in charge of a police station considers\n"
        "3. Recording of search and seizure through audio-video electronic means that is\n"
        f"recorded.{dash}The process of conducting search\n"
        f"4. Power of police officer to seize certain property.{dash}(1) Any police officer may seize\n"
        f"5. Attachment, forfeiture or restoration of property .{dash}(1) Where a police officer\n"
        f"(6) rateably distribute such proceeds of crime\n2. Not a heading.{dash}because numbering went back\n"
        f"40. Too far ahead.{dash}a jump of 35 is not plausible",
    ]
    secs = detect_sections(_doc(pages, "bnss-2023"), src)
    keys = [s.key for s in secs]
    assert keys == ["Front matter", "1", "2", "3", "4", "5"]
    by = {s.key: s for s in secs}
    assert "Warrant of arrest" in by["Front matter"].text()  # arrangement lines are not headings
    assert by["2"].title == "Summons to produce document or other thing"
    assert (
        by["3"].title
        == "Recording of search and seizure through audio-video electronic means that is recorded"
    )
    assert "Not a heading" in by["5"].text() and "Too far ahead" in by["5"].text()
    assert by["4"].label == "BNSS Section 4"
    chunks = chunk_document(_doc(pages, "bnss-2023"), src)
    assert {c.section_key for c in chunks} == {"2", "4", "5"}  # index_sections filter
    assert {c.citation_id for c in chunks} == {"BNSS-94", "BNSS-106", "BNSS-107"}


def test_pmlr_rules_and_definition_clauses():
    src = sources_by_id()["pmlr-2005"]
    pages = [
        "1. Short title and commencement. —(1) These rules\n2. Definitions.—(1) In these rules,\n"
        "(a) “Act” means the Act;\n[(g) “Suspicious transaction” means a transaction\n"
        "(b) appears to be made in circumstances of unusual complexity;\n[(h) “transaction” means\n"
        "(2) All other words\n3. Maintenance of records of transactions (nature and value).\n"
        "—[Every reporting entity shall maintain\n[8. Furnishing of information to the Director .—(1) The "
        "Principal Officer\n1. Subs. by G.S.R. 1.—footnote",
    ]
    secs = detect_sections(_doc(pages, "pmlr-2005"), src)
    by = {s.key: s for s in secs}
    assert list(by) == ["1", "2", "2(1)(a)", "2(1)(g)", "2(1)(h)", "2(2)", "3", "8"]
    assert "unusual complexity" in by["2(1)(g)"].text()
    assert by["2(1)(g)"].label == "PML Rules, Rule 2(1)(g)"
    assert "footnote" in by["8"].text()
    chunks = chunk_document(_doc(pages, "pmlr-2005"), src)
    assert {c.citation_id for c in chunks if c.section_key == "2(1)(g)"} == {"PMLR-2-1-G"}
    assert {c.citation_id for c in chunks if c.section_key == "8"} == {"PMLR-8"}


def test_rbi_paragraph_numbering_follows_chapters():
    src = sources_by_id()["rbi-frm-md-2024"]
    text = (
        "RBI/DOS/2024-25/118\nDear Sir,\nAnnex\nINTRODUCTION\nIn exercise of the powers\nCHAPTER III\n"
        "3. Early Detection of Frauds\n3.1 Governance Structure\n3.1.1 Banks shall have a framework for EWS\n"
        "4.1 Not in chapter four yet\nCHAPTER IV\n4.1 Credit facility\nList of Circulars Repealed\n"
        "1. DOS.CO circular\n* Export-Import Bank\n2 RRBs shall report"
    )
    secs = detect_sections(_doc([text], "rbi-frm-md-2024", kind="html"), src)
    keys = [s.key for s in secs]
    assert keys == [
        "Covering letter", "Master Directions", "Introduction", "3", "3.1", "3.1.1", "4.1",
        "Annex (circulars repealed)", "Footnotes",
    ]  # fmt: skip
    by = {s.key: s for s in secs}
    assert "Not in chapter four yet" in by["3.1.1"].text()
    chunks = chunk_document(_doc([text], "rbi-frm-md-2024", kind="html"), src)
    assert {c.citation_id for c in chunks if c.section_key.startswith("3")} == {"RBI-FRM-MD-2024"}
    assert all(c.page is None for c in chunks)  # HTML has no pages


def test_faq_questions_become_sections():
    src = sources_by_id()["fiu-faq"]
    text = "What is PMLA?\nAn Act.\nAre there any time limits prescribed for furnishing information to Director, FIU-IND?\nYes."
    secs = detect_sections(_doc([text], "fiu-faq", kind="html"), src)
    assert [s.key for s in secs] == [
        "What is PMLA?",
        "Are there any time limits prescribed for furnishing information to Director, FIU-IND?",
    ]
    assert secs[0].label == "FIU-IND FAQ: What is PMLA?"
    chunks = chunk_document(_doc([text], "fiu-faq", kind="html"), src)
    assert chunks[1].citation_id == "PMLR-8"


def test_clean_pdf_pages_strips_running_headers_and_keeps_labels():
    words = ["zero", "alpha", "beta", "gamma", "delta"]
    raw = [
        f"Page {n} of 4\n{words[n]} opens\nmiddle {words[n]} one\nmiddle {words[n]} two\n{words[n]} closes\nIndiaCode"
        for n in range(1, 5)
    ]
    raw[0] = "12\nCover body\nIndiaCode"
    texts, labels = clean_pdf_pages(raw)
    assert texts[1] == "beta opens\nmiddle beta one\nmiddle beta two\nbeta closes"
    assert texts[0] == "Cover body"
    assert labels == ["12", "2", "3", "4"]
    assert all("IndiaCode" not in t for t in texts)


def test_html_to_text_and_sniff():
    html = "<html><head><style>x{}</style><script>var a=1;</script></head><body><div>Hello&nbsp;<b>world</b></div><p>Next</p></body></html>"
    assert html_to_text(html) == "Hello world\nNext"
    assert sniff(b"%PDF-1.7 ...") == "pdf"
    assert sniff(b"\xef\xbb\xbf<!DOCTYPE html><html>") == "html"
    assert sniff(b'{"a": 1}') == "json"
    assert sniff(b"\x00\x01") == "unknown"
