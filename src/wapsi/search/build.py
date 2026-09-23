"""Build the local docs index from the raw files.

    uv run python -m wapsi.search.build

Writes ``data/docs/index/chunks.jsonl``, ``index/text/<doc_id>.txt`` (extracted text with page markers, for
inspection), ``index/build_report.json`` and refreshes ``data/docs/MANIFEST.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wapsi.search.chunk import DocChunk, chunk_document, section_keys
from wapsi.search.extract import ExtractedDoc, extract
from wapsi.search.fetch import load_log, raw_path, sha256_file
from wapsi.search.manifest import write_manifest
from wapsi.search.normalize import QuoteMatcher
from wapsi.search.sections import detect_sections
from wapsi.search.sources import BUILD_REPORT, CITATIONS_JSON, INDEX_DIR, RAW_DIR, SOURCES, Source


def load_quotes(path: Path = CITATIONS_JSON) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return {c["citation_id"]: c for c in json.loads(path.read_text(encoding="utf-8"))}


def load_document(source: Source, raw_dir: Path = RAW_DIR) -> tuple[ExtractedDoc, Path]:
    path = raw_path(load_log().get(source.id), source, raw_dir)
    if path is None:
        raise FileNotFoundError(
            f"{source.id}: no raw file in {raw_dir}; run uv run python -m wapsi.search.fetch"
        )
    return extract(path.read_bytes(), source), path


def doc_report(
    source: Source, doc: ExtractedDoc, path: Path, chunks: list[DocChunk], keys: set[str], n_sections: int
) -> dict[str, Any]:
    rep: dict[str, Any] = {
        "file": path.name,
        "sha256": sha256_file(path),
        "kind": doc.kind,
        "chunks": len(chunks),
        "sections": n_sections,
        "missing_sections": [k for k in source.expect_sections if k not in keys],
        "warnings": list(doc.warnings),
    }
    if doc.paged:
        rep.update(
            pages=len(doc.pages),
            text_pages=sum(1 for p in doc.pages if p.status == "text"),
            scanned_pages=[p.number for p in doc.pages if p.status in ("scanned", "sparse_with_images")],
            blank_pages=[p.number for p in doc.pages if p.status == "blank"],
            page_labels={str(p.number): p.label for p in doc.pages if p.label},
        )
    else:
        rep["chars"] = doc.pages[0].chars if doc.pages else 0
    return rep


def build(
    raw_dir: Path = RAW_DIR,
    index_dir: Path = INDEX_DIR,
    citations_path: Path = CITATIONS_JSON,
    only: list[str] | None = None,
) -> dict[str, Any]:
    quotes = load_quotes(citations_path)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "text").mkdir(exist_ok=True)
    all_chunks: list[DocChunk] = []
    report: dict[str, Any] = {"built_at": datetime.now(UTC).replace(microsecond=0).isoformat(), "docs": {}}
    errors: list[str] = []
    for source in SOURCES:
        if only and source.id not in only:
            continue
        try:
            doc, path = load_document(source, raw_dir)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
            report["docs"][source.id] = {"error": str(exc)}
            continue
        (index_dir / "text" / f"{source.id}.txt").write_text(doc.full_text(), encoding="utf-8")
        sections = detect_sections(doc, source)
        doc_quotes = {
            cid: quotes[cid]["quote"] for cid in source.citation_ids if quotes.get(cid, {}).get("quote")
        }
        chunks = chunk_document(doc, source, doc_quotes, sections=sections)
        rep = doc_report(source, doc, path, chunks, section_keys(sections), len(sections))
        for cid, q in doc_quotes.items():
            if not any(QuoteMatcher(c.text).match(q) for c in chunks if cid in c.citation_ids):
                rep["warnings"].append(f"quote for {cid} is not contained in any indexed chunk")
        rep["citations_in_chunks"] = sorted({cid for c in chunks for cid in c.citation_ids})
        report["docs"][source.id] = rep
        all_chunks.extend(chunks)
    out = index_dir / "chunks.jsonl"
    tmp = out.with_suffix(".jsonl.part")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for c in all_chunks:
            fh.write(c.model_dump_json() + "\n")
    tmp.replace(out)
    report["total_chunks"] = len(all_chunks)
    report["errors"] = errors
    (index_dir / BUILD_REPORT.name).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    return report


def print_report(report: dict[str, Any]) -> None:
    for doc_id, rep in report["docs"].items():
        if "error" in rep:
            print(f"{doc_id:<18} ERROR {rep['error']}")
            continue
        pages = (
            f"{rep['pages']} pages ({rep['text_pages']} with text)"
            if "pages" in rep
            else f"{rep['chars']:,} chars"
        )
        print(f"{doc_id:<18} {pages:<28} sections={rep['sections']:<4} chunks={rep['chunks']}")
        if rep.get("scanned_pages"):
            print(f"{'':<18} pages without text layer: {rep['scanned_pages']}")
        if rep.get("blank_pages"):
            print(f"{'':<18} blank pages: {rep['blank_pages']}")
        if rep.get("missing_sections"):
            print(f"{'':<18} MISSING sections: {rep['missing_sections']}")
        for w in rep.get("warnings", []):
            print(f"{'':<18} warning: {w}")
    print(f"total chunks: {report['total_chunks']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="source ids to build (the index then contains only these)")
    args = ap.parse_args(argv)
    report = build(only=args.only)
    write_manifest()
    print_report(report)
    missing = any(rep.get("missing_sections") for rep in report["docs"].values())
    return 1 if report["errors"] or missing or not report["total_chunks"] else 0


if __name__ == "__main__":
    sys.exit(main())
