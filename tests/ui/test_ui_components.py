from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import plotly.graph_objects as go
import pytest

from ui.components import html as h
from ui.components.baseline_strip import BaselineSummary, baseline_sentence, baseline_strip_html, summarize
from ui.components.clock_timeline import ClockRow, build_clock_timeline
from ui.components.compat import optional, stretch
from ui.components.formatting import inr_short
from ui.components.theme import CHIP_TONES, css, detect_dark, series_hex
from ui.components.totals_waterfall import build_totals_waterfall, located_share, waterfall_steps
from ui.views.audit import summarize_details
from ui.views.clocks import subject_parts
from ui.views.drafts import clean_message, demote_headings, expected_drafts, failing_spans, warning_spans
from ui.views.hold_plan import hold_sentence, sentences
from ui.views.kpis import METRICS, fmt_metric, meets, role_chart, target_text, typology_chart
from wapsi.models import CheckResult, Clock, ClockKind, ClockStatus, Draft, DraftKind, VerificationReport
from wapsi.timeutil import IST, utcnow

# --------------------------------------------------------------------------- waterfall


def test_waterfall_steps_match_showcase_totals(bundles):
    steps = waterfall_steps(bundles[0].trace.totals)
    assert [s.label for s in steps] == ["Stolen", "Holdable now", "Cashed out", "Other banks", "Pool / nodal",
                                        "Merchant settled", "Unlocated"]
    assert [s.value for s in steps] == [480000.0, 123648.0, 144000.0, 192000.0, 19200.0, 1152.0, 0.0]
    assert steps[1].base == pytest.approx(480000 - 123648)
    assert steps[5].base == pytest.approx(0.0)
    assert located_share(bundles[0].trace.totals) == pytest.approx(1.0)


def test_waterfall_figure(bundles):
    fig = build_totals_waterfall(bundles[0].trace.totals)
    assert isinstance(fig, go.Figure)
    bar = fig.data[0]
    assert bar.orientation == "h"
    assert list(bar.text)[:3] == ["₹4.80 L", "₹1.24 L", "₹1.44 L"]
    assert len(fig.layout.shapes) == 6  # connectors between the 7 steps
    dark = build_totals_waterfall(bundles[0].trace.totals, dark=True)
    assert dark.data[0].marker.color[1] == series_hex("blue", True)


# --------------------------------------------------------------------------- baseline strip


def test_baseline_key_message_showcase_one(bundles):
    plan = bundles[0].plan
    s = summarize(plan)
    held = [d for d in plan.decisions if d.hold_amount > 0]
    assert (s.freeze_amount, s.freeze_accounts) == (50350000.0, 6)
    assert (s.hold_amount, s.hold_accounts) == (plan.total_hold, len(held))
    assert s.sentence == ("A whole-account freeze would immobilise ₹5.04 Cr across 6 accounts; "
                          f"Wapsi holds {inr_short(plan.total_hold)} across {len(held)} — only the victim's money")
    html = baseline_strip_html(s)
    assert f"{50350000 / plan.total_hold:,.0f}×" in html and "₹5.04 Cr" in html
    assert "\n" not in html


def test_baseline_sentence_matches_the_key_message():
    s = BaselineSummary(freeze_amount=50350000.0, freeze_accounts=6, hold_amount=123648.0, hold_accounts=4)
    assert s.sentence == ("A whole-account freeze would immobilise ₹5.04 Cr across 6 accounts; "
                          "Wapsi holds ₹1.24 L across 4 — only the victim's money")
    assert "407×" in baseline_strip_html(s)


def test_baseline_when_hold_equals_balance(bundles):
    s = summarize(bundles[1].plan)
    assert s.hold_amount == s.freeze_amount == 120000.0
    assert "same ₹1.20 L" in s.sentence
    assert "same amount" in baseline_strip_html(s)


def test_baseline_without_holds():
    s = BaselineSummary(freeze_amount=500000, freeze_accounts=2, hold_amount=0, hold_accounts=0)
    assert "no hold" in baseline_sentence(s)
    assert s.ratio is None
    assert "holds recommended" in baseline_strip_html(s)


# --------------------------------------------------------------------------- html helpers


def test_html_escapes_and_never_emits_blank_lines():
    out = h.quote("line one\n\n<script>alert(1)</script>", "Complainant")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "\n" not in out
    assert h.esc('"x"') == "&quot;x&quot;"


def test_status_chips_carry_an_icon():
    assert "✓" in h.chip("Passed", "good")
    assert "✕" in h.chip("Failed", "bad")
    assert "!" in h.chip("Due soon", "warn")
    assert "✓" not in h.chip("Merchant", "yellow")


def test_table_and_cells():
    html = h.table([("A", False), ("B", True)],
                   [h.Row([h.Cell("<b>x</b>"), h.Cell("₹1", right=True, nowrap=True)], sub="<i>detail</i>")],
                   caption="1 row")
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert '<td class="r nw">' in html
    assert '<tr class="wp-sub"><td colspan="2"><i>detail</i></td></tr>' in html
    assert "1 row" in html


