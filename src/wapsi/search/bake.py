"""Bake the docs search index into a deployable tree (the Cloud Run image), so ``search_sop`` and the citation
corpus work on the deployed URL and not only on a developer's machine.

    uv run --extra docs python -m wapsi.search.bake [--fetch] [--prune-raw] [--strict]

``data/docs/index/`` is gitignored (the source documents carry no open licence, so their text is never committed),
which means a fresh clone, and therefore a container build, has no index and ``LocalDocSearch.load()`` serves the
small citations-only corpus. This command makes sure the index is there, in this order:

1. ``present``: ``index/chunks.jsonl`` is already in the tree (the deploy built it on the deploying machine and
   uploaded it with the build context); it is only validated.
2. ``built``: the raw documents are in ``raw/``; the index is built from them (``wapsi.search.build``).
3. ``fetched``: with ``--fetch``, the public documents are downloaded first (``wapsi.search.fetch``: polite,
   TLS verified, content checked), then built. ``--prune-raw`` deletes ``raw/`` afterwards (the image needs only
   the index).

The result is validated (it loads without the fallback, has chunks, and carries citation ids) and recorded in
``index/bake.json``. If no index can be produced the command warns and exits 0, so an image build never fails
over a government website being down: the app then serves the citations-only corpus and ``/healthz`` says so
(``docs_fallback: true``). ``--strict`` makes that an error (exit 1).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wapsi.search import sources as src

BAKE_REPORT = "bake.json"
PRESENT, BUILT, FETCHED, FALLBACK = "present", "built", "fetched", "fallback"


class BakeError(RuntimeError):
    """No usable index could be produced."""


def _chunks_file(index_dir: Path) -> Path:
    return index_dir / "chunks.jsonl"


def _has_raw(raw_dir: Path) -> bool:
    return raw_dir.is_dir() and any(next(raw_dir.glob(f"{s.id}.*"), None) is not None for s in src.SOURCES)


def validate(index_dir: Path) -> dict[str, Any]:
    """Load the index the way the app does (no fallback) and summarise it; ``BakeError`` if it is unusable."""
    from wapsi.search.index import LocalDocSearch

    chunks_file = _chunks_file(index_dir)
    if not chunks_file.exists():
        raise BakeError(f"{chunks_file} does not exist")
    try:
        search = LocalDocSearch.load(index_dir, allow_fallback=False)
    except Exception as exc:  # noqa: BLE001 - a malformed line, a schema change: all mean "unusable"
        raise BakeError(f"{chunks_file} does not load: {type(exc).__name__}: {exc}") from exc
    if search.fallback or not len(search):
        raise BakeError(f"{chunks_file} holds no chunks")
    docs = Counter(c.doc_id for c in search.chunks)
    cited = sorted({cid for c in search.chunks for cid in c.citation_ids})
    if not cited:
        raise BakeError(f"{chunks_file} has no chunk that carries a citation id")
    return {"chunks": len(search), "docs": dict(sorted(docs.items())), "citation_ids": len(cited)}


def _fetch(raw_dir: Path) -> list[str]:
    """Download every source into ``raw_dir`` (``wapsi.search.fetch``); returns the ids that failed. The committed
    MANIFEST.md is not rewritten (that is ``wapsi.search.fetch``'s job when a maintainer refreshes the corpus)."""
    import httpx

    from wapsi.search import fetch as fetch_mod

    raw_dir.mkdir(parents=True, exist_ok=True)
    log_path = raw_dir / src.FETCH_LOG.name
    log = fetch_mod.load_log(log_path)
    failed: list[str] = []
    headers = {"User-Agent": fetch_mod.USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}
    with httpx.Client(headers=headers, timeout=fetch_mod.TIMEOUT, follow_redirects=True) as client:
        for i, source in enumerate(src.SOURCES):
            if i:
                time.sleep(fetch_mod.PAUSE_S)
            entry = fetch_mod.fetch_source(client, source, log, raw_dir=raw_dir)
            if entry.get("status") == "failed":
                failed.append(source.id)
            else:
                log[source.id] = entry
            print(f"  fetch {source.id:<18} {entry.get('status')}")
    fetch_mod.save_log(log, log_path)
    return failed


def _build(raw_dir: Path, index_dir: Path) -> dict[str, Any]:
    from wapsi.search.build import build

    citations = index_dir.parent / src.CITATIONS_JSON.name
    report = build(raw_dir=raw_dir, index_dir=index_dir,
                   citations_path=citations if citations.exists() else src.CITATIONS_JSON)
    for doc_id, rep in report["docs"].items():
        print(f"  build {doc_id:<18} " + (f"ERROR {rep['error']}" if "error" in rep else f"chunks={rep['chunks']}"))
    return report


def bake(docs_dir: Path | None = None, *, fetch: bool = False, prune_raw: bool = False) -> dict[str, Any]:
    """Make sure ``<docs_dir>/index`` holds a usable index; returns the bake report (also written to
    ``index/bake.json``). Raises ``BakeError`` when none could be produced."""
    docs_dir = Path(docs_dir) if docs_dir is not None else src.DOCS_DIR
    raw_dir, index_dir = docs_dir / "raw", docs_dir / "index"
    problems: list[str] = []
    status = None
    if _chunks_file(index_dir).exists():
        try:
            summary = validate(index_dir)
            status = PRESENT
        except BakeError as exc:
            problems.append(f"shipped index unusable: {exc}")
    if status is None and not _has_raw(raw_dir) and fetch:
        failed = _fetch(raw_dir)
        if failed:
            problems.append(f"could not download: {', '.join(failed)}")
        fetched = True
    else:
        fetched = False
    if status is None and _has_raw(raw_dir):
        _build(raw_dir, index_dir)
        try:
            summary = validate(index_dir)
            status = FETCHED if fetched else BUILT
        except BakeError as exc:
            problems.append(f"built index unusable: {exc}")
    if prune_raw and raw_dir.is_dir():
        shutil.rmtree(raw_dir, ignore_errors=True)
    if status is None:
        if not fetch and not problems:
            problems.append("no index and no raw documents (pass --fetch to download them)")
        raise BakeError("; ".join(problems) or "no index could be produced")
    report = {"status": status, **summary, "problems": problems,
              "baked_at": datetime.now(UTC).replace(microsecond=0).isoformat()}
    (index_dir / BAKE_REPORT).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wapsi.search.bake", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs-dir", type=Path, default=None, help="the docs folder (default: WAPSI_DOCS_DIR or data/docs)")
    ap.add_argument("--fetch", action="store_true", help="download the public sources when raw/ is empty")
    ap.add_argument("--prune-raw", action="store_true", help="delete raw/ afterwards (images keep only the index)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when no index could be produced")
    args = ap.parse_args(argv)
    try:
        rep = bake(args.docs_dir, fetch=args.fetch, prune_raw=args.prune_raw)
    except BakeError as exc:
        print(f"WARNING: docs index not baked ({exc}); the app will serve the citations-only corpus "
              "(search_sop is weaker; /healthz reports docs_fallback: true)")
        return 1 if args.strict else 0
    docs = ", ".join(f"{d}={n}" for d, n in rep["docs"].items())
    print(f"docs index {rep['status']}: {rep['chunks']} chunks ({docs}); {rep['citation_ids']} citation ids")
    for p in rep["problems"]:
        print(f"  note: {p}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
