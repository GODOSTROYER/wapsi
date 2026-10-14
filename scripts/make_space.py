"""Assemble the Hugging Face Space folder for the public demo, so all that is left is to push it.

    uv run python scripts/make_space.py --out ../wapsi-space          # build the folder
    uv run python scripts/make_space.py --out ../wapsi-space --force  # rebuild it (keeps its .git)
    uv run python scripts/make_space.py --check                       # verify the sources only; write nothing

A Docker Space builds the ``Dockerfile`` at the root of its own repository, so the Space cannot point at
``deploy/demo/Dockerfile`` inside this one. This script copies exactly what that Dockerfile reads (its ``COPY``
lines are the list; nothing else from the repository goes to the Space) and adds three files:

- ``Dockerfile``     a copy of ``deploy/demo/Dockerfile`` (same paths: the Space folder is its build context)
- ``README.md``      the Space card: the YAML block Spaces read (``sdk: docker``, ``app_port: 7860``) and a short
                     description (the Dockerfile copies it too: the package metadata names it as the readme)
- ``.gitattributes`` Git LFS rules for the binary files (the synthetic voice notes), which the Hub requires

What never goes in: ``.env`` files, ``secrets.toml``, the raw legal documents and the docs search index (their text
carries no open licence and a Space repository is public; the image builds the index itself from the public
sources). ``--with-docs-index`` copies a locally built index anyway, for a **private** Space only. Only files
tracked by git are copied, and the result is scanned for things that look like keys before it is reported as ready.

The model endpoint is configured in the Space's settings, never in the folder: ``WAPSI_LLM_API_KEY`` as a secret,
``WAPSI_LLM_BASE_URL`` and ``WAPSI_LLM_MODEL`` as variables. Without them the Space shows the recorded run.
Full steps: ``deploy/DEMO_HOSTING.md``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = "deploy/demo/Dockerfile"
APP_PORT = 7860
GENERATED = ("Dockerfile", "README.md", ".gitattributes")
# never copied, whatever the Dockerfile lists (posix paths relative to the repository root)
NEVER = ("data/docs/raw/", "data/docs/index/text/", ".streamlit/secrets.toml")
DOCS_INDEX = "data/docs/index/"
VOICE_SAMPLES = "data/samples/voice/"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
SKIP_NAMES = {".env", "secrets.toml", ".DS_Store"}
LFS_PATTERNS = ("*.wav", "*.mp3", "*.m4a", "*.ogg", "*.webm", "*.png", "*.jpg", "*.pdf", "*.parquet")
# shapes of real keys (the scan is a last line of defence, not a licence to keep keys near the repository)
SECRET_PATTERNS = {
    "AMD Token Factory key": re.compile(r"\brc-[A-Za-z0-9_-]{24,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    "Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SECRET_ENVS = ("WAPSI_LLM_API_KEY", "RADEON_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "HF_TOKEN")

CARD = """\
---
title: {title}
emoji: 🏦
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: {port}
pinned: true
license: mit
short_description: Hold only the victim's money, not the whole account
tags:
  - amd
  - gemma
  - fraud
  - streamlit
---

# Wapsi (वापसी): money-trail and victim-restoration copilot

A copilot for a bank's cyber-fraud nodal officer. A digital-payment fraud freezes honest merchants' and customers'
whole accounts: the kirana shop that received one tainted payment loses its account. Wapsi traces the stolen money
through the mule layers, works out how much of it sits in each account, and plans **amount-specific holds** instead
of whole-account freezes, so commerce keeps running and the bank keeps its customers. It drafts the paperwork,
re-checks every number and citation, and waits for a supervisor to approve. Every event lands in a hash-chained
audit log. **All data here is synthetic.**

## What you are looking at

- **Open the Case Room** on the showcase case: add `?page=case-room&case=CASE-31009260000101` to the Space URL.
- **Numbers come from data, words come from the model.** Amounts, account numbers, UTRs and dates are slots filled
  from the ledger; the model writes prose around placeholders only, and a separate verifier recomputes every number.
- **Which model.** The chip on the Drafts page names the model and the endpoint host that wrote each case's prose,
  for example "Gemma 4 on AMD via developer.amd.com.cn". The Space calls an OpenAI-compatible endpoint (the AMD Token
  Factory, or a vLLM server on AMD) when `WAPSI_LLM_BASE_URL`, `WAPSI_LLM_API_KEY` and `WAPSI_LLM_MODEL` are set in
  its settings. Without them the page says "Recorded run — live AMD endpoint not configured" and replays prose
  recorded from an open model served on AMD; the numbers are still computed live.
