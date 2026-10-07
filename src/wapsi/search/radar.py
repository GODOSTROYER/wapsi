"""Fetch and verify the documents behind the regulatory change radar (``data/docs/reg_radar.json``).

    uv run python -m wapsi.search.radar fetch [--force] [--only ID ...]   # raw files -> data/docs/raw/
    uv run python -m wapsi.search.radar verify [--write]                  # re-check every quote

Radar sources marked ``corpus`` are the indexed corpus (``wapsi.search.sources``) and are fetched by
``wapsi.search.fetch``. The others (a draft, a repeal circular, RBI's withdrawn-circulars list, a repealed
direction, a parliamentary answer) are fetched here into the same gitignored ``data/docs/raw/`` and logged in
``raw/_fetch.json``, but they are **not indexed**: search and drafts never ground an STR or a letter on a draft or
a repealed text. ``wapsi.search.check_citations`` runs :func:`check_rows` so every radar quote is verified with the
same matcher as the citations (and ``facts.json`` may cite a radar source by id).

Polite by default (identifying User-Agent). ``rbidocs.rbi.org.in`` answers non-browser clients with a JavaScript
bot-challenge page, so sources that set ``fetch_headers: "browser"`` are requested with ordinary browser request
headers; a challenge page is still rejected by the expected-text check. TLS verification is never disabled.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from wapsi.domain.regradar import Radar, RadarSource, Side, load_radar, radar_path, validate_radar
from wapsi.search.normalize import match_quote
from wapsi.search.sources import FETCH_LOG, RAW_DIR, SOURCES, Source, sources_by_id

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 "
        "Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def browser_headers(url: str) -> dict[str, str]:
    """Browser request headers with the site's own origin as Referer."""
    parts = urlsplit(url)
    return {**BROWSER_HEADERS, "Referer": f"{parts.scheme}://{parts.netloc}/"}


def to_source(rs: RadarSource) -> Source:
    """The corpus Source for corpus rows; otherwise an unindexed Source built from the radar row."""
    if rs.corpus:
        return sources_by_id()[rs.id]
    return Source(
        id=rs.id,
        title=rs.title,
        publisher=rs.publisher or "-",
        type=rs.type or "html",
        url=rs.url,
        licence=rs.licence or "Official publication; not redistributed; short verbatim quotes only.",
        profile="rbi_dir",  # unused: radar-only sources are never indexed, so no section detection runs
        expect_text=rs.expect_text,
        start_marker=rs.start_marker,
        end_marker=rs.end_marker,
        notes=rs.notes,
    )


def radar_sources(radar: Radar) -> dict[str, Source]:
    return {rs.id: to_source(rs) for rs in radar.sources}


def radar_only_sources(radar: Radar | None = None) -> dict[str, Source]:
    """Radar sources that are not part of the indexed corpus (empty if the registry file is absent)."""
    if radar is None:
        path = radar_path()
        if not path.exists():
            return {}
        radar = load_radar(path)
    return {rs.id: to_source(rs) for rs in radar.sources if not rs.corpus}


# --------------------------------------------------------------------------- fetch


def fetch(radar: Radar, only: list[str] | None = None, *, force: bool = False, raw_dir: Path = RAW_DIR,
          log_path: Path = FETCH_LOG, transport: Any = None) -> int:
    """Download radar-only sources; returns the number of failures. ``transport`` is for tests (httpx mock)."""
    import httpx

    from wapsi.search.fetch import PAUSE_S, TIMEOUT, USER_AGENT, fetch_source, load_log, save_log

    raw_dir.mkdir(parents=True, exist_ok=True)
    log = load_log(log_path)
    polite = {"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}
    failures = 0
    todo = [rs for rs in radar.sources if not rs.corpus and (not only or rs.id in only)]
    for i, rs in enumerate(todo):
        if i:
            time.sleep(PAUSE_S)
        headers = browser_headers(rs.url) if rs.fetch_headers == "browser" else polite
        with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True, transport=transport) as client:
            entry = fetch_source(client, to_source(rs), log, force=force, raw_dir=raw_dir)
        entry["fetch_headers"] = rs.fetch_headers
        if entry["status"] == "failed":
            failures += 1
            prev = log.get(rs.id)
            if prev and prev.get("status") != "failed":
                prev["last_failure"] = entry
                entry = prev
        log[rs.id] = entry
        size = f"{entry.get('bytes', 0):>10,} B" if entry.get("bytes") else " " * 12
        print(f"{rs.id:<26} {entry['status']:<11} {size} {entry.get('url', '')}")
        for att in entry.get("failed_attempts", []):
            print(f"{'':<26} failed: {att['url']} -> {att['error']}")
    save_log(log, log_path)
    return failures


# --------------------------------------------------------------------------- verify


