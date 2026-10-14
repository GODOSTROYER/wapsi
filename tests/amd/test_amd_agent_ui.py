"""The "Ask the agent" box in the Case Room (AMD edition) and the Agent trace tab on runs it produced (offline)."""

from __future__ import annotations

import html
import re

import pytest

pytest.importorskip("streamlit", reason="ui extra not installed")
pytest.importorskip("google.adk", reason="agent extra not installed (uv sync --extra agent)")
pytest.importorskip("litellm", reason="agent extra not installed (uv sync --extra agent)")

from ui.components import agent_chat  # noqa: E402
from ui.components import tool_trace as tt  # noqa: E402
from wapsi.agenttrace import traces_from_audit  # noqa: E402
from wapsi.fixtures import showcase as sc  # noqa: E402

CASE1 = f"CASE-{sc.N1}"
PIPELINE = ["open_case", "trace_money", "hold_plan", "verification_report", "draft_documents"]


def _text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setenv("WAPSI_AGENT_BACKEND", "offline")


# --------------------------------------------------------------------------- pure builders


def test_status_chips_name_the_framework_model_platform_and_tool_mode():
    live = _text(agent_chat.status_html({"available": True, "backend": "endpoint", "model": "Gemma-4-31B-IT",
                                         "provider": "AMD Token Factory", "api_key_set": True,
                                         "tool_mode": "native", "base_url": "https://developer.amd.com.cn/radeon/api/v1"}))
    for part in ("Google ADK agent", "Gemma-4-31B-IT", "AMD Token Factory", "native tool calls"):
        assert part in live
    assert "No API key" not in live
    offline = _text(agent_chat.status_html({"available": True, "backend": "offline", "tool_mode": None}))
    assert "Offline: scripted planner behind a fake OpenAI-compatible endpoint" in offline
    assert "probed on first use" in offline
    assert "JSON tool protocol" in _text(agent_chat.status_html({"available": True, "backend": "endpoint",
                                                                 "tool_mode": "json", "api_key_set": False}))
    down = agent_chat.status_html({"available": False, "reason": "google-adk not installed; uv sync --extra agent"})
    assert "Agent unavailable" in _text(down) and "uv sync --extra agent" in html.unescape(down)
    assert agent_chat.tool_mode_label("auto (not probed yet)") == "tool mode: probed on first use"


def test_reply_chips_say_how_the_reply_was_checked():
    ok = _text(agent_chat.reply_meta_html({"reply_source": "model", "grounded": True, "tools": PIPELINE,
                                           "blocked": 0, "wall_ms": 2140.0}))
    assert "Reply grounded" in ok and "5 tool calls: open_case → trace_money" in ok and "2.1 s" in ok
    replaced = _text(agent_chat.reply_meta_html({"reply_source": "tool_results", "grounded": False,
                                                 "tools": ["hold_plan"], "blocked": 0, "wall_ms": 300.0}))
    assert "Model reply withheld" in replaced and "1 tool call: hold_plan" in replaced and "300 ms" in replaced
    stopped = _text(agent_chat.reply_meta_html({"reply_source": "model", "grounded": None, "tools": ["approve_draft"],
                                                "blocked": 1}))
    assert "no figures to check" in stopped and "1 blocked" in stopped
    assert "Notice" in _text(agent_chat.reply_meta_html({"reply_source": "notice"}))


def test_suggestions_come_from_the_case_in_view(bundles_by_id):
    asks = agent_chat.suggestions(bundles_by_id[CASE1])
    assert asks[0] == f"Open case {sc.N1}"
    assert asks[1].startswith("Why is HB01-") and CASE1 in asks[1]
    assert agent_chat.suggestions(None) == []
    assert not agent_chat.agent_capable(object())  # other editions' data sources: the box is not drawn


@pytest.fixture()
def bundles_by_id():
    from wapsi.fixtures.bundles import all_showcase_bundles

    return {b.case.case_id: b for b in all_showcase_bundles()}


def test_run_header_names_the_model_and_where_it_is_served(agent_runtime):
    trace = agent_runtime().run(f"Open case {sc.N1}", user_id="asha").trace
    head = _text(tt.run_header_html(trace, 1))
    assert "Offline ADK run (fake OpenAI-compatible endpoint, no API key)" in head
    assert "Gemma-4-31B-IT · offline fake endpoint" in head and "5 tool calls" in head
    live = trace.model_copy(update={"source": "adk-endpoint", "provider": "AMD Token Factory"})
    head = _text(tt.run_header_html(live, 1))
    assert "Live ADK run (open model on an OpenAI-compatible endpoint)" in head
    assert "Gemma-4-31B-IT · AMD Token Factory" in head
    assert "character-count stand-ins (offline fake endpoint)" in _text(tt.summary_html([trace]))
    assert "as reported by the model" in _text(tt.summary_html([live]))