- **Who is a mule** is decided by a transparent scorer and hard rules, never by a language model.
- **Your own copy.** Each browser session works on its own copy of the cases; "Start over" in the sidebar resets it.

Source, tests and evaluation: {repo}
"""


@dataclass
class SpaceReport:
    out: Path
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def copy_sources(dockerfile_text: str) -> list[str]:
    """The build-context paths the Dockerfile copies (``COPY <src>... <dest>``; ``--from=`` stages are images)."""
    out: list[str] = []
    for line in dockerfile_text.splitlines():
        parts = line.split()
        if parts[:1] != ["COPY"] or any(p.startswith("--from") for p in parts):
            continue
        out += [p.rstrip("/") for p in parts[1:-1] if not p.startswith("--")]
    return out


def tracked_files(root: Path) -> set[str] | None:
    """Files tracked by git (posix paths), or ``None`` outside a work tree: only tracked files go to the Space, so
    a stray local file (a key, a scratch note) cannot ride along."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run([git, "-C", str(root), "ls-files", "-c", "-z"], capture_output=True, check=True,
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return {p for p in done.stdout.decode("utf-8", "replace").split("\0") if p}


def _wanted(rel: str, *, with_docs_index: bool, voice_samples: bool) -> bool:
    parts = rel.split("/")
    if SKIP_PARTS & set(parts) or parts[-1] in SKIP_NAMES or parts[-1].startswith(".env"):
        return False
    if rel.endswith((".pyc", ".pyo")) or any(rel.startswith(p) or rel == p.rstrip("/") for p in NEVER):
        return False
    if rel.startswith(DOCS_INDEX) and not with_docs_index:
        return False
    if rel.startswith(VOICE_SAMPLES) and not voice_samples and rel.endswith((".wav", ".mp3", ".m4a", ".ogg")):
        return False
    return True


def source_files(root: Path, sources: list[str], *, with_docs_index: bool = False,
                 voice_samples: bool = True) -> tuple[list[str], list[str]]:
    """(files to copy, sources that are missing), both as posix paths relative to ``root``."""
    tracked = tracked_files(root)
    files: list[str] = []
    missing: list[str] = []
    for src in sources:
        path = root / src
        if path.is_file():
            found = [src]
        elif path.is_dir():
            found = sorted(p.relative_to(root).as_posix() for p in path.rglob("*") if p.is_file())
        else:
            missing.append(src)
            continue
        kept = []
        for rel in found:
            is_index = rel.startswith(DOCS_INDEX)  # never tracked: allowed only on request
            if tracked is not None and rel not in tracked and not (is_index and with_docs_index):
                continue
            if _wanted(rel, with_docs_index=with_docs_index, voice_samples=voice_samples):
                kept.append(rel)
        if not kept:
            missing.append(src)
        files += kept
    return sorted(dict.fromkeys(files)), missing


def space_readme(title: str = "Wapsi", repo: str = "https://github.com/<you>/wapsi") -> str:
    return CARD.format(title=title, port=APP_PORT, repo=repo)


def gitattributes() -> str:
    lines = ["# binary files go through Git LFS / Xet (the Hub rejects them in plain git)"]
    lines += [f"{pat} filter=lfs diff=lfs merge=lfs -text" for pat in LFS_PATTERNS]
    return "\n".join(lines) + "\n"


def scan_for_secrets(root: Path, files: list[str]) -> list[str]:
    """Findings such as ``path: looks like a Google API key``. Also catches the literal value of a key that is set
    in this process's environment. Values are never echoed."""
    literal = {name: os.environ[name].strip() for name in SECRET_ENVS if len(os.environ.get(name, "").strip()) >= 12}
    out: list[str] = []
    for rel in files:
        path = root / rel
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                out.append(f"{rel}: looks like a {label}")
        out += [f"{rel}: contains the value of ${name}" for name, value in literal.items() if value in text]
    return out


