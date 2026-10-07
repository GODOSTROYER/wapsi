"""Re-verify every committed quote against the raw documents.

    uv run python -m wapsi.search.check_citations

Checks (exit status 1 on any failure):
- ``citations.json`` covers every ID in ``wapsi.citations`` and nothing else, with exactly the Citation fields;
- every quote has at most 25 words;
- every non-empty quote occurs verbatim (after whitespace / typographic normalisation, see
  ``wapsi.search.normalize``) in the document its URL points to, on the stated PDF page (or across the break
  into the next page); HTML sources carry ``page: null``;
- judgments reported by news/blog sites carry a URL and no quote text;
- every ``facts.json`` snippet occurs on its page, and the printed SOP page label matches (a fact's ``doc_id``
  is a corpus source or a radar-only source registered in ``data/docs/reg_radar.json``);
- the regulatory change radar (``data/docs/reg_radar.json``) is valid and every one of its quotes occurs verbatim
  in its source (``wapsi.search.radar.check_rows``).
Requires the raw files (``uv run python -m wapsi.search.fetch`` and ``uv run python -m wapsi.search.radar fetch``).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wapsi.citations import KNOWN_IDS
from wapsi.models import Citation
from wapsi.search.build import load_document
from wapsi.search.extract import ExtractedDoc
from wapsi.search.normalize import match_quote, quote_word_count
from wapsi.search.sources import CITATIONS_JSON, FACTS_JSON, RAW_DIR, Source, source_for_url, sources_by_id

MAX_WORDS = 25
FIELDS = set(Citation.model_fields)


@dataclass
class Row:
    item: str
    ok: bool
    detail: str


class DocCache:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self._docs: dict[str, ExtractedDoc] = {}

    def get(self, source: Source) -> ExtractedDoc:
        if source.id not in self._docs:
            self._docs[source.id], _ = load_document(source, self.raw_dir)
        return self._docs[source.id]


def locate(doc: ExtractedDoc, quote: str, page: int | None) -> tuple[str | None, list[int]]:
    """(match mode on the stated page or across its break, pages where the quote was found anywhere)."""
    mode = None
    if page is not None:
        here, after = doc.page_text(page), doc.page_text(page + 1)
        mode = match_quote(quote, here)
        if not mode and not match_quote(quote, after):  # must start on the stated page
            mode = match_quote(quote, f"{here}\n{after}")
    found = [p.number for p in doc.pages if match_quote(quote, p.text)]
    return mode, found


def check_citation(entry: dict[str, Any], docs: DocCache) -> Row:
    cid = entry.get("citation_id", "?")
    problems: list[str] = []
    if cid not in KNOWN_IDS:
        problems.append("unknown citation_id")
    keys = set(entry)
    if keys != FIELDS:
        problems.append(f"fields {sorted(keys ^ FIELDS)} differ from Citation")
    quote = entry.get("quote") or ""
    url = entry.get("url") or ""
    page = entry.get("page")
    words = quote_word_count(quote)
    if words > MAX_WORDS:
        problems.append(f"quote has {words} words (> {MAX_WORDS})")
    if not url:
        problems.append("missing url")
    source = source_for_url(url)
    if cid.startswith("CASE-") and source is None and quote:
        problems.append("news/blog-reported judgment must not store quote text")
    detail = ""
    if quote:
        if source is None:
            problems.append(f"quote given but url is not a fetched source: {url}")
        else:
            try:
                doc = docs.get(source)
            except (FileNotFoundError, ValueError) as exc:
                return Row(cid, False, f"cannot load {source.id}: {exc}")
            if doc.paged:
                if page is None:
                    problems.append("PDF quote without page")
                mode, found = locate(doc, quote, page)
                if not mode:
                    where = f"found on pages {found}" if found else "NOT FOUND in document"
                    problems.append(f"quote not on page {page}; {where}")
                detail = f"{source.id} p{page} {mode or ''}".strip()
            else:
                if page is not None:
                    problems.append("HTML source must have page null")
                mode = match_quote(quote, doc.pages[0].text)
                if not mode:
                    problems.append("quote NOT FOUND in document")
                detail = f"{source.id} {mode or ''}".strip()
    else:
        detail = f"url only ({url[:60]})"
    if problems:
        return Row(cid, False, "; ".join(problems))
    return Row(cid, True, f"{detail}; {words} words" if quote else detail)


def check_fact(fact: dict[str, Any], docs: DocCache) -> Row:
    fid = fact.get("fact_id", "?")
    source = sources_by_id().get(fact.get("doc_id", ""))
    if source is None:  # a radar-only source (fetched and verified, not indexed)
        from wapsi.search.radar import radar_only_sources

        source = radar_only_sources().get(fact.get("doc_id", ""))
    if source is None:
        return Row(f"fact:{fid}", False, f"unknown doc_id {fact.get('doc_id')}")
    try:
        doc = docs.get(source)
    except (FileNotFoundError, ValueError) as exc:
        return Row(f"fact:{fid}", False, f"cannot load {source.id}: {exc}")
    quote = fact.get("quote", "")
    page = fact.get("pdf_page")
    problems = []
    if quote_word_count(quote) > MAX_WORDS:
        problems.append("quote longer than 25 words")
    if doc.paged:
        mode, found = locate(doc, quote, page)
        if not mode:
            problems.append(f"not on page {page}; found on {found}" if found else "NOT FOUND")
        label = next((p.label for p in doc.pages if p.number == page), None)
        printed = fact.get("printed_page")
        if printed is not None and label != str(printed):
            problems.append(f"printed page {printed} != label {label} on PDF page {page}")
    else:
        mode = match_quote(quote, doc.pages[0].text)
        if not mode:
            problems.append("NOT FOUND")
    if problems:
        return Row(f"fact:{fid}", False, "; ".join(problems))
    return Row(f"fact:{fid}", True, f"{source.id} p{page} {mode}")


def run(
    citations_path: Path = CITATIONS_JSON,
    facts_path: Path = FACTS_JSON,
    raw_dir: Path = RAW_DIR,
    radar_path: Path | None = None,
) -> list[Row]:
    docs = DocCache(raw_dir)
    rows: list[Row] = []
    if not citations_path.exists():
        return [Row(str(citations_path), False, "file not found")]
    entries = json.loads(citations_path.read_text(encoding="utf-8"))
    ids = [e.get("citation_id") for e in entries]
    for missing in sorted(KNOWN_IDS - set(ids)):
        rows.append(Row(missing, False, "missing from citations.json"))
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        rows.append(Row("citations.json", False, f"duplicate ids {dupes}"))
    rows.extend(check_citation(e, docs) for e in entries)
    if facts_path.exists():
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        rows.extend(check_fact(f, docs) for f in facts.get("facts", []))
    from wapsi.search.radar import check_rows  # lazy: radar imports this module

    rows.extend(check_rows(docs, radar_path))  # default data/docs/reg_radar.json; none if the file is absent
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--citations", type=Path, default=CITATIONS_JSON)
    ap.add_argument("--facts", type=Path, default=FACTS_JSON)
    ap.add_argument("--radar", type=Path, default=None, help="regulatory radar (default data/docs/reg_radar.json)")
    args = ap.parse_args(argv)
    rows = run(args.citations, args.facts, radar_path=args.radar)
    width = max(len(r.item) for r in rows)
    for r in rows:
        print(f"{'OK ' if r.ok else 'FAIL'} {r.item:<{width}}  {r.detail}")
    bad = [r for r in rows if not r.ok]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