# --------------------------------------------------------------------------- data source


def test_ui_source_runs_the_agent_and_shows_the_sealed_trace(demo_services, offline):
    from editions.amd.app.ui_source import AmdDataSource

    ds = AmdDataSource(demo_services, seed_limit=2)
    assert agent_chat.agent_capable(ds)
    status = ds.agent_status()
    assert status["available"] is True and status["backend"] == "offline"
    assert not traces_from_audit(ds.get_bundle(CASE1).audit, case_id=CASE1)

    out = ds.ask_agent(CASE1, "Show the hold plan.", actor="nodal.officer@hb01", role="analyst")
    assert out.ok and out.data.tools == ["hold_plan"] and out.data.grounded is True
    assert "₹1,42,848.00" in out.message and "Policy check: passed" in out.message
    bundle = ds.get_bundle(CASE1)  # refreshed from the store: the page shows the run at once
    traces, origin = tt.case_traces(bundle)
    assert origin == tt.ORIGIN_AUDIT and [t.summary().tools for t in traces] == [["hold_plan"]]
    assert tt.trace_origin(traces[0], origin, tt.sealed_trace_ids(bundle)) == tt.ORIGIN_AUDIT  # sealed, not just logged
    assert tt.tab_label(bundle) == "Agent trace · 1" and ds.verify_audit(CASE1).ok
    assert out.bundle is bundle and ds.agent_status()["tool_mode"] == "native"

    assert not ds.ask_agent(CASE1, "Show the hold plan.", actor="auditor@hb01", role="auditor").ok
    assert len(traces_from_audit(ds.get_bundle(CASE1).audit, case_id=CASE1)) == 1  # the auditor ran nothing


def test_ui_source_reports_a_missing_extra(demo_services, monkeypatch):
    from editions.amd.app import agent as agent_mod
    from editions.amd.app.ui_source import AmdDataSource

    monkeypatch.setattr(agent_mod, "missing_dependencies", lambda: ["litellm"])
    ds = AmdDataSource(demo_services, seed_limit=1)
    assert ds.agent_status()["available"] is False
    out = ds.ask_agent(CASE1, "hi", actor="a", role="analyst")
    assert not out.ok and out.unavailable and "uv sync --extra agent" in out.message


# --------------------------------------------------------------------------- the page


def _page(at) -> str:
    return "\n".join(html.unescape(m.value) for m in at.markdown)


def test_case_room_chat_asks_the_agent_and_the_trace_tab_shows_the_run(tmp_path, amd_dir, offline):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from editions.amd.app import agent as agent_mod
    from editions.amd.app.services import build_services, set_services
    from editions.amd.app.settings import AmdSettings
    from editions.shared import context

    svc = build_services(AmdSettings(demo_mode=True, state_dir=str(tmp_path), ui_seed_limit=2), search=False)
    set_services(svc)
    try:
        st.cache_resource.clear()
        at = AppTest.from_file(str(amd_dir / "app" / "ui_app.py"), default_timeout=240)
        at.session_state["case_id"] = CASE1
        at.session_state["page"] = "Case Room"
        at.session_state["role"] = "analyst"
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        page = _page(at)
        assert "Ask the agent" in page and "Google ADK agent" in page and "Offline: scripted planner" in page
        assert [t.label for t in at.tabs][:2] == ["Money trail", "Agent trace · 8"]  # the recorded run, until asked
        suggested = [b for b in at.button if str(b.key).startswith("agent_suggest_")]
        assert suggested and suggested[0].label == f"Open case {sc.N1}"

        at.text_input(key="agent_chat_text").set_value("Show the hold plan.")
        next(b for b in at.button if b.label == "Ask the agent").click()
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        page = _page(at)
        assert "Show the hold plan." in page and "₹1,42,848.00" in page and "Policy check: passed" in page
        assert "Reply grounded" in page and "1 tool call: hold_plan" in page
        assert [t.label for t in at.tabs][1] == "Agent trace · 1"  # the live run replaced the recording
        assert "Offline ADK run (fake OpenAI-compatible endpoint, no API key)" in page
        assert "In the case's hash-chained audit log" in page and "Recorded offline run." not in page
        assert not [b for b in at.button if str(b.key).startswith("agent_suggest_")]  # the conversation started
        assert svc.store.verify_audit(CASE1).ok

        at.session_state["_agent_chat_pending"] = "Search the SOP for the forward hold rule."  # a queued question
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert "From the corpus" in _page(at) or "search_sop" in _page(at)

        at.session_state["role"] = "auditor"
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert "Auditors have read-only access" in _page(at)
        assert all(t.key != "agent_chat_text" for t in at.text_input)
    finally:
        st.cache_resource.clear()
        set_services(None)
        agent_mod.set_runtime(None)
        context.set_services(None)