def build_space(out: Path, *, root: Path = ROOT, title: str = "Wapsi", repo: str = "https://github.com/<you>/wapsi",
                with_docs_index: bool = False, voice_samples: bool = True, force: bool = False,
                check: bool = False) -> SpaceReport:
    """Assemble the Space folder at ``out`` (``check``: verify only). An existing folder is rebuilt only with
    ``force`` (its ``.git`` directory is kept, so a cloned Space repository stays a clone)."""
    report = SpaceReport(out=out)
    dockerfile = root / DOCKERFILE
    if not dockerfile.is_file():
        report.problems.append(f"{DOCKERFILE} is missing")
        return report
    sources = copy_sources(dockerfile.read_text(encoding="utf-8"))
    files, missing = source_files(root, sources, with_docs_index=with_docs_index, voice_samples=voice_samples)
    report.problems += [f"the Dockerfile copies {m}, which has no tracked file to copy" for m in missing]
    for name in GENERATED:
        files = [f for f in files if f != name]  # written below
    report.problems += scan_for_secrets(root, files)
    report.files = sorted([*files, *GENERATED])
    report.bytes = sum((root / f).stat().st_size for f in files)
    if check or report.problems:
        return report
    out = out.resolve()
    if out == root.resolve() or out in root.resolve().parents:
        report.problems.append("--out must not be the repository itself or a folder that contains it")
        return report
    if out.exists() and any(p.name != ".git" for p in out.iterdir()):
        if not force:
            report.problems.append(f"{out} is not empty (pass --force to rebuild it; its .git is kept)")
            return report
        for p in out.iterdir():
            if p.name == ".git":
                continue
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    out.mkdir(parents=True, exist_ok=True)
    for rel in files:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dest)
    (out / "Dockerfile").write_bytes(dockerfile.read_bytes())
    (out / "README.md").write_text(space_readme(title, repo), encoding="utf-8", newline="\n")
    (out / ".gitattributes").write_text(gitattributes(), encoding="utf-8", newline="\n")
    report.bytes += sum((out / n).stat().st_size for n in GENERATED)
    report.out = out
    return report


NEXT_STEPS = """\
Next (you run these; nothing was uploaded):
  1. Create the Space once: https://huggingface.co/new-space  ->  SDK "Docker", template "Blank", hardware "CPU basic"
     (free, no card).
  2. Upload this folder (handles the binary files for you):
       uv tool run --from huggingface_hub hf auth login
       uv tool run --from huggingface_hub hf upload <user>/<space> "{out}" . --repo-type space
     (or: git clone https://huggingface.co/spaces/<user>/<space>, build into that clone with --force, then
      git lfs install && git add -A && git commit -m "Wapsi demo" && git push)
  3. Space -> Settings -> Variables and secrets (never in a file):
       secret    WAPSI_LLM_API_KEY    your AMD Token Factory key
       variable  WAPSI_LLM_BASE_URL   https://developer.amd.com.cn/radeon/api/v1
       variable  WAPSI_LLM_MODEL      Gemma-4-31B-IT
       variable  WAPSI_LLM_MODELS     Gemma-4-31B-IT,DeepSeek-V4-Flash   (fail over while one answers in garbage)
     Without them the Space runs in recorded mode and says so.
  4. Judged URL: https://<user>-<space>.hf.space/?page=case-room&case=CASE-31009260000101
"""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT.parent / "wapsi-space", help="the Space folder to write")
    ap.add_argument("--title", default="Wapsi")
    ap.add_argument("--repo", default="https://github.com/<you>/wapsi", help="public source repository (for the card)")
    ap.add_argument("--force", action="store_true", help="rebuild a folder that is not empty (its .git is kept)")
    ap.add_argument("--check", action="store_true", help="verify the sources and the secret scan; write nothing")
    ap.add_argument("--with-docs-index", action="store_true",
                    help="also copy a locally built data/docs/index (PRIVATE Spaces only: the documents' text "
                         "carries no open licence)")
    ap.add_argument("--no-voice-samples", action="store_true", help="leave out the synthetic voice notes (5 MB)")
    args = ap.parse_args(argv)
    report = build_space(args.out, title=args.title, repo=args.repo, with_docs_index=args.with_docs_index,
                         voice_samples=not args.no_voice_samples, force=args.force, check=args.check)
    if not report.ok:
        for p in report.problems:
            print(f"ERROR: {p}", file=sys.stderr)
        return 1
    verb = "would hold" if args.check else "holds"
    where = "the Space folder" if args.check else str(report.out)
    print(f"{where} {verb} {len(report.files)} files, {report.bytes / 1e6:.1f} MB; no key-shaped strings found")
    if not args.check:
        print(NEXT_STEPS.format(out=report.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
