"""The shared token grammar (wapsi.money) the verifier checks and the drafter sanitises."""

from __future__ import annotations

import pytest

from wapsi.money import (
    find_account_ids,
    find_amounts,
    find_dates,
    find_money_mentions,
    find_utrs,
    fmt_inr,
    is_single_amount,
    normalize_text,
    parse_inr,
)


def amounts(text: str) -> list[tuple[str, float]]:
    return [(m.text, m.value) for m in find_money_mentions(text)]


@pytest.mark.parametrize(
    "text,expected",
    [
        # digits, any marker, any spacing, suffixes
        ("Hold ₹43,200.00 now", [("₹43,200.00", 43200.0)]),
        ("Hold 9,99,999 rupees in it", [("9,99,999 rupees", 999999.0)]),
        ("Hold INR  9,99,999.00 in it", [("INR  9,99,999.00", 999999.0)]),
        ("Hold Rs.4,80,000/- in it", [("Rs.4,80,000/-", 480000.0)]),
        ("Hold Rs 4,80,000/- only.", [("Rs 4,80,000/- only", 480000.0)]),
        (
            "Rs4000, 5000rs, Re. 1 and ₨ 7",
            [("Rs4000", 4000.0), ("5000rs", 5000.0), ("Re. 1", 1.0), ("₨ 7", 7.0)],
        ),
        ("Hold 480,000 now", [("480,000", 480000.0)]),  # Western grouping, no marker
        ("Hold ₹480,000.50", [("₹480,000.50", 480000.5)]),
        # scale words multiply
        ("Hold ₹43,200.00 crore", [("₹43,200.00 crore", 432_000_000_000.0)]),
        ("Hold ₹1.2 lakh", [("₹1.2 lakh", 120000.0)]),
        ("Hold ₹5k", [("₹5k", 5000.0)]),
        ("Hold 2.5 cr", [("2.5 cr", 25_000_000.0)]),
        ("9 lakh 50 thousand moved", [("9 lakh 50 thousand", 950000.0)]),
        # other scripts
        ("Hold ₹९,९९,९९९ now", [("₹९,९९,९९९", 999999.0)]),
        ("Hold ₹１，２００.００ now", [("₹１，２００.００", 1200.0)]),
        ("रु 500 जमा", [("रु 500", 500.0)]),
        # words
        (
            "nine lakh fifty thousand rupees is held and rupees two crore rest elsewhere",
            [("nine lakh fifty thousand rupees", 950000.0), ("rupees two crore", 20_000_000.0)],
        ),
        ("Rs. five lakh only", [("Rs. five lakh only", 500000.0)]),
        ("rupees nine lakh ninety-nine thousand", [("rupees nine lakh ninety-nine thousand", 999000.0)]),
        ("one hundred and five rupees", [("one hundred and five rupees", 105.0)]),
        ("a lakh rupees", [("a lakh rupees", 100000.0)]),
        ("one lakh crore", [("one lakh crore", 1e12)]),
        ("forty three thousand two hundred went", [("forty three thousand two hundred", 43200.0)]),
        ("do lakh rupaye bheje", [("do lakh rupaye", 200000.0)]),
        ("paanch hazaar diye", [("paanch hazaar", 5000.0)]),
        ("fifty paise", [("fifty paise", 0.5)]),
    ],
)
def test_money_mentions(text, expected):
    assert amounts(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Section 480 and Rule 2(1)(g), Para 9.1, Section 106",
        "A/c No. HB01-1000000001 vide UTR 425310000001 and ATM000000004",
        "on 10-09-2026 11:02 IST, circular of 02.06.2025 [FIU-CIRC-2025-06-02]",
        "Invoice INV-K-5521, ATM PUNE-SECTOR-4-01, FIR 198/2026",
        "three accounts, 1,200 transactions, 5 lakh accounts, twelve hundred",
        "do not hold; Mr Das and a teen; seven days",
        "95% within 24 hours; risk score of 0.91; FINnet 2.0",
        "UTRs 425310000001 and transfers 425310000002",
        "lakhs of rupees",  # vague, no amount
        "01CR5 and 5kg",
    ],
)
def test_not_money(text):
    assert amounts(text) == []


def test_count_after_amount_is_not_glued():
    assert amounts("₹43,200.00 two accounts") == [("₹43,200.00", 43200.0)]


def test_offsets_and_compat_helpers():
    text = "x ₹९,००० y"
    (m,) = find_money_mentions(text)
    assert text[m.start : m.end] == m.text == "₹९,०००"
    assert len(normalize_text(text)) == len(text) and normalize_text(text) == "x ₹9,000 y"
    assert find_amounts("Hold ₹43,200.00") == [("₹43,200.00", 43200.0)]
    assert parse_inr("₹4,80,000.00") == 480000.0 and parse_inr("4,80,000") == 480000.0
    assert is_single_amount(fmt_inr(0)) is not None and is_single_amount("₹1 and ₹2") is None
    assert find_money_mentions("nine lakh rupees")[0].in_words


def test_account_ids_any_case_and_separator():
    text = "hb01-1000000001, HB01 1000000002, HB011000000003, OB02-12345678, TX0000000001, HB01N26026123456"
    assert [c for _, c in find_account_ids(text)] == [
        "HB01-1000000001",
        "HB01-1000000002",
        "HB01-1000000003",
        "OB02-12345678",
    ]


def test_utr_shapes():
    text = (
        "4253 1000 0001; 4253-1000-0002; 425310000003; N260261234567890; hb01n26026123456; "
        "HDFCR52024091012345678; ATM000000004; CSH000000005"
    )
    assert [c for _, c in find_utrs(text)] == [
        "425310000001",
        "425310000002",
        "425310000003",
        "N260261234567890",
        "HB01N26026123456",
        "HDFCR52024091012345678",
        "ATM000000004",
        "CSH000000005",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "NCRP 31009260000101",  # 14-digit complaint number
        "HB01-1000000001",
        "10-09-2026 11:02 IST",
        "mobile 99XXXXXX13, PAN XXXXX2335A, DEV-3813341, CUST-0000001",
        "Rs 4,80,000.00",
        "[FIU-CIRC-2025-06-02]",
    ],
)
def test_not_utrs(text):
    assert find_utrs(text) == []


def test_date_shapes():
    text = (
        "10-09-2027, 10/09/2027, 10.09.2027, 10 Sep 2027, 10-Sep-2027, 10th of September, 2027, "
        "September 10, 2027, Sep 10th 2027, 2027-09-10, 31/02/2026"
    )
    found = find_dates(text)
    assert [c for _, c in found] == ["10-09-2027"] * 9 + [None]
    assert [s for s, _ in found][:3] == ["10-09-2027", "10/09/2027", "10.09.2027"]


def test_timestamps_convert_to_ist_and_non_dates_are_ignored():
    assert find_dates("2026-09-10T20:00:00+00:00") == [("2026-09-10T20:00:00+00:00", "11-09-2026")]
    assert find_dates("1998-02-24") == [("1998-02-24", "24-02-1998")]
    assert find_dates("[FIU-CIRC-2025-06-02] SETTLEMENT 02-09, FIR 198/2026, Para 9.1, may 2026") == []