def test_highlight_spans_marks_failures_and_warnings():
    out = h.highlight_spans("Hold ₹43,200.00 per [CASE-X] now", ["₹43,200.00"], ["[CASE-X]"])
    assert '<mark class="wp-flag" title="Failed a check">₹43,200.00</mark>' in out
    assert 'wp-flag-warn' in out
    assert h.highlight_spans("a<b", []) == "a&lt;b"
    doc = h.document("line1\n\nline2", ["line2"])
    assert "\n" not in doc and "&#10;&#10;" in doc


def test_tiles_empty_state_and_callout():
    html = h.tiles([h.Tile("Stolen", "₹4.80 L", "note", accent=True, swatch="blue")], min_width_rem=8)
    assert "wp-tile-accent" in html and "--wp-s-blue" in html
    assert 'role="status"' in h.empty_state("Nothing yet", "Body", code="uv run x")
    assert "wp-callout-warn" in h.callout("x", "warn")


# --------------------------------------------------------------------------- theme / compat


def test_css_is_self_contained_and_defines_tokens():
    sheet = css()
    assert sheet.startswith("<style>") and sheet.endswith("</style>")
    assert "http" not in sheet and "@import" not in sheet and "url(" not in sheet
    for tone in CHIP_TONES:
        assert f"--wp-{tone}-fg" in sheet and f"--wp-{tone}-bg" in sheet
    assert "\n\n" not in sheet
    assert css(True) != css(False)


def test_detect_dark_outside_streamlit_is_safe():
    assert detect_dark() in (None, True, False)


def test_compat_helpers():
    import streamlit as st

    kw = stretch(st.button)
    assert kw in ({"width": "stretch"}, {"use_container_width": True})
    assert "use_container_width" in stretch(st.dataframe) or "width" in stretch(st.dataframe)
    assert optional(st.button, not_a_real_kwarg=1) == {}
    assert optional(st.button, help="x") == {"help": "x"}


# --------------------------------------------------------------------------- clocks


def _clock(kind=ClockKind.STR_DUE, subject=None, days=5, warn=None, status=ClockStatus.OPEN) -> Clock:
    start = datetime(2026, 9, 10, 12, 0, tzinfo=IST)
    return Clock(clock_id=f"CLK-{kind.value}", case_id="CASE-1", kind=kind, subject=subject, start=start,
                 due=start + timedelta(days=days), warn_at=warn, status=status)


def test_clock_timeline_figure():
    now = datetime(2026, 9, 12, tzinfo=IST)
    rows = [ClockRow("STR due", _clock().start, _clock().due, ClockStatus.OPEN, warn_at=_clock().due),
            ClockRow("Hold lapses", _clock().start, _clock(days=90).due, ClockStatus.OVERDUE)]
    fig = build_clock_timeline(rows, now)
    names = {t.name for t in fig.data}
    assert {"On track", "Overdue", "Warning point"} <= names
    assert fig.layout.shapes[0].line.dash == "dot"


def test_clock_subject_parts(bundles):
    b = bundles[0].model_copy(deep=True)
    b.drafts = [Draft(draft_id="DRF-STR", case_id=b.case.case_id, kind=DraftKind.STR, title="STR", body="x",
                      created_at=utcnow())]
    assert subject_parts(_clock(subject="HB01-1 (provisional: hold not yet executed)"), b) == (
        "HB01-1", "Provisional: hold not yet executed")
    assert subject_parts(_clock(subject="DRF-STR (provisional: counted from trace)"), b)[0] == "STR"
    assert subject_parts(_clock(subject=b.case.case_id), b) == ("Whole case", "")
    assert subject_parts(_clock(subject=b.case.ncrp_ids[0]), b)[0].startswith("NCRP ")
    assert subject_parts(_clock(subject=None), b) == ("Whole case", "")


# --------------------------------------------------------------------------- drafts helpers


def _report(checks: list[CheckResult]) -> VerificationReport:
    return VerificationReport(draft_id="D1", case_id="C1", passed=all(c.passed or c.severity == "warning"
                                                                      for c in checks),
                              checks=checks, verified_at=utcnow())


def test_draft_helpers():
    rep = _report([
        CheckResult(name="numeric", passed=False, message="amount_mismatch: ₹9.00 not in slots", spans=["₹9.00"]),
        CheckResult(name="citations", passed=False, severity="warning", message="citation_quote_missing: X",
                    spans=["[CASE-X]"]),
        CheckResult(name="policy", passed=True, message="ok: fine"),
    ])
    assert failing_spans(rep) == ["₹9.00"]
    assert warning_spans(rep) == ["[CASE-X]"]
    assert failing_spans(None) == [] and warning_spans(None) == []
    assert clean_message("ok: 12 slot(s) recomputed") == "12 slot(s) recomputed"
    assert clean_message("citation_quote_missing: no quote for X") == "No quote for X"
    assert clean_message("") == ""
    assert demote_headings("# Title\n## Sub\ntext #1") == "#### Title\n##### Sub\ntext #1"


