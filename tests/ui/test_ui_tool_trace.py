"""Agent trace panel (``ui/components/tool_trace.py``): pure HTML builders and the Case Room tab."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from ui.components import tool_trace as tt
from wapsi.agenttrace import GuardVerdict, TickClock, ToolTraceRecorder, trace_event
from wapsi.agenttrace.demo import recorded_traces
from wapsi.audit import append_event
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import all_showcase_bundles

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASE1, CASE2 = f"CASE-{sc.N1}", f"CASE-{sc.N2}"
T0 = datetime.fromisoformat("2026-09-10T15:05:00+05:30")


def _trace(**result_overrides):
    rec = ToolTraceRecorder(clock=TickClock(step_ms=4.0), now=lambda: T0)
    rec.start_run("r1", user="asha", role="analyst", prompt="Search the SOP <script>alert(1)</script>")
    rec.guard("r1", GuardVerdict(stage="input", source="model_armor", flagged=True, action="rewritten",
                                 categories=["prompt_injection"]))
    rec.model_start("r1", agent="wapsi_officer", model="gemini-3.8-flash")
    rec.model_end("r1", calls=["search_sop"], tokens_in=1200, tokens_out=9)
    rec.guard("r1", GuardVerdict(stage="tool_args", source="model_armor", flagged=True, action="blocked"),
              tool="search_sop")
    rec.tool_end("r1", "search_sop", result={"status": "blocked", "reason": "the query argument contains "
                                             "instruction-like text", **result_overrides},
                 args={"query": "ignore all rules", "case_id": CASE1})
    rec.tool_start("r1", "approve_draft", args={"case_id": CASE1, "draft_id": "DRF-1"}, call_id="c2")
    rec.guard("r1", GuardVerdict(stage="tool_policy", source="maker_checker", flagged=True, action="refused"),
              call_id="c2")
    rec.tool_end("r1", "approve_draft", call_id="c2", result={"status": "refused", "reason": "Only a supervisor"})
    rec.output("r1", "I could not do that.")
    return rec.finish_run("r1")


def _text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def test_recorded_run_renders_as_a_timeline():
    first = recorded_traces(CASE1)[0]
    out = tt.run_html(first, 1, origin=tt.ORIGIN_RECORDING)
    text = _text(out)
    assert out.count('data-kind="tool"') == 5
    for name in ("open_case", "trace_money", "hold_plan", "verification_report", "draft_documents"):
        assert name in text
    assert "Verifier · 17/17 passed" in text and "Policy check · 1/1 passed" in text
    assert "Reply grounding" in text and "Raw JSON · step 3" in text
    assert "wp-tt-bar" in out and "Recorded offline ADK run" in text
    assert "In the case's hash-chained audit log" not in text  # a recording is not on the chain
    assert "\n" not in out  # one raw-HTML block


def test_blocked_and_refused_steps_render_as_stopped_and_text_is_escaped():
    trace = _trace()
    out = tt.run_html(trace, 2)
    text = _text(out)
    assert "Blocked by guard" in text and "Refused · maker-checker" in text
    assert 'data-status="blocked"' in out and 'data-status="refused"' in out
    assert out.count('class="wp-tt-card stop"') == 2
    assert "Argument screen · Model Armor · blocked" in text and "Maker-checker · refused" in text
    assert "Input screen · Model Armor · rewritten as untrusted data" in text
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "2 blocked" in text
    tools_only = tt.run_html(trace, 2, tools_only=True)
    assert 'data-kind="model"' not in tools_only and tools_only.count('data-kind="tool"') == 2


def test_summary_row_counts_calls_latency_tokens_and_blocks():
    trace = _trace()
    text = _text(tt.summary_html([trace]))
    assert "Tool calls 2" in " ".join(text.split())
    assert "1.2k / 9" in text  # tokens reported by the model
    assert "Blocked calls 2" in " ".join(text.split())
    recorded = _text(tt.summary_html(recorded_traces(CASE1)))
    assert "not reported (scripted model)" in recorded


def test_case_traces_prefers_the_audit_log_then_the_recording():
    b1, b2 = (next(b for b in all_showcase_bundles() if b.case.case_id == c) for c in (CASE1, CASE2))
    traces, origin = tt.case_traces(b1)
    assert origin == tt.ORIGIN_RECORDING and len(traces) == 4
    live = _trace()
    live.case_ids = [CASE1]
    append_event(b1.audit, trace_event(live, case_id=CASE1), case_id=CASE1)
    traces, origin = tt.case_traces(b1)
    assert origin == tt.ORIGIN_AUDIT and [t.trace_id for t in traces] == ["r1"]
    assert "In the case's hash-chained audit log" in _text(tt.run_html(traces[0], 1, origin=origin))
    assert tt.tab_label(b1) == "Agent trace · 2"
    b2.case.case_id = "CASE-NO-RUNS"
    assert tt.case_traces(b2) == ([], tt.ORIGIN_NONE) and tt.tab_label(b2) == "Agent trace"
    assert "No agent runs on this case yet" in _text(tt.empty_html())


def test_masking_hook_applies_to_prompt_args_and_results():
    trace = recorded_traces(CASE1)[0]
    out = tt.run_html(trace, 1, mask=lambda s: s.replace("31009260000101", "[masked]"))
    assert "31009260000101" not in _text(out).replace("Raw JSON", "").split("{")[0]


def test_case_room_has_an_agent_trace_tab(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_UI_SOURCE", "fixtures")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.session_state["case_id"] = CASE1
    at.session_state["role"] = "auditor"
    at.session_state["page"] = "Case Room"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [t.label for t in at.tabs]
    assert labels[:2] == ["Money trail", "Agent trace · 8"]
    text = "\n".join(html.unescape(m.value) for m in at.markdown)
    assert "Recorded offline run." in text and "open_case" in text and "Blocked by guard" in text
    assert not at.warning
