"""CoCo hook wiring: the PreToolUse matcher must select CoCo's real SQL/shell tool names, and the SQL guard must
block a destructive statement delivered in CoCo's tool-event shape."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "editions" / "snowflake" / "templates" / "settings.json"
GUARD = ROOT / "editions" / "snowflake" / "hooks" / "pre_sql_guard.py"


def coco_matches(matcher: str, tool_name: str) -> bool:
    """CoCo CLI 1.1.87 matcher semantics: '*'/'' match all, else anchored case-insensitive regex."""
    if not matcher or matcher == "*" or matcher.lower() == tool_name.lower():
        return True
    return re.fullmatch(f"(?:{matcher})", tool_name, re.I) is not None


def _matcher(event: str) -> str:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"][event][0]["matcher"]


def test_pre_tool_use_matcher_selects_sql_and_shell_tools():
    m = _matcher("PreToolUse")
    for tool in ("snowflake_sql_execute", "sql_execute", "bash", "Bash"):
        assert coco_matches(m, tool), tool
    for tool in ("write", "edit", "read", "snowflake_sql_execute_extra"):
        assert not coco_matches(m, tool), tool


def test_post_tool_use_matcher_selects_edits_only():
    m = _matcher("PostToolUse")
    assert coco_matches(m, "edit") and coco_matches(m, "write") and coco_matches(m, "apply_patch")
    assert not coco_matches(m, "snowflake_sql_execute")


def _run_guard(event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD)], input=json.dumps(event), capture_output=True, text=True, timeout=30
    )


def test_guard_blocks_drop_schema_from_sql_tool():
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "snowflake_sql_execute",
        "tool_input": {"sql": "DROP SCHEMA WAPSI.CASEMGMT", "description": "cleanup"},
    }
    res = _run_guard(event)
    assert res.returncode == 2
    assert json.loads(res.stdout.splitlines()[0])["decision"] == "block"


def test_guard_blocks_audit_log_delete_from_shell_tool():
    event = {
        "tool_name": "bash",
        "tool_input": {"command": 'snow sql -q "DELETE FROM WAPSI.CASEMGMT.AUDIT_LOG"'},
    }
    assert _run_guard(event).returncode == 2


def test_guard_allows_select():
    event = {
        "tool_name": "snowflake_sql_execute",
        "tool_input": {"sql": "SELECT COUNT(*) FROM WAPSI.CORE.TXN", "description": "count"},
    }
    assert _run_guard(event).returncode == 0
