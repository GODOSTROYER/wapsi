"""CheckResult carries its machine code in ``code``; the message is plain text. Legacy records still read."""

from __future__ import annotations

from wapsi.models import CheckResult
from wapsi.verify.report import build_report, code_of, fail, label_of, ok, summarize, text_of, warn


def test_fail_warn_ok_set_the_code_field():
    f = fail("numeric", "slot_mismatch", "hold_amount differs", ["₹9.00", ""])
    assert (f.code, f.message, f.spans, f.severity, f.passed) == (
        "slot_mismatch", "hold_amount differs", ["₹9.00"], "error", False)
    w = warn("citations", "citation_quote_missing", "no quote for X")
    assert (w.code, w.severity) == ("citation_quote_missing", "warning")
    o = ok("policy", "rules re-checked")
    assert (o.code, o.message, o.passed) == ("ok", "rules re-checked", True)
    for c in (f, w, o):
        assert code_of(c) == c.code and text_of(c) == c.message
    assert label_of(f) == "slot_mismatch: hold_amount differs"


def test_legacy_records_keep_the_code_in_the_message():
    legacy = CheckResult.model_validate({"name": "policy", "passed": False, "message": "over_hold: A holds ₹9.00"})
    assert legacy.code == ""
    assert code_of(legacy) == "over_hold" and text_of(legacy) == "A holds ₹9.00"
    assert label_of(legacy) == "over_hold: A holds ₹9.00"
    plain = CheckResult(name="policy", passed=False, message="Account A: held twice")
    assert code_of(plain) == "" and text_of(plain) == "Account A: held twice" and label_of(plain) == text_of(plain)


def test_summary_lines_are_unchanged():
    report = build_report("D1", "C1", [
        fail("numeric", "slot_mismatch", "hold_amount differs", ["₹9.00"]),
        warn("citations", "citation_quote_missing", "no quote for X"),
        ok("policy", "rules re-checked"),
    ])
    assert summarize(report).splitlines() == [
        "FAILED: D1 (1 error(s), 1 warning(s))",
        '  ERROR [numeric] slot_mismatch: hold_amount differs | "₹9.00"',
        "  WARN [citations] citation_quote_missing: no quote for X",
        "  OK [policy] rules re-checked",
    ]
