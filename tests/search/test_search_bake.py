"""``python -m wapsi.search.bake``: the docs search index a container image carries (no network: downloads and the
PDF build are replaced by fakes that write what the real ones would)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from wapsi.search import bake as bake_mod
from wapsi.search import sources as src
from wapsi.search.index import LocalDocSearch, citation_chunks, load_citations


def _docs(root: Path) -> Path:
    """A docs folder as a fresh clone has it: the committed citation corpus, no index, no raw documents."""
    docs = root / "docs"
    docs.mkdir()
    shutil.copy(src.CITATIONS_JSON, docs / "citations.json")
    return docs


def _write_index(index_dir: Path, n_copies: int = 2) -> int:
    """An index made of real ``DocChunk``s (the citation quotes, as the BM25 corpus would hold them)."""
    chunks = citation_chunks(load_citations())
    index_dir.mkdir(parents=True, exist_ok=True)
    lines = [c.model_copy(update={"chunk_id": f"{c.chunk_id}-{i}"}).model_dump_json()
             for i in range(n_copies) for c in chunks]
    (index_dir / "chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def test_a_shipped_index_is_validated_and_kept(workdir):
    docs = _docs(workdir)
    n = _write_index(docs / "index")
    rep = bake_mod.bake(docs)
    assert rep["status"] == bake_mod.PRESENT and rep["chunks"] == n and rep["citation_ids"] > 10
    assert json.loads((docs / "index" / bake_mod.BAKE_REPORT).read_text(encoding="utf-8"))["status"] == "present"
    search = LocalDocSearch.load(docs / "index", allow_fallback=False)  # what the app loads at run time
    assert not search.fallback and search.search("amount specific hold tainted", k=3)


def test_no_index_and_no_network_is_a_warning_not_a_failed_build(workdir, capsys):
    docs = _docs(workdir)
    with pytest.raises(bake_mod.BakeError, match="pass --fetch"):
        bake_mod.bake(docs)
    assert bake_mod.main(["--docs-dir", str(docs)]) == 0  # the image build goes on: citations-only corpus
    assert "WARNING: docs index not baked" in capsys.readouterr().out
    assert bake_mod.main(["--docs-dir", str(docs), "--strict"]) == 1
    assert not (docs / "index" / "chunks.jsonl").exists()


def _fake_network(monkeypatch, *, fail: set[str] = frozenset()):
    """``fetch_source`` writes each raw document (or fails for ``fail``); ``build`` turns raw/ into chunks.jsonl."""
    from wapsi.search import build as build_mod
    from wapsi.search import fetch as fetch_mod

    calls: list[str] = []

    def fetch_source(client, source, log, *, force=False, raw_dir=src.RAW_DIR):
        calls.append(source.id)
        if source.id in fail:
            return {"status": "failed", "failed_attempts": [{"url": source.url, "error": "ConnectError"}]}
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{source.id}.{source.type}").write_bytes(b"%PDF-1.7 synthetic stand-in")
        return {"status": "downloaded", "filename": f"{source.id}.{source.type}", "bytes": 27}

    def build(raw_dir, index_dir, citations_path=src.CITATIONS_JSON, only=None):
        present = [s.id for s in src.SOURCES if (raw_dir / f"{s.id}.{s.type}").exists()]
        _write_index(index_dir, n_copies=1)
        return {"docs": {d: {"chunks": 1} for d in present}, "total_chunks": len(present), "errors": []}

    monkeypatch.setattr(fetch_mod, "fetch_source", fetch_source)
    monkeypatch.setattr(fetch_mod, "PAUSE_S", 0.0)
    monkeypatch.setattr(build_mod, "build", build)
    return calls


def test_fetch_then_build_then_prune_the_raw_documents(workdir, monkeypatch):
    calls = _fake_network(monkeypatch)
    docs = _docs(workdir)
    rep = bake_mod.bake(docs, fetch=True, prune_raw=True)
    assert rep["status"] == bake_mod.FETCHED and rep["chunks"] > 0
    assert calls == [s.id for s in src.SOURCES]
    assert not (docs / "raw").exists()  # the image keeps only the index
    assert (docs / "index" / "chunks.jsonl").exists()
    # the committed MANIFEST.md is not touched by a bake (only a maintainer's fetch refreshes it)
    assert not (docs / "MANIFEST.md").exists()


def test_raw_documents_on_disk_are_built_without_downloading(workdir, monkeypatch):
    calls = _fake_network(monkeypatch)
    docs = _docs(workdir)
    (docs / "raw").mkdir()
    (docs / "raw" / "sop-2026.pdf").write_bytes(b"%PDF-1.7 synthetic stand-in")
    rep = bake_mod.bake(docs, fetch=True)
    assert rep["status"] == bake_mod.BUILT and calls == [] and (docs / "raw").exists()


def test_a_failed_download_leaves_the_citations_only_corpus(workdir, monkeypatch, capsys):
    _fake_network(monkeypatch, fail={s.id for s in src.SOURCES})
    docs = _docs(workdir)
    assert bake_mod.main(["--docs-dir", str(docs), "--fetch", "--prune-raw"]) == 0
    out = capsys.readouterr().out
    assert "could not download" in out and "citations-only corpus" in out
    assert not (docs / "index" / "chunks.jsonl").exists() and not (docs / "raw").exists()


def test_a_corrupt_shipped_index_is_rebuilt(workdir, monkeypatch):
    _fake_network(monkeypatch)
    docs = _docs(workdir)
    (docs / "index").mkdir()
    (docs / "index" / "chunks.jsonl").write_text('{"not": "a chunk"}\n', encoding="utf-8")
    rep = bake_mod.bake(docs, fetch=True, prune_raw=True)
    assert rep["status"] == bake_mod.FETCHED
    assert any("shipped index unusable" in p for p in rep["problems"])
    with pytest.raises(bake_mod.BakeError, match="holds no chunks|does not load"):
        (docs / "index" / "chunks.jsonl").write_text("", encoding="utf-8")
        bake_mod.validate(docs / "index")
