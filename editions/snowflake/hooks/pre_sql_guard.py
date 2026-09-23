"""CoCo PreToolUse guardrail (matcher `snowflakesqlexecute|bash`): block destructive, costly or governance-breaking SQL.

Blocks (exit 2, reason on stderr and as {"decision": "block"} on stdout):
  - DROP DATABASE / DROP SCHEMA; DROP or TRUNCATE of WAPSI.CASEMGMT tables          (override: WAPSI_ALLOW_DESTRUCTIVE=1)
  - any DELETE / UPDATE / TRUNCATE / DROP on CASEMGMT.AUDIT_LOG or APPROVALS         (append-only; no override)
  - UPDATE of CASEMGMT.HOLD_PLAN amounts by hand (rebuild with BUILD_HOLD_PLAN)      (no override)
  - GRANT on WAPSI.ML (ground truth) to an app role                                  (no override)
  - warehouse sizes above SMALL; AUTO_SUSPEND above 300 s                            (override: WAPSI_ALLOW_COST=1)
  - ALTER ACCOUNT other than CORTEX_ENABLED_CROSS_REGION / AI_SETTINGS               (override: WAPSI_ALLOW_DESTRUCTIVE=1)
  - CREATE EXTERNAL ACCESS INTEGRATION (trial accounts have no egress; stage actions in CASEMGMT.ACTIONS)
                                                                                     (override: WAPSI_ALLOW_EGRESS=1)
The CoCo tool name (`snowflakesqlexecute`) and its input key are taken from the CoCo tools doc; the script accepts
`sql`, `query`, `statement` or `command` keys. Confirm in the first session with a harmless `DROP SCHEMA` dry test.
"""

from __future__ import annotations

import json
import os
import re
import sys

INPUT_KEYS = ("sql", "query", "statement", "statements", "command", "cmd")
APP_ROLES = r"WAPSI_(?:ANALYST|SUPERVISOR|AUDITOR|LEA_READONLY)"
BIG_SIZES = r"(?:MEDIUM|LARGE|XLARGE|X-LARGE|XXLARGE|X2LARGE|2X-LARGE|XXXLARGE|X3LARGE|3X-LARGE|X4LARGE|4X-LARGE|X5LARGE|X6LARGE)"

RULES: list[tuple[str, re.Pattern[str], str | None, str]] = [
    (
        "drop-db-schema",
        re.compile(r"\bDROP\s+(?:DATABASE|SCHEMA)\b"),
        "WAPSI_ALLOW_DESTRUCTIVE",
        "DROP DATABASE/SCHEMA is blocked in CoCo sessions.",
    ),
    (
        "casemgmt-destroy",
        re.compile(
            r"\b(?:DROP\s+TABLE\s+|TRUNCATE\s+(?:TABLE\s+)?)(?:IF\s+EXISTS\s+)?(?:WAPSI\.)?CASEMGMT\."
        ),
        "WAPSI_ALLOW_DESTRUCTIVE",
        "Case-management tables hold case history; do not drop or truncate them.",
    ),
    (
        "audit-append-only",
        re.compile(
            r"\b(?:DELETE\s+FROM\s+|UPDATE\s+|TRUNCATE\s+(?:TABLE\s+)?(?:IF\s+EXISTS\s+)?"
            r"|DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?|MERGE\s+INTO\s+)"
            r"(?:WAPSI\.)?(?:CASEMGMT\.)?(?:AUDIT_LOG|APPROVALS)\b"
        ),
        None,
        "AUDIT_LOG and APPROVALS are append-only (SOP 5(ix) accountability).",
    ),
    (
        "hand-edited-hold",
        re.compile(r"\bUPDATE\s+(?:WAPSI\.)?(?:CASEMGMT\.)?HOLD_PLAN\b"),
        None,
        "Hold amounts come from data. Rebuild with CALL WAPSI.CASEMGMT.BUILD_HOLD_PLAN(...) instead of UPDATE.",
    ),
    (
        "ground-truth-grant",
        re.compile(r"\bGRANT\b[^;]*\bWAPSI\.ML\b[^;]*\bTO\s+ROLE\s+" + APP_ROLES),
        None,
        "WAPSI.ML holds ground truth; it must never be granted to app roles.",
    ),
    (
        "warehouse-size",
        re.compile(r"\bWAREHOUSE_SIZE\s*=\s*'?" + BIG_SIZES + r"\b"),
        "WAPSI_ALLOW_COST",
        "Keep warehouses at XSMALL/SMALL (cost guardrail).",
    ),
    (
        "auto-suspend",
        re.compile(r"\bAUTO_SUSPEND\s*=\s*(?:30[1-9]|3[1-9]\d|[4-9]\d{2}|[1-9]\d{3,}|0\b|NULL\b)"),
        "WAPSI_ALLOW_COST",
        "AUTO_SUSPEND must be 60-300 seconds (cost guardrail).",
    ),
    (
        "alter-account",
        re.compile(r"\bALTER\s+ACCOUNT\b(?![^;]*\b(?:CORTEX_ENABLED_CROSS_REGION|AI_SETTINGS)\b)"),
        "WAPSI_ALLOW_DESTRUCTIVE",
        "Only CORTEX_ENABLED_CROSS_REGION and AI_SETTINGS may be changed at account level.",
    ),
    (
        "egress",
        re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?EXTERNAL\s+ACCESS\s+INTEGRATION\b"),
        "WAPSI_ALLOW_EGRESS",
        "Trial accounts have no external network access. Stage the call in WAPSI.CASEMGMT.ACTIONS for the drain runner.",
    ),
]


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def extract_sql(event: dict) -> str:
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    if isinstance(tool_input, str):
        return tool_input
    parts = []
    for key in INPUT_KEYS:
        v = tool_input.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts += [s for s in v if isinstance(s, str)]
    return "\n".join(parts)


def evaluate(sql: str, env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    env = os.environ if env is None else env
    text = " ".join(strip_comments(sql).upper().split())
    hits = []
    for name, pattern, override, reason in RULES:
        if pattern.search(text) and not (override and env.get(override) == "1"):
            hits.append(
                (name, reason + (f" (override: set {override}=1 for this session)" if override else ""))
            )
    return hits


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    sql = extract_sql(event)
    if not sql.strip():
        return 0
    hits = evaluate(sql)
    if not hits:
        if re.search(r"\bUSE\s+ROLE\s+ACCOUNTADMIN\b", sql, re.I):
            print("note: ACCOUNTADMIN in use — switch back to WAPSI_ADMIN after this step.")
        return 0
    reason = "; ".join(f"[{n}] {r}" for n, r in hits)
    print(json.dumps({"decision": "block", "reason": reason}))
    print(f"Blocked by Wapsi SQL guardrail: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
