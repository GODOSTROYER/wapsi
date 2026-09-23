"""Mirror .claude/skills/* to .agents/skills/* so Gemini CLI (and other Agent Skills readers) load the same skills.

Claude Code and Snowflake CoCo read .claude/skills; Gemini CLI reads .agents/skills. The source of truth is
.claude/skills. The sync is idempotent: files are copied only when their bytes differ, files deleted from a
source skill are deleted from its mirror, and mirrored skills removed from the source are removed from
.agents/skills (tracked in .agents/skills/.sync-manifest.json). Skills that exist only in .agents/skills are
never touched.

    uv run python scripts/sync_skills.py            # sync
    uv run python scripts/sync_skills.py --check    # exit 1 if the mirror is stale (CI / hooks); writes nothing
    uv run python scripts/sync_skills.py -n         # dry run: print what would change

Each SKILL.md must start with YAML frontmatter holding only `name` (= directory name, lowercase letters, digits,
hyphens, <= 64 chars) and `description` (non-empty, <= 1024 chars), so every tool can load it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".claude" / "skills"
DST = ROOT / ".agents" / "skills"
MANIFEST = ".sync-manifest.json"
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_KEYS = {"name", "description"}


def frontmatter_errors(skill_dir: Path) -> list[str]:
    path = skill_dir / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return [f"{path}: missing YAML frontmatter"]
    try:
        _, block, _ = text.split("---", 2)
    except ValueError:
        return [f"{path}: unterminated frontmatter"]
    keys: dict[str, str] = {}
    for line in block.strip().splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return [f"{path}: unparseable frontmatter line {line!r}"]
        keys[key.strip()] = value.strip()
    errors = []
    extra = set(keys) - ALLOWED_KEYS
    if extra:
        errors.append(f"{path}: frontmatter keys {sorted(extra)} not allowed (only name, description)")
    name = keys.get("name", "")
    if name != skill_dir.name:
        errors.append(f"{path}: name {name!r} must equal the directory name {skill_dir.name!r}")
    if not NAME_RE.match(name) or len(name) > 64:
        errors.append(f"{path}: name {name!r} must be lowercase letters/digits/hyphens, <= 64 chars")
    desc = keys.get("description", "")
    if not desc:
        errors.append(f"{path}: description is empty")
    elif len(desc) > 1024:
        errors.append(f"{path}: description is {len(desc)} chars (max 1024)")
    return errors


def skill_files(skill_dir: Path) -> dict[str, Path]:
    out = {}
    for p in sorted(skill_dir.rglob("*")):
        rel = p.relative_to(skill_dir)
        if p.is_dir() or set(rel.parts) & IGNORED_DIRS or p.suffix in IGNORED_SUFFIXES:
            continue
        out[rel.as_posix()] = p
    return out


def plan_sync() -> tuple[list[tuple[str, Path, Path | None]], list[str], list[str]]:
    """Return (actions, synced skill names, validation errors). action = (op, target, source)."""
    actions: list[tuple[str, Path, Path | None]] = []
    errors: list[str] = []
    skills = []
    for skill_dir in sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "SKILL.md").exists()):
        errs = frontmatter_errors(skill_dir)
        if errs:
            errors += errs
            continue
        skills.append(skill_dir.name)
        target_dir = DST / skill_dir.name
        src_files = skill_files(skill_dir)
        for rel, src in src_files.items():
            dst = target_dir / rel
            if not dst.exists():
                actions.append(("add", dst, src))
            elif dst.read_bytes() != src.read_bytes():
                actions.append(("update", dst, src))
        if target_dir.exists():
            for rel, dst in skill_files(target_dir).items():
                if rel not in src_files:
                    actions.append(("delete", dst, None))
    previous = []
    manifest = DST / MANIFEST
    if manifest.exists():
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8")).get("skills", [])
        except json.JSONDecodeError:
            previous = []
    source_names = {p.name for p in SRC.iterdir() if p.is_dir()}
    for name in previous:
        if name not in source_names and (DST / name).exists():
            actions.append(("remove-skill", DST / name, None))
    manifest_text = json.dumps({"source": ".claude/skills", "skills": skills}, indent=2) + "\n"
    if not manifest.exists() or manifest.read_text(encoding="utf-8") != manifest_text:
        actions.append(("manifest", manifest, None))
    return actions, skills, errors


def apply(actions, skills: list[str]) -> None:
    for op, target, source in actions:
        if op in ("add", "update"):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        elif op == "delete":
            target.unlink()
        elif op == "remove-skill":
            shutil.rmtree(target)
        elif op == "manifest":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"source": ".claude/skills", "skills": skills}, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
    # drop directories left empty by deletions
    if DST.exists():
        for d in sorted((p for p in DST.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if anything would change; write nothing")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print planned changes; write nothing")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not SRC.is_dir():
        print(f"no source skills directory: {SRC}")
        return 2
    actions, skills, errors = plan_sync()
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    if not args.quiet or args.check or args.dry_run:
        for op, target, _ in actions:
            print(f"{op:<13} {target.relative_to(ROOT).as_posix()}")
    if args.check or args.dry_run:
        if not args.quiet:
            print(f"{len(actions)} change(s) pending; {len(skills)} valid skill(s).")
        return 1 if (args.check and (actions or errors)) else (1 if errors else 0)
    apply(actions, skills)
    if not args.quiet:
        state = f"{len(actions)} change(s) applied" if actions else "already in sync"
        print(f"{state}: {len(skills)} skill(s) mirrored to {DST.relative_to(ROOT).as_posix()}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
