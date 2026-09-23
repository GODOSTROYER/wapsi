"""Smoke tests: the app and every view render for both showcase cases and all three roles."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui.data_source as data_source
from ui.views.common import PAGES
from wapsi.fixtures import showcase as sc

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASES = ["CASE-31009260000101", "CASE-31109260000102"]
ROLES = ["analyst", "supervisor", "auditor"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")  # surface exceptions instead of the friendly error card
    monkeypatch.setenv("WAPSI_UI_SOURCE", "fixtures")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    monkeypatch.delenv("WAPSI_EVAL_REPORT", raising=False)


def _app(case_id: str = CASES[0], role: str = "analyst", page: str = "Inbox") -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["case_id"] = case_id
    at.session_state["role"] = role
    at.session_state["page"] = page
    return at.run()


def _text(at: AppTest) -> str:
    parts = [html.unescape(m.value) for m in at.markdown]
    parts += [str(x.value) for x in (*at.success, *at.info, *at.warning, *at.error)]
    for sb in at.selectbox:
        parts += [str(o) for o in sb.options]
    for el in at.get("graphviz_chart"):
        parts.append(el.proto.spec)
    return "\n".join(parts)


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert "This page couldn't load" not in _text(at)


def _go(at: AppTest, page: str) -> AppTest:
    return at.radio(key="page").set_value(page).run()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case_id", CASES)
def test_every_view_renders(case_id, role):
    at = _app(case_id, role)
    _ok(at)
    for page in PAGES:
        _go(at, page)
        _ok(at)
        assert at.session_state["page"] == page
        assert not at.warning, (page, [w.value for w in at.warning])


def test_inbox_lists_both_cases_and_opens_the_case_room():
    at = _app()
    text = _text(at)
    assert "Inbox" in text and "Meera Joshi" in text and "Arjun Nair" in text
    at.button(key=f"open_{CASES[1]}").click().run()
    _ok(at)
    assert at.session_state["page"] == "Case Room"
    assert at.session_state["case_id"] == CASES[1]
    assert "Case room" in _text(at)


def test_case_room_shows_trail_totals_and_inspector():
    at = _app(page="Case Room")
    _ok(at)
    graphs = at.get("graphviz_chart")
    assert len(graphs) == 1
    spec = graphs[0].proto.spec
    for acct in sc.SHOWCASE_1["layers"]:
        assert f'"{acct}"' in spec
    text = _text(at)
    assert "Pro-rata · SOP Para 5(xiii)" in text
    assert "₹4.80 L" in text
    assert "Why this recommendation" in text
    assert len(at.get("plotly_chart")) == 1
    # selecting another account highlights it and updates the inspector
    at.selectbox(key="inspect_account").set_value(sc.L).run()
    _ok(at)
    assert f'"{sc.L}" [' in at.get("graphviz_chart")[0].proto.spec
    assert "Possible innocent holder" in _text(at)


def test_hold_plan_shows_baseline_strip_and_filters():
    at = _app(page="Hold Plan")
    _ok(at)
    text = _text(at)
    assert "A whole-account freeze would immobilise ₹5.04 Cr across 6 accounts" in text
    assert "only the victim's money" in text
    at.multiselect(key="plan_groups").set_value(["Other-bank requests"]).run()
    _ok(at)
    assert sc.C in _text(at) and "Suspected mule" in _text(at)
    at.multiselect(key="plan_groups").set_value([]).run()
    assert "Nothing matches this filter" in _text(at)


def test_auditor_sees_masked_personal_details():
    at = _app(role="auditor", page="Case Room")
    _ok(at)
    text = _text(at)
    for secret in ("Meera Joshi", "Ravi Kulkarni", "Imran Sayyed", "98XXXXXX21"):
        assert secret not in text, secret
    assert "M•••• J••••" in text
    _go(at, "Audit")
    assert "Masked view" in _text(at)


def test_analyst_sees_names_but_cannot_approve():
    at = _app(role="analyst", page="Approvals")
    _ok(at)
    assert "View only" in _text(at)
    assert not [b for b in at.button if b.key == "record_plan"]
    _go(at, "Case Room")
    assert "Meera Joshi" in _text(at)


def test_supervisor_decision_is_recorded_and_audited():
    at = _app(role="supervisor", page="Approvals")
    _ok(at)
    at.button(key="record_plan").click().run()
    _ok(at)
    assert any("Approved" in s.value for s in at.success)
    text = _text(at)
    assert "supervisor@hb01" in text and "Checks passed and matches the hold plan" in text
    _go(at, "Audit")
    _ok(at)
    assert "Hold plan approved" in _text(at)


def test_supervisor_can_request_changes_with_reason():
    at = _app(case_id=CASES[1], role="supervisor", page="Approvals")
    at.radio(key="dec_plan").set_value(at.radio(key="dec_plan").options[1]).run()
    at.text_input(key="note_plan").input("Check the FIR details").run()
    at.button(key="record_plan").click().run()
    _ok(at)
    assert any("change request" in s.value for s in at.success)
    at.radio(key="appr_filter").set_value("All items").run()
    assert "Changes requested" in _text(at)


def test_role_switch_is_audited():
    at = _app()
    at.selectbox(key="role").set_value("supervisor").run()
    _go(at, "Audit")
    _ok(at)
    text = _text(at)
    assert "Switched role" in text and "To: Supervisor" in text


def test_drafts_empty_state_and_friendly_message_without_engine(monkeypatch):
    def _missing(module, attr):
        raise ImportError(module)

    monkeypatch.setattr(data_source, "_optional", _missing)
    at = _app(page="Drafts & Verification")
    _ok(at)
    assert "Drafts not generated yet" in _text(at)
    at.button(key="drafts_generate").click().run()
    _ok(at)
    assert any("isn't switched on" in i.value for i in at.info)
    _go(at, "Clocks")
    _ok(at)
    assert "No clocks running yet" in _text(at)


def test_auditor_cannot_generate_drafts():
    at = _app(role="auditor", page="Drafts & Verification")
    _ok(at)
    button = at.button(key="drafts_generate")
    assert button.disabled


def test_kpis_empty_state(monkeypatch, workdir):
    monkeypatch.setenv("WAPSI_EVAL_REPORT", str(workdir / "missing.json"))
    at = _app(page="KPIs")
    _ok(at)
    assert "No evaluation report yet" in _text(at)
    assert "uv run python -m evals.harness" in _text(at)


def test_kpis_render_a_report(monkeypatch, sample_report):
    monkeypatch.setenv("WAPSI_EVAL_REPORT", str(sample_report))
    at = _app(page="KPIs")
    _ok(at)
    text = _text(at)
    for expected in ("Traced-amount recall", "99%", "Injection leaks", "Brand new metric", "C-007",
                     "Whole-account freeze vs Wapsi"):
        assert expected in text, expected
    assert len(at.get("plotly_chart")) == 2


def test_kpis_broken_report_shows_error_state(monkeypatch, workdir):
    bad = workdir / "latest.json"
    bad.write_text("{oops", encoding="utf-8")
    monkeypatch.setenv("WAPSI_EVAL_REPORT", str(bad))
    at = _app(page="KPIs")
    _ok(at)
    assert "couldn't be read" in _text(at)


def test_page_errors_show_a_recoverable_error_state(monkeypatch):
    import ui.views.kpis as kpis

    def boom(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.delenv("WAPSI_UI_RAISE")
    monkeypatch.setattr(kpis, "render", boom)
    at = _app(page="KPIs")
    assert not at.exception
    assert "This page couldn't load" in _text(at)
    assert at.button(key="page_retry")


def test_live_pipeline_source_renders(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_SOURCE", "pipeline")
    at = _app(role="supervisor")
    _ok(at)
    for page in ("Case Room", "Hold Plan", "Drafts & Verification", "Approvals", "Clocks"):
        _go(at, page)
        _ok(at)


def test_deep_link_opens_a_page():
    at = AppTest.from_file(str(APP), default_timeout=60)
    if not hasattr(at, "query_params"):
        pytest.skip("AppTest query params not supported on this Streamlit version")
    at.query_params["page"] = "hold-plan"
    at.query_params["role"] = "auditor"
    at.run()
    _ok(at)
    assert at.session_state["page"] == "Hold Plan"
    assert at.session_state["role"] == "auditor"
