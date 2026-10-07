"""UI copy and panels for the legal-robustness features: labels for the new roles / dispositions / drafts, the
simulated-CFCFRMS disclaimer, the exceptional account-level hold (maker-checker) and the police / LEA request check."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.components import formatting as f
from ui.components.theme import DISPOSITION_TONE, ROLE_TONE
from ui.components.trail_dot import GRAPH_ROLE
from ui.views.hold_plan import split_refs
from wapsi.fixtures import showcase as sc
from wapsi.models import AccountLevelReason, Disposition, DraftKind, Role

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASE1 = "CASE-31009260000101"
REQUEST_ID = f"DRF-{CASE1}-ACCOUNT_LEVEL_HOLD_REQUEST-{sc.A}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_UI_SOURCE", "fixtures")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    monkeypatch.delenv("WAPSI_EVAL_REPORT", raising=False)


def _app(role: str = "analyst", page: str = "Hold Plan", case_id: str = CASE1) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.session_state["case_id"] = case_id
    at.session_state["role"] = role
    at.session_state["page"] = page
    return at.run()


def _text(at: AppTest) -> str:
    parts = [html.unescape(m.value) for m in at.markdown]
    parts += [str(x.value) for x in (*at.success, *at.info, *at.warning, *at.error)]
    return "\n".join(parts)


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert "This page couldn't load" not in _text(at)


# --------------------------------------------------------------------------- labels (no raw enums in the UI)


def test_every_role_disposition_and_draft_kind_has_a_label_and_tone():
    for r in Role:
        assert r in f.ROLE_LABELS and r in ROLE_TONE and r in GRAPH_ROLE, r
    for d in Disposition:
        assert d in f.DISPOSITION_LABELS and d in DISPOSITION_TONE, d
    for k in DraftKind:
        assert k in f.DRAFT_KIND_LABELS, k
    assert f.ROLE_LABELS[Role.BONA_FIDE_RECIPIENT] == "Possible innocent holder · bona fide recipient"
    assert f.ROLE_LABELS[Role.UNWITTING_CONDUIT] == "Possible innocent holder · unwitting conduit"
    assert f.label(Role.INNOCENT_HOLDER) == f.ROLE_LABELS[Role.BONA_FIDE_RECIPIENT]
    assert f.DISPOSITION_LABELS[Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD] == "Account-level hold · approved exception"


def test_reason_codes_and_audit_actions_have_labels():
    for r in AccountLevelReason:
        assert f.reason_label(r.value) == f.ACCOUNT_LEVEL_REASON_LABELS[r]
    assert f.reason_label("STR_RECOMMENDED_OFFICER_DECISION") == (
        "STR recommended — no automatic STR; the officer decides")
    assert f.reason_label("UNWITTING_CONDUIT").startswith("Unwitting conduit")
    for code in ("OFFICER_REQUESTED_EXCEPTION", "SUPERVISOR_APPROVED_EXCEPTION"):
        assert f.reason_label(code) != f.humanize(code)
    for action in ("account_level_hold_requested", "account_level_hold_applied", "lea_request_checked"):
        assert f.audit_action_label(action) != f.humanize(action)
    assert f.SIMULATED_CFCFRMS_NOTE.startswith("Simulated CFCFRMS interface")


def test_account_level_request_items_carry_the_requester():
    from ui.views.approvals import items_for
    from wapsi.adapters.llm_fake import FakeLLM
    from wapsi.pipeline.run import process_case, request_account_level_hold

    ledger = sc.showcase_ledger()
    b = process_case(ledger, [sc.N1], llm=FakeLLM())
    req = request_account_level_hold(b, ledger, sc.A, [AccountLevelReason.IDENTITY_THEFT_ACCOUNT.value],
                                     requested_by="asha", role="analyst")
    items = {it.key: it for it in items_for(b)}
    assert items[req.draft_id].requested_by == "asha"  # never swept into a bulk approval
    assert items[req.draft_id].kind == "Account-level hold request"
    assert all(it.requested_by == "" for k, it in items.items() if k != req.draft_id)


def test_split_refs():
    assert split_refs("425310000001, 622220000102;TX-1  TX-2") == ["425310000001", "622220000102", "TX-1", "TX-2"]
    assert split_refs("") == []


# --------------------------------------------------------------------------- hold plan panels


@pytest.mark.parametrize("role", ["analyst", "supervisor", "auditor"])
def test_hold_plan_shows_the_exception_and_lea_panels(role):
    at = _app(role)
    _ok(at)
    text = _text(at)
    assert "Exceptional account-level hold" in text and "Holds are amount-specific by default" in text
    assert "Police / LEA hold request" in text
    assert f.SIMULATED_CFCFRMS_NOTE in text  # showcase 1 requests two other banks through CFCFRMS
    if role == "auditor":
        assert "Auditors can see exceptions but not request them." in text
        assert not [b for b in at.button if b.key in ("al_send", "lea_check_btn")]
    else:
        assert at.button(key="al_send").disabled  # no recorded reason yet
        assert set(at.selectbox(key="al_account").options) == {sc.A, sc.B, sc.D, sc.L}


def test_officer_requests_and_a_different_supervisor_approves():
    at = _app("analyst")
    at.selectbox(key="al_account").set_value(sc.A)
    at.multiselect(key="al_reasons").set_value([AccountLevelReason.FRAUDSTER_CONTROLLED_ACCOUNT]).run()
    assert not at.button(key="al_send").disabled
    at.button(key="al_send").click().run()
    _ok(at)
    assert "is waiting for a supervisor in Approvals" in _text(at)

    at.selectbox(key="role").set_value("supervisor").run()
    at.radio(key="page").set_value("Approvals").run()
    _ok(at)
    assert "Approving applies an exceptional account-level hold" in _text(at)
    at.button(key=f"record_{REQUEST_ID}").click().run()
    _ok(at)
    at.radio(key="page").set_value("Hold Plan").run()
    _ok(at)
    text = _text(at)
    assert "Applied exception." in text and "Account wholly controlled by the fraudster" in text
    assert f.DISPOSITION_LABELS[Disposition.EXCEPTIONAL_ACCOUNT_LEVEL_HOLD] in text


def test_lea_request_check_shows_the_limited_hold_and_the_letter():
    at = _app("analyst")
    at.text_input(key="lea_ncrp").set_value(sc.N1)
    at.text_input(key="lea_account").set_value(sc.A)
    at.number_input(key="lea_amount").set_value(480000.0)
    at.checkbox(key="lea_whole").check()
    at.button(key="lea_check_btn").click().run()
    _ok(at)
    text = _text(at)
    assert "Hold limited to" in text and "₹43,200" in text
    assert "Reply to hold request" in text and "clarification needed" in text
    assert "Name and rank of the requesting officer" in text
    assert "The request asks for action on the whole account" in text


def test_drafts_page_marks_cfcfrms_drafts_as_simulated():
    at = _app("analyst", page="Drafts & Verification")
    _ok(at)
    if any(b.key == "drafts_generate" for b in at.button):  # fixture bundles start without drafts
        at.button(key="drafts_generate").click().run()
        _ok(at)
    labels = [t.label for t in at.tabs]
    assert any(lab.startswith("CFCFRMS update") for lab in labels), labels
    assert f.SIMULATED_CFCFRMS_NOTE in _text(at)  # every tab's content is rendered
