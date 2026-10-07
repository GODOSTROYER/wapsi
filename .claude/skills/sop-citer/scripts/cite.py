"""Look up, search and validate Wapsi citation ids (SOP / BNSS / PML Rules / RBI / FIU / court cases).

Part of the `sop-citer` skill. Run from the repo root.

    uv run python .claude/skills/sop-citer/scripts/cite.py SOP-9-2 SOP-5-XII          # look up ids
    uv run python .claude/skills/sop-citer/scripts/cite.py --list                    # every known id
    uv run python .claude/skills/sop-citer/scripts/cite.py --search "merchant goods delivered hold" -k 3
    uv run python .claude/skills/sop-citer/scripts/cite.py --check "Hold per [SOP-9-1] and [SOP-99-Z]"

Only ids in wapsi.citations.KNOWN_IDS may be cited. Quotes are capped at 25 words.
Exit codes: 0 ok, 1 unknown id / no supporting text, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from wapsi.citations import KNOWN_IDS, registry  # noqa: E402

MAX_WORDS = 25
TOKEN_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\]")
ACCOUNT_RE = re.compile(r"[A-Z]{2}\d{2}-\d{10}")


def clip(text: str, max_words: int = MAX_WORDS) -> str:
    words = " ".join(text.split()).split(" ")
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " …"


def lookup(ids: list[str]) -> int:
    reg = registry()
    missing = 0
    for cid in ids:
        c = reg.get(cid)
        if c is None:
            missing += 1
            near = [k for k in sorted(KNOWN_IDS) if k.split("-")[0] == cid.split("-")[0]][:8]
            print(
                f"- [{cid}] UNKNOWN — not in wapsi.citations; do not cite it. Known ids with this prefix: "
                f"{', '.join(near) or 'none'}"
            )
            continue
        page = f", p. {c.page}" if c.page else ""
        print(f"- [{c.citation_id}] {c.source_title} — {c.section}{page}")
        if c.quote:
            print(f'  Quote (≤{MAX_WORDS} words): "{clip(c.quote)}"')
        else:
            print(
                "  Quote: not available yet (data/docs/citations.json has no entry). Cite the id and section "
                "title only; do not write a quotation."
            )
        if c.url:
            print(f"  Source: {c.url}")
    return 1 if missing else 0


def list_ids() -> int:
    for cid, c in sorted(registry().items()):
        print(f"{cid:<22} {c.section}")
    return 0


def search(query: str, k: int) -> int:
    try:
        from wapsi.search.index import LocalDocSearch

        ds = LocalDocSearch.load()
    except Exception as exc:
        print(
            f"local doc index unavailable ({type(exc).__name__}: {exc}). Build it with "
            "`uv run python -m wapsi.search.build`."
        )
        return 1
    hits = ds.search(query, k=k)
    if not hits:
        print("No supporting passage found. Say so; do not guess a clause.")
        return 1
    for h in hits:
        cid = h.citation_id if h.citation_id in KNOWN_IDS else None
        label = f"[{cid}]" if cid else "(no citation id — context only, do not cite)"
        print(f"- {label} {h.doc_id} · {h.section} · score {h.score:.3f}")
        print(f'  "{clip(h.text)}"')
    return 0


def check(text: str) -> int:
    tokens = [t for t in dict.fromkeys(TOKEN_RE.findall(text)) if not ACCOUNT_RE.fullmatch(t)]
    bad = [t for t in tokens if t not in KNOWN_IDS]
    for t in tokens:
        print(f"- [{t}] {'ok' if t in KNOWN_IDS else 'UNKNOWN (remove or replace)'}")
    if not tokens:
        print("No [ID] citation tokens found.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="citation ids, e.g. SOP-9-2")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--search", metavar="QUERY")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--check", metavar="TEXT", help="validate [ID] tokens in a piece of text")
    args = ap.parse_args(argv)
    if args.list:
        return list_ids()
    if args.search:
        return search(args.search, args.k)
    if args.check is not None:
        return check(args.check)
    if args.ids:
        return lookup([i.strip("[]").upper() for i in args.ids])
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
