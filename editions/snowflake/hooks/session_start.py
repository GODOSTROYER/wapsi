"""CoCo / Claude Code SessionStart hook: print the latest golden-set KPIs and the next hackathon gate.

Configured in .cortex/settings.json (template: editions/snowflake/templates/settings.json):
    uv run python editions/snowflake/hooks/session_start.py

Keeps output to a few lines (token budget). Never fails: always exits 0.
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
KPI_SCRIPT = ROOT / ".claude" / "skills" / "wapsi-eval" / "scripts" / "kpi_summary.py"
GATES = [
    (date(2026, 9, 18), "G0 setup: CoCo trial, WAPSI DB/schemas, v0 data loaded, skills scaffolded"),
    (date(2026, 9, 27), "G1 Snowflake golden path on 5 showcase cases in SiS"),
    (date(2026, 10, 3), "G2 CoCo submission (hard deadline 04-10-2026 23:59 IST)"),
    (date(2026, 10, 27), "CoCo live finale window 27-30 Oct (if shortlisted)"),
]


def kpi_line() -> str:
    if not KPI_SCRIPT.exists():
        return "Wapsi KPIs: wapsi-eval skill not found"
    sys.path.insert(0, str(KPI_SCRIPT.parent))
    sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
    try:
        import kpi_summary

        buf = io.StringIO()
        with redirect_stdout(buf):
            kpi_summary.main(["--brief"])
        return buf.getvalue().strip()
    except Exception as exc:  # a hook must never break the session
        return f"Wapsi KPIs: unavailable ({type(exc).__name__}: {exc})"


def gate_line(today: date) -> str:
    for d, label in GATES:
        if d >= today:
            days = (d - today).days
            return f"Next gate: {label} — {d:%d-%m-%Y} ({'today' if days == 0 else f'in {days} day(s)'})"
    return "All hackathon gates have passed."


def skills_line() -> str:
    sync = ROOT / "scripts" / "sync_skills.py"
    if not sync.exists():
        return ""
    try:
        rc = subprocess.run(
            [sys.executable, str(sync), "--check", "--quiet"], cwd=ROOT, capture_output=True, timeout=20
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return "" if rc == 0 else "Skills mirror is stale: run `uv run python scripts/sync_skills.py`."


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    lines = [kpi_line(), gate_line(date.today()), skills_line()]
    lines.append(
        "Skills: $synth-fraud-ledger $money-trail-tracer $lien-calculator $str-drafter $sop-citer $wapsi-eval"
    )
    print("\n".join(line for line in lines if line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
