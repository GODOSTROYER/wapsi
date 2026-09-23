from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ui.components import formatting as f
from wapsi.models import (
    AccountType,
    ApportionmentRule,
    ApprovalDecision,
    CaseStatus,
    Channel,
    ClockKind,
    ClockStatus,
    Disposition,
    DraftKind,
    RestorationProcess,
    RiskLevel,
    Role,
    StrGround,
    TerminalKind,
)
from wapsi.timeutil import IST


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (123648.0, "₹1.24 L"),
        (50350000.0, "₹5.04 Cr"),
        (43200.0, "₹43,200"),
        (288000.0, "₹2.88 L"),
        (480000, "₹4.80 L"),
        (315648.0, "₹3.16 L"),
        (1152.0, "₹1,152"),
        (4800.0, "₹4,800"),
        (0, "₹0"),
        (100000, "₹1.00 L"),
        (99999.6, "₹1.00 L"),
        (9999500, "₹1.00 Cr"),
        (120000.0, "₹1.20 L"),
        (-43200.0, "-₹43,200"),
    ],
)
def test_inr_short(amount, expected):
    assert f.inr_short(amount) == expected


def test_inr_short_missing_values():
    assert f.inr_short(None) == "—"
    assert f.inr_short(float("nan")) == "—"


def test_inr_full_and_table():
    assert f.inr(480000) == "₹4,80,000.00"
    assert f.inr_table(43200.0) == "₹43,200"
    assert f.inr_table(1152.5) == "₹1,152.50"
    assert f.inr_table(None) == "—"


def test_pct_and_ratio():
    assert f.pct(0.96) == "96%"
    assert f.pct(0.935, 1) == "93.5%"
    assert f.ratio_text(50350000, 123648) == "407×"
    assert f.ratio_text(3, 2) == "1.5×"
    assert f.ratio_text(1, 0) == "—"


def test_ist_formatting():
    ts = datetime(2026, 9, 10, 11, 2, tzinfo=IST)
    assert f.ist(ts) == "10-09-2026 11:02 IST"
    assert f.ist_date(ts) == "10-09-2026"
    assert f.ist_clock(ts) == "11:02"
    assert f.ist(None) == "—"


def test_relative_due():
    now = datetime(2026, 9, 16, 12, 0, tzinfo=IST)
    assert f.relative_due(now + timedelta(days=3, hours=2), now) == "due in 3 days"
    assert f.relative_due(now + timedelta(days=1), now) == "due in 1 day"
    assert f.relative_due(now + timedelta(hours=5), now) == "due today"
    assert f.relative_due(now - timedelta(days=2, hours=1), now) == "overdue by 2 days"
    assert f.relative_due(now - timedelta(hours=3), now) == "overdue by less than a day"
    assert f.relative_past(now - timedelta(days=5), now) == "5 days ago"


def test_mask_name_and_mobile():
    assert f.mask_name("Meera Joshi") == "M•••• J••••"
    assert f.mask_name("") == ""
    assert f.mask_name(None) == "—"
    assert f.mask_mobile("9876543210") == "••••••••10"
    assert f.mask_mobile("98XXXXXX21") == "••••••••21"
    assert f.mask_mobile(None) == "—"


def test_mask_text_hides_names_and_mobiles():
    text = "Meera Joshi (98XXXXXX21) says MEERA JOSHI was called from 9876543210 about Ravi Kulkarni."
    out = f.mask_text(text, names=["Meera Joshi", "Ravi Kulkarni"], mobiles=["98XXXXXX21"])
    assert "Meera" not in out and "MEERA" not in out and "Ravi" not in out
    assert "9876543210" not in out and "98XXXXXX21" not in out
    assert "M•••• J••••" in out
    assert "••••••••10" in out
    # account numbers and UTRs are not mobile-shaped and stay readable
    assert f.mask_text("A/c HB01-1000000001 UTR 425310000001") == "A/c HB01-1000000001 UTR 425310000001"


def test_viewer_roles():
    auditor, analyst, supervisor = f.Viewer("auditor"), f.Viewer("analyst"), f.Viewer("supervisor")
    assert auditor.masked and not analyst.masked
    assert auditor.name("Meera Joshi") == "M•••• J••••"
    assert analyst.name("Meera Joshi") == "Meera Joshi"
    assert auditor.mobile("9876543210") == "••••••••10"
    assert supervisor.can_approve and not analyst.can_approve and not auditor.can_approve
    assert analyst.can_run and supervisor.can_run and not auditor.can_run
    assert analyst.actor.endswith("@hb01")
    assert auditor.text("Meera Joshi", ["Meera Joshi"]) == "M•••• J••••"
    assert analyst.text("Meera Joshi", ["Meera Joshi"]) == "Meera Joshi"


@pytest.mark.parametrize(
    "enum_cls",
    [Disposition, Role, DraftKind, ClockKind, ClockStatus, CaseStatus, ApprovalDecision, RestorationProcess,
     StrGround, ApportionmentRule, RiskLevel, AccountType, TerminalKind],
)
def test_every_enum_member_has_a_plain_label(enum_cls):
    for member in enum_cls:
        text = f.label(member)
        acronym = member.value.isalpha() and len(member.value) <= 4  # STR, UPI: official terms stay as-is
        assert text and (text != member.value or acronym), (enum_cls.__name__, member)
        assert "_" not in text


def test_label_specifics():
    assert f.label(Disposition.HOLD_AMOUNT) == "Hold victim amount"
    assert f.label(Role.MULE) == "Suspected mule"
    assert f.label(ApportionmentRule.PRO_RATA) == "Pro-rata · SOP Para 5(xiii)"
    assert f.label(Channel.UPI) == "UPI"
    assert f.label(Channel.CASH_DEPOSIT) == "Cash deposit"
    assert f.label(CaseStatus.PLANNED) == "Recommended — pending approval"
    assert f.label("FORWARD_HOLD_FIRST_LAYER").startswith("Forward hold")
    assert f.label("SOMETHING_NEW") == "Something new"
    assert f.label(None) == "—"
    assert f.reason_label("CHECKS_PASSED") == "Checks passed and matches the hold plan"
    assert f.audit_action_label("draft_approved") == "Draft approved"
    assert f.audit_action_label("mystery_event") == "Mystery event"
    assert f.check_label("injection") == "Untrusted text"


def test_no_ai_thinks_phrasing_in_labels():
    texts = [*f.CASE_STATUS_LABELS.values(), *f.DISPOSITION_LABELS.values(), *f.REASON_CODE_LABELS.values()]
    assert not any("AI thinks" in t or "the AI" in t for t in texts)


def test_short_account():
    assert f.short_account("HB01-1000000001") == "HB01 ···0001"
    assert f.short_account(None) == "—"
