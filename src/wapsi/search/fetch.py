"""Download the legal corpus into ``data/docs/raw/`` (gitignored) and record provenance.

    uv run python -m wapsi.search.fetch [--force] [--only sop-2026 ...]

Polite: identifying User-Agent, timeouts, one request per second, bounded retries. Each source is tried at
its primary URL, then its mirrors; a download is accepted only if the content type matches (PDF magic bytes /
HTML with the expected marker text), so bot-challenge pages are rejected. TLS verification is never disabled.
sha256 and retrieval time go to ``raw/_fetch.json`` and the committed ``data/docs/MANIFEST.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from wapsi.search.extract import decode_html, sniff
from wapsi.search.manifest import write_manifest
from wapsi.search.sources import FETCH_LOG, RAW_DIR, SOURCES, Source

USER_AGENT = "WapsiDocsFetcher/0.1 (hackathon research; fetches public legal texts once; python-httpx)"
TIMEOUT = httpx.Timeout(120.0, connect=20.0)
PAUSE_S = 1.0
RETRIES = 2
MIN_BYTES = 2_000


class RejectedContent(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_log(path: Path = FETCH_LOG) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_log(log: dict[str, Any], path: Path = FETCH_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate(data: bytes, source: Source) -> str:
    kind = sniff(data)
    if len(data) < MIN_BYTES:
        raise RejectedContent(f"too small ({len(data)} bytes)")
    if kind not in ("pdf", "html"):
        raise RejectedContent(f"unexpected content ({kind})")
    if kind == "html" and source.expect_text and source.expect_text not in decode_html(data):
        raise RejectedContent(
            f"HTML without expected text {source.expect_text!r} (bot challenge or error page?)"
        )
    return kind


def download(client: httpx.Client, url: str, source: Source) -> tuple[bytes, str, str]:
    last_exc: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = client.get(url)
            if resp.status_code >= 500 and attempt < RETRIES:
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise RejectedContent(f"HTTP {resp.status_code}")
            kind = validate(resp.content, source)
            return resp.content, kind, resp.headers.get("content-type", "")
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            last_exc = exc
            if attempt < RETRIES:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
    raise RejectedContent(f"gave up: {last_exc}")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def raw_path(log_entry: dict[str, Any] | None, source: Source, raw_dir: Path = RAW_DIR) -> Path | None:
    if log_entry and log_entry.get("filename") and (raw_dir / log_entry["filename"]).exists():  # failed: none
        return raw_dir / log_entry["filename"]
    for ext in ("pdf", "html"):
        p = raw_dir / f"{source.id}.{ext}"
        if p.exists():
            return p
    return None


def fetch_source(
    client: httpx.Client, source: Source, log: dict[str, Any], *, force: bool = False, raw_dir: Path = RAW_DIR
) -> dict[str, Any]:
    prev = log.get(source.id)
    existing = raw_path(prev, source, raw_dir)
    if existing and not force:
        digest = sha256_file(existing)
        entry = dict(prev or {})
        if not prev or prev.get("sha256") != digest:
            entry.update(
                filename=existing.name,
                bytes=existing.stat().st_size,
                sha256=digest,
                retrieved_at=entry.get("retrieved_at") or "unknown (pre-existing file)",
            )
        entry["status"] = "cached"
        return entry

    attempts: list[dict[str, str]] = []
    for n, url in enumerate(source.all_urls()):
        if n:
            time.sleep(PAUSE_S)
        try:
            data, kind, ctype = download(client, url, source)
        except (httpx.HTTPError, RejectedContent) as exc:
            attempts.append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        filename = f"{source.id}.{kind}"
        _write_atomic(raw_dir / filename, data)
        for other in ("pdf", "html"):
            stale = raw_dir / f"{source.id}.{other}"
            if other != kind and stale.exists():
                stale.unlink()
        return {
            "status": "downloaded",
            "url": url,
            "via_mirror": n > 0,
            "filename": filename,
            "kind": kind,
            "content_type": ctype,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
            "retrieved_at": now_utc(),
            "failed_attempts": attempts,
        }
    return {"status": "failed", "failed_attempts": attempts, "retrieved_at": now_utc()}


def check_mirrors(client: httpx.Client, source: Source) -> list[dict[str, str]]:
    """Probe mirrors (HEAD-less GET) without saving, to record their availability in the manifest."""
    out = []
    for url in source.mirrors:
        time.sleep(PAUSE_S)
        try:
            download(client, url, source)
            out.append({"url": url, "status": "ok"})
        except (httpx.HTTPError, RejectedContent) as exc:
            out.append({"url": url, "status": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def run(only: list[str] | None = None, force: bool = False, probe_mirrors: bool = False) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    log = load_log()
    failures = 0
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"}
    with httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True) as client:
        for i, source in enumerate(SOURCES):
            if only and source.id not in only:
                continue
            if i:
                time.sleep(PAUSE_S)
            entry = fetch_source(client, source, log, force=force)
            if probe_mirrors and source.mirrors:
                entry["mirror_check"] = check_mirrors(client, source)
                entry["mirror_checked_at"] = now_utc()
            if entry["status"] == "failed":
                failures += 1
                prev = log.get(source.id)
                if prev and prev.get("status") != "failed":
                    prev["last_failure"] = entry
                    entry = prev
            log[source.id] = entry
            size = f"{entry.get('bytes', 0):>10,} B" if entry.get("bytes") else " " * 12
            print(f"{source.id:<18} {entry['status']:<11} {size} {entry.get('url', '')}")
            for att in entry.get("failed_attempts", []):
                print(f"{'':<18} failed: {att['url']} -> {att['error']}")
    save_log(log)
    write_manifest()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="source ids to fetch")
    ap.add_argument("--force", action="store_true", help="re-download even if the raw file exists")
    ap.add_argument("--probe-mirrors", action="store_true", help="also test every mirror URL (not saved)")
    args = ap.parse_args(argv)
    return run(args.only, args.force, args.probe_mirrors)


if __name__ == "__main__":
    sys.exit(main())