def test_expected_drafts_follow_the_contract(bundles):
    exp = dict(expected_drafts(bundles[0].plan))
    # 4 customer-facing holds (A, B, D, L) + the pool account's amount hold (no customer notice)
    assert exp == {DraftKind.HOLD_INSTRUCTION: 5, DraftKind.CUSTOMER_NOTICE: 4, DraftKind.EDD_MEMO: 1,
                   DraftKind.OUTBOUND_REQUEST: 2, DraftKind.STR: 1, DraftKind.CFCFRMS_UPDATE: 1,
                   DraftKind.RESTORATION_PACK: 1}
    exp2 = dict(expected_drafts(bundles[1].plan))
    assert exp2[DraftKind.HOLD_INSTRUCTION] == 1 and DraftKind.EDD_MEMO not in exp2
    assert expected_drafts(None) == []


def test_hold_sentence_prefers_annexure_wording(bundles):
    plan = bundles[0].plan
    dec = next(d for d in plan.decisions if d.account_id == "HB01-1000000001")
    body = ("# Hold instruction: A/c No. HB01-1000000001\n\n- **Case:** X\n\n"
            "A notice has been received [BNSS-168]. As per the SOP, please put on hold the amount equivalent to "
            "the disputed amount received in A/c No. HB01-1000000001 vide UTR 425310000001 [SOP-9-1].\n\n"
            "| **Hold amount** | **₹43,200.00** |\n\nPlace the hold for ₹43,200.00 only.")
    d = Draft(draft_id="D", case_id="C", kind=DraftKind.HOLD_INSTRUCTION, account_id=dec.account_id,
              title="t", body=body, slots={"hold_amount": "₹43,200.00"}, created_at=utcnow())
    sentence = hold_sentence(d, dec)
    assert sentence.startswith("As per the SOP, please put on hold")
    assert "A/c No. HB01-1000000001" in sentence
    no_annex = d.model_copy(update={"body": "Intro. Place the hold for ₹43,200.00 only. Done."})
    assert hold_sentence(no_annex, dec) == "Place the hold for ₹43,200.00 only."
    assert sentences("# H\n| a |\n- **x** y. Z") == ["x y.", "Z"]


# --------------------------------------------------------------------------- KPI helpers


def test_kpi_formatting_and_targets():
    by_key = {m.key: m for m in METRICS}
    recall = by_key["traced_amount_recall"]
    assert fmt_metric("pct", 0.99) == "99%"
    assert fmt_metric("pct", 0.935) == "93.5%"
    assert fmt_metric("pct", 0.29) == "29%"
    assert fmt_metric("pct", 1.0) == "100%"
    assert fmt_metric("inr", 0) == "₹0"
    assert fmt_metric("sec", 0.12) == "0.12 s"
    assert fmt_metric("int", 3) == "3"
    assert fmt_metric("pct", None) == "—"
    assert fmt_metric("pct", "n/a") == "—"
    assert meets(recall, 0.99) is True and meets(recall, 0.5) is False
    assert meets(by_key["over_hold_inr"], 0.0) is True
    assert meets(by_key["injection_leaks"], 2) is False
    assert meets(by_key["disposition_accuracy"], 0.5) is None
    assert target_text(recall) == "≥ 95%"
    assert target_text(by_key["injection_leaks"]) == "0"
    assert target_text(by_key["p95_time_to_hold_plan_s"]).startswith("≤ 90")


def test_kpi_charts(sample_report):
    report = json.loads(sample_report.read_text(encoding="utf-8"))
    fig = role_chart(report["metrics"])
    assert [t.name for t in fig.data] == ["Precision", "Recall"]
    assert list(fig.data[0].x) == ["Mule", "Innocent holder", "Merchant"]
    assert role_chart({}) is None
    assert role_chart({"role_precision": "bad"}) is None
    typ = typology_chart(report["per_typology"])
    assert len(typ.data[0].y) == 2
    assert typology_chart({"T1": {"cases": 1}}) is None


# --------------------------------------------------------------------------- audit helpers


def test_audit_detail_summary():
    text = summarize_details({"reason_code": "CHECKS_PASSED", "kind": "HOLD_INSTRUCTION", "empty": "",
                              "timings": {"total_s": 0.4}, "ncrp_ids": ["1", "2"], "case_id": "CASE-1"},
                             limit=1000)
    assert "Reason code: Checks passed and matches the hold plan" in text
    assert "Kind: Hold instruction" in text
    assert "Empty" not in text
    assert "Ncrp ids: 1, 2" in text
    assert "Case id: CASE-1" in text
    long = summarize_details({"x": "y" * 500})
    assert len(long) <= 140 and long.endswith("…")
    assert re.search(r"\n", text) is None
