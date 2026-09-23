"""fetch.py behaviour with an in-process mock transport (no network)."""

import hashlib

import httpx
import pytest

from wapsi.search import fetch
from wapsi.search.manifest import render_manifest
from wapsi.search.sources import Source

PDF = b"%PDF-1.7\n" + b"0" * 5000
CHALLENGE = b"<!DOCTYPE html><html><script>window.bobcmn='challenge'</script>" + b" " * 3000 + b"</html>"
GOOD_HTML = b"<!DOCTYPE html><html><body>Early Warning Signals" + b" " * 3000 + b"</body></html>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda _s: None)


def _source(**kw) -> Source:
    base = dict(
        id="doc",
        title="Doc",
        publisher="Pub",
        type="pdf",
        url="https://primary.example/doc.pdf",
        mirrors=["https://mirror.example/doc.pdf"],
        licence="test",
        profile="sop",
    )
    base.update(kw)
    return Source(**base)


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["user-agent"].startswith("WapsiDocsFetcher/")
        return routes.get(request.url.host, httpx.Response(404))

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": fetch.USER_AGENT},
        follow_redirects=True,
    )
    client.seen = seen  # type: ignore[attr-defined]
    return client


def test_primary_failure_falls_back_to_mirror(workdir):
    client = _client(
        {
            "primary.example": httpx.Response(403, text="Access Denied"),
            "mirror.example": httpx.Response(200, content=PDF),
        }
    )
    entry = fetch.fetch_source(client, _source(), {}, raw_dir=workdir)
    assert entry["status"] == "downloaded"
    assert entry["via_mirror"] is True
    assert entry["url"] == "https://mirror.example/doc.pdf"
    assert entry["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert entry["bytes"] == len(PDF)
    assert entry["retrieved_at"].endswith("+00:00")
    assert entry["failed_attempts"][0]["error"] == "RejectedContent: HTTP 403"
    assert (workdir / "doc.pdf").read_bytes() == PDF


def test_bot_challenge_html_is_rejected(workdir):
    src = _source(
        type="html", url="https://rbi.example/page", mirrors=[], expect_text="Early Warning Signals"
    )
    entry = fetch.fetch_source(
        _client({"rbi.example": httpx.Response(200, content=CHALLENGE)}), src, {}, raw_dir=workdir
    )
    assert entry["status"] == "failed"
    assert "expected text" in entry["failed_attempts"][0]["error"]
    assert not list(workdir.iterdir())
    entry = fetch.fetch_source(
        _client({"rbi.example": httpx.Response(200, content=GOOD_HTML)}), src, {}, raw_dir=workdir
    )
    assert entry["status"] == "downloaded" and entry["kind"] == "html"
    assert (workdir / "doc.html").exists()


def test_validate_rejects_tiny_or_unknown_content():
    with pytest.raises(fetch.RejectedContent):
        fetch.validate(b"%PDF-1.4 tiny", _source())
    with pytest.raises(fetch.RejectedContent):
        fetch.validate(b"\x00" * 5000, _source())
    assert fetch.validate(PDF, _source()) == "pdf"


def test_server_errors_are_retried(workdir):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, content=PDF)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entry = fetch.fetch_source(client, _source(mirrors=[]), {}, raw_dir=workdir)
    assert entry["status"] == "downloaded" and calls["n"] == 2


def test_existing_file_is_reused_without_network(workdir):
    (workdir / "doc.pdf").write_bytes(PDF)
    client = _client({})
    log = {"doc": {"filename": "doc.pdf", "sha256": "stale", "retrieved_at": "2026-09-01T00:00:00+00:00"}}
    entry = fetch.fetch_source(client, _source(), log, raw_dir=workdir)
    assert entry["status"] == "cached"
    assert entry["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert entry["retrieved_at"] == "2026-09-01T00:00:00+00:00"
    assert client.seen == []


def test_manifest_renders_provenance():
    log = {
        "sop-2026": {
            "url": "https://example/sop.pdf",
            "bytes": 10,
            "sha256": "abc",
            "retrieved_at": "2026-09-16T00:00:00+00:00",
            "failed_attempts": [{"url": "https://mirror", "error": "ConnectError: TLS"}],
        }
    }
    build = {
        "docs": {"sop-2026": {"kind": "pdf", "pages": 3, "text_pages": 2, "scanned_pages": [3], "chunks": 5}}
    }
    md = render_manifest(log, build)
    assert "`abc`" in md and "2026-09-16T00:00:00+00:00" in md
    assert "no text layer (scanned): [3]" in md
    assert "Failed attempt: https://mirror -> ConnectError: TLS" in md
    assert "`bnss-2023`" in md and "not fetched" in md