def verify_side(side: Side, sources: dict[str, Source], docs: Any) -> tuple[bool, str]:
    """(ok, detail) for one quote. ``docs`` is a ``check_citations.DocCache`` (or a test double)."""
    from wapsi.search.check_citations import locate

    source = sources.get(side.source_id)
    if source is None:
        return False, f"unknown source {side.source_id}"
    try:
        doc = docs.get(source)
    except (FileNotFoundError, ValueError) as exc:
        return False, f"cannot load {source.id}: {exc} (run uv run python -m wapsi.search.radar fetch)"
    if doc.paged:
        if side.page is None:
            return False, f"{source.id}: PDF quote without page"
        mode, found = locate(doc, side.quote, side.page)
        if not mode:
            where = f"found on pages {found}" if found else "NOT FOUND in document"
            return False, f"{source.id}: quote not on page {side.page}; {where}"
        return True, f"{source.id} p{side.page} {mode}"
    if side.page is not None:
        return False, f"{source.id}: HTML source must have page null"
    mode = match_quote(side.quote, doc.pages[0].text)
    if not mode:
        return False, f"{source.id}: quote NOT FOUND in document"
    return True, f"{source.id} {mode}"


def verify(radar: Radar, docs: Any) -> tuple[dict[str, tuple[bool, list[str]]], list[tuple[str, bool, str]]]:
    """({clause_id: (all ok, details)}, rows) where rows are (item, ok, detail) for every quote and evidence."""
    sources = radar_sources(radar)
    rows: list[tuple[str, bool, str]] = []
    for rs in radar.sources:
        for i, side in enumerate(rs.evidence):
            ok, detail = verify_side(side, sources, docs)
            rows.append((f"radar:{rs.id}:evidence[{i}]", ok, detail))
    per_clause: dict[str, tuple[bool, list[str]]] = {}
    for c in radar.clauses:
        details, all_ok = [], True
        for label, side in c.sides():
            ok, detail = verify_side(side, sources, docs)
            all_ok &= ok
            details.append(f"{label}: {detail}")
            rows.append((f"radar:{c.id}:{label}", ok, detail))
        per_clause[c.id] = (all_ok, details)
    return per_clause, rows


def check_rows(docs: Any, path: Path | None = None) -> list[Any]:
    """Rows for ``check_citations``: registry validity plus every radar quote (none if the file is absent)."""
    from wapsi.search.check_citations import Row

    p = path or radar_path()
    if not p.exists():
        return []
    try:
        radar = load_radar(p)
    except (ValueError, OSError) as exc:
        return [Row("radar:registry", False, f"cannot load {p.name}: {exc}")]
    out = [Row("radar:registry", False, prob) for prob in validate_radar(radar, {s.id for s in SOURCES})]
    per_clause, rows = verify(radar, docs)
    out += [Row(item, ok, detail) for item, ok, detail in rows]
    for c in radar.clauses:
        ok, _ = per_clause[c.id]
        if c.verified and not ok:
            out.append(Row(f"radar:{c.id}", False, "marked verified but a quote failed; run radar verify --write"))
    if not out or all(r.ok for r in out):
        out.append(Row("radar:registry", True, f"{len(radar.clauses)} rows, {len(radar.sources)} sources"))
    return out


def write_verification(path: Path, per_clause: dict[str, tuple[bool, list[str]]], today: date) -> int:
    """Stamp each clause's ``verification`` in the JSON file (other fields untouched). Returns rows verified."""
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for c in data["clauses"]:
        ok, details = per_clause.get(c["id"], (False, ["not checked"]))
        c["verification"] = {
            "status": "verified" if ok else "unverified",
            "checked_on": today.isoformat(),
            "detail": details,
        }
        n += ok
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download radar-only sources into data/docs/raw/")
    f.add_argument("--only", nargs="*", help="radar source ids to fetch")
    f.add_argument("--force", action="store_true", help="re-download even if the raw file exists")
    v = sub.add_parser("verify", help="re-verify every quote against the raw documents")
    v.add_argument("--write", action="store_true", help="stamp each row's verification in reg_radar.json")
    ap.add_argument("--path", type=Path, default=None, help="registry file (default data/docs/reg_radar.json)")
    args = ap.parse_args(argv)
    path = args.path or radar_path()
    radar = load_radar(path)
    if args.cmd == "fetch":
        from wapsi.search.manifest import write_manifest

        failures = fetch(radar, args.only, force=args.force)
        write_manifest()  # provenance (URL, bytes, sha256, retrieval time) of the radar documents too
        return 1 if failures else 0

    from wapsi.search.check_citations import DocCache

    problems = validate_radar(radar, {s.id for s in SOURCES})
    for p in problems:
        print(f"FAIL radar:registry  {p}")
    per_clause, rows = verify(radar, DocCache(RAW_DIR))
    width = max((len(r[0]) for r in rows), default=10)
    for item, ok, detail in rows:
        print(f"{'OK ' if ok else 'FAIL'} {item:<{width}}  {detail}")
    bad = sum(1 for _, ok, _ in rows if not ok) + len(problems)
    print(f"\n{len(rows) - bad + len(problems)}/{len(rows)} quotes verified; "
          f"{sum(ok for ok, _ in per_clause.values())}/{len(per_clause)} rows fully verified")
    if args.write:
        n = write_verification(path, per_clause, date.today())
        print(f"stamped {path} ({n} rows verified)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
