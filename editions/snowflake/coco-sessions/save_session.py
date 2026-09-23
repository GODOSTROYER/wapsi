"""Save a CoCo session transcript as hackathon evidence, with secrets redacted, and index it.

CoCo CLI stores sessions under ~/.snowflake/cortex/conversations/ (SNOWFLAKE_HOME overrides ~/.snowflake);
the session id is printed at start and exit. Automation runs: export first with
`cortex conversations transcript <thread_id> > run.txt` and pass --source run.txt.

    uv run python editions/snowflake/coco-sessions/save_session.py --session latest --step S05 --slug trace-proc
    uv run python editions/snowflake/coco-sessions/save_session.py --session 3f9c --step S15 --slug actions-drain \
        --gate G2 --note "drained 3 actions to Jira via MCP"
    uv run python editions/snowflake/coco-sessions/save_session.py --source run.txt --step A01 --slug nightly-eval

Writes transcripts/<YYYY-MM-DD>_<step>_<slug>.<ext> (or a folder) and appends a row to INDEX.md.
Always open the saved file and check it before committing: redaction is pattern-based.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log", ".yaml", ".yml", ""}
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<jwt>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)(\"?(?:password|passwd|token|secret|client_secret|api[_-]?key|private_key|pat)\"?\s*[:=]\s*\"?)"
            r"[^\"\s,}]{4,}"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)\b(?:[a-z0-9_-]+\.)+snowflakecomputing\.com"),
        "<account>.snowflakecomputing.com",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)(C:(?:\\\\|\\|/)Users(?:\\\\|\\|/))[^\\/\"\s]+"), r"\1<user>"),
    (re.compile(r"(/home/|/Users/)[^/\"\s]+"), r"\1<user>"),
    (re.compile(r"\b(?:xox[abposr]|xapp)-[A-Za-z0-9-]{10,}"), "<slack-token>"),
    (re.compile(r"\bATATT[A-Za-z0-9_=-]{20,}"), "<atlassian-token>"),
]


def conversations_dir() -> Path:
    base = Path(os.environ.get("SNOWFLAKE_HOME") or Path.home() / ".snowflake")
    return base / "cortex" / "conversations"


def find_session(session: str) -> Path:
    root = conversations_dir()
    if not root.is_dir():
        raise SystemExit(f"no CoCo conversations folder at {root}; pass --source <file>")
    entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    if session == "latest":
        if not entries:
            raise SystemExit(f"{root} is empty")
        return max(entries, key=lambda p: p.stat().st_mtime)
    matches = [p for p in entries if session in p.name]
    if len(matches) != 1:
        names = ", ".join(sorted(p.name for p in matches)[:10]) or "none"
        raise SystemExit(f"session id {session!r} matched {len(matches)} entries in {root}: {names}")
    return matches[0]


def redact(text: str) -> tuple[str, int]:
    total = 0
    for pattern, repl in REDACTIONS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


def copy_redacted(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in TEXT_SUFFIXES:
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(src, dst)
            return 0
        clean, n = redact(text)
        dst.write_text(clean, encoding="utf-8", newline="")
        return n
    shutil.copyfile(src, dst)
    return 0


def append_index(index: Path, row: list[str]) -> None:
    if not index.exists():
        index.write_text(
            "# CoCo session evidence index\n\n"
            "| Date | Gate | Step | Slug | Transcript | Note |\n|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    with index.open("a", encoding="utf-8") as fh:
        fh.write("| " + " | ".join(c.replace("|", "/") for c in row) + " |\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src_group = ap.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--session", help="CoCo session id (or a unique part of it), or 'latest'")
    src_group.add_argument("--source", help="explicit transcript file or folder")
    ap.add_argument("--step", required=True, help="playbook step id, e.g. S05 (A01.. for automation runs)")
    ap.add_argument("--slug", required=True, help="short kebab-case label, e.g. trace-proc")
    ap.add_argument("--gate", default="", help="G0 / G1 / G2 / finale")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", default=str(HERE), help="evidence folder (default: this folder)")
    args = ap.parse_args(argv)

    if not re.fullmatch(r"[A-Z]\d{2}", args.step) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.slug):
        print("--step must look like S05 and --slug like trace-proc")
        return 2
    src = Path(args.source) if args.source else find_session(args.session)
    if not src.exists():
        print(f"not found: {src}")
        return 2
    out = Path(args.out)
    stem = f"{date.today():%Y-%m-%d}_{args.step}_{args.slug}"
    redactions = 0
    if src.is_dir():
        dst = out / "transcripts" / stem
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            redactions += copy_redacted(f, dst / f.relative_to(src))
    else:
        dst = out / "transcripts" / f"{stem}{src.suffix}"
        redactions += copy_redacted(src, dst)
    rel = dst.relative_to(out).as_posix()
    append_index(
        out / "INDEX.md",
        [f"{date.today():%Y-%m-%d}", args.gate, args.step, args.slug, f"[{rel}]({rel})", args.note],
    )
    print(f"saved {src.name} -> {dst} ({redactions} redaction(s)); indexed in {out / 'INDEX.md'}")
    print("Review the file before committing (pattern-based redaction).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
