"""The tamper drill (``uv run python -m wapsi.audit drill``) catches every tampering attempt."""

from __future__ import annotations

import json

import pytest

from wapsi.audit.__main__ import main
from wapsi.audit.chain import CONTENT, HEAD, LINK, SEQUENCE, TRUNCATED
from wapsi.audit.drill import render, run_drill


@pytest.fixture(scope="module")
def drill():
    return run_drill()


def test_clean_log_verifies_and_every_tamper_is_detected(drill):
    assert drill.clean.ok and drill.clean.anchored and drill.events == drill.clean.length
    assert drill.passed
    by_name = {r.name: r for r in drill.results}
    expected = {"edit": CONTENT, "edit+rehash": LINK, "delete": SEQUENCE, "reorder": SEQUENCE,
                "truncate": TRUNCATED, "rechain": HEAD}
    assert {n: r.code for n, r in by_name.items()} == expected
    assert all(r.detected for r in drill.results)
    k = drill.approval_seq
    assert by_name["edit"].first_bad_seq == k and by_name["edit+rehash"].first_bad_seq == k + 1
    assert by_name["delete"].first_bad_seq == k and by_name["reorder"].first_bad_seq == k - 1
    # the log alone catches edits, deletions and reorders; the anchor is what catches truncation and re-hashing
    assert {r.name for r in drill.results if not r.detected_without_anchor} == {"truncate", "rechain"}


def test_editing_one_stored_event_is_detected_at_that_event(drill):
    assert not drill.stored_edit.ok and drill.stored_edit.code == CONTENT
    assert drill.stored_edit.first_bad_seq == drill.approval_seq
    assert len(drill.approval_fingerprint) == 64


def test_render_is_plain_ascii_and_reports_pass(drill):
    text = render(drill)
    text.encode("ascii")  # Windows consoles: no characters outside ASCII
    assert "6/6 tampering attempts detected" in text and text.rstrip().endswith("PASS")


def test_cli_drill_exit_code_and_json(capsys):
    assert main(["drill", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True and len(out["results"]) == 6 and out["clean"]["ok"] is True


def test_cli_verify_accepts_an_event_list(tmp_path, drill, capsys):
    from wapsi.audit.chain import append_event
    from wapsi.models import AuditEvent
    from wapsi.timeutil import utcnow

    events: list[AuditEvent] = []
    for i in range(3):
        append_event(events, AuditEvent(at=utcnow(), actor="a", role="analyst", action="viewed",
                                        object_type="page", object_id=f"p{i}"), case_id="CASE-X")
    path = tmp_path / "events.json"
    path.write_text(json.dumps([e.model_dump(mode="json") for e in events]), encoding="utf-8")
    assert main(["verify", str(path)]) == 0
    assert "Chain intact: 3 events" in capsys.readouterr().out
    del events[1]
    path.write_text(json.dumps([e.model_dump(mode="json") for e in events]), encoding="utf-8")
    assert main(["verify", str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == SEQUENCE
