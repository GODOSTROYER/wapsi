"""CoCo / Claude Code PostToolUse hook (matcher `edit|write`): run the tests for the area that was just edited.

Reads the hook event JSON from stdin ({"tool_name", "tool_input", ...}), finds the edited file and maps it:

    src/wapsi/<area>/...                 -> tests/<area>  (or tests/test_<area>*.py)
    src/wapsi/{models,ports,config,...}  -> the whole suite (shared contracts)
    tests/**.py                          -> that test file
    evals/**, ui/**                      -> tests/evals, tests/ui (if present)
    editions/snowflake/**.py             -> tests/snowflake or editions/snowflake/tests (if present)
    .claude/skills/**                    -> scripts/sync_skills.py (keeps the .agents/skills mirror current)
    anything else                        -> nothing

Exit 0 when tests pass or nothing applies. Exit 2 with the failure tail on stderr when tests fail, so the agent sees
it (set WAPSI_HOOK_NONBLOCKING=1 to always exit 0). The exit-code contract follows the CoCo hooks doc
(0 allow, 2 block) and should be confirmed in the first CoCo session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
CONTRACT_FILES = {
    "models.py",
    "ports.py",
    "config.py",
    "money.py",
    "timeutil.py",
    "citations.py",
    "__init__.py",
}
PATH_KEYS = ("file_path", "path", "filePath", "filename", "file", "notebook_path", "target_file")
TIMEOUT_S = 240


def edited_paths(event: dict) -> list[Path]:
    tool_input = event.get("tool_input") or event.get("toolInput") or {}
    found: list[str] = []
    if isinstance(tool_input, dict):
        for key in PATH_KEYS:
            if isinstance(tool_input.get(key), str):
                found.append(tool_input[key])
        for edit in tool_input.get("edits", []) if isinstance(tool_input.get("edits"), list) else []:
            if isinstance(edit, dict):
                found += [edit[k] for k in PATH_KEYS if isinstance(edit.get(k), str)]
    out = []
    for raw in dict.fromkeys(found):
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        try:
            out.append(p.resolve().relative_to(ROOT))
        except ValueError:
            continue  # outside the repo
    return out


def targets_for(rel: Path) -> tuple[str, list[str]] | None:
    parts = rel.parts
    if parts[:2] == (".claude", "skills"):
        return "sync", []
    if rel.suffix != ".py":
        return None
    if parts[0] == "tests":
        return "pytest", [rel.as_posix()]
    if parts[:2] == ("src", "wapsi"):
        if len(parts) == 3 and parts[2] in CONTRACT_FILES:
            return "pytest", ["tests"]
        area = parts[2]
        if (ROOT / "tests" / area).is_dir():
            return "pytest", [f"tests/{area}"]
        files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob(f"test_{area}*.py"))
        return ("pytest", files) if files else ("pytest", ["tests"])
    if parts[0] in ("evals", "ui"):
        d = ROOT / "tests" / parts[0]
        return ("pytest", [f"tests/{parts[0]}"]) if d.is_dir() else None
    if parts[:2] == ("editions", "snowflake"):
        for d in (ROOT / "tests" / "snowflake", ROOT / "editions" / "snowflake" / "tests"):
            if d.is_dir():
                return "pytest", [d.relative_to(ROOT).as_posix()]
    return None


def main() -> int:
    nonblocking = os.environ.get("WAPSI_HOOK_NONBLOCKING") == "1"
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    jobs: dict[str, set[str]] = {}
    for rel in edited_paths(event):
        t = targets_for(rel)
        if t:
            jobs.setdefault(t[0], set()).update(t[1])
    if not jobs:
        return 0
    failed = []
    if "sync" in jobs:
        r = subprocess.run(
            [sys.executable, "scripts/sync_skills.py", "--quiet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print("skills mirrored to .agents/skills" if r.returncode == 0 else f"skill sync failed:\n{r.stderr}")
        if r.returncode:
            failed.append(r.stderr[-2000:])
    if jobs.get("pytest"):
        selection = sorted(jobs["pytest"])
        if "tests" in selection:
            selection = ["tests"]
        cmd = [sys.executable, "-m", "pytest", "-x", "--no-header", *selection]  # pyproject addopts adds -q
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print(f"pytest {' '.join(selection)}: timed out after {TIMEOUT_S}s (not blocking)")
            return 0
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-40:])
        summary = tail.splitlines()[-1] if tail else ""
        print(f"pytest {' '.join(selection)}: {summary}")
        if r.returncode not in (0, 5):  # 5 = no tests collected
            failed.append(tail)
    if failed and not nonblocking:
        print(
            "Tests failed after this edit. Fix them before continuing:\n" + "\n---\n".join(failed),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
