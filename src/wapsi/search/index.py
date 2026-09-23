"""LocalDocSearch: BM25 over ``data/docs/index/chunks.jsonl``; implements :class:`wapsi.ports.DocSearch`.

``LocalDocSearch.load()`` never needs the network or pypdf. If the index has not been built (it is
gitignored), it falls back to a tiny corpus made of the committed verbatim quotes in ``citations.json`` and
emits a warning, so callers still get citation-bearing hits.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from wapsi.citations import KNOWN_IDS, registry
from wapsi.models import Citation, SearchHit
from wapsi.search.bm25 import BM25, K1, B, tokenize
from wapsi.search.chunk import DocChunk
from wapsi.search.sources import CITATIONS_JSON, INDEX_DIR, source_for_url

_CITATION_FIELDS = set(Citation.model_fields)

CitationsArg = Mapping[str, Citation | Mapping[str, Any]] | Iterable[Citation | Mapping[str, Any]] | None


def _to_citation(obj: Citation | Mapping[str, Any]) -> Citation:
    if isinstance(obj, Citation):
        return obj.model_copy()
    return Citation.model_validate({k: v for k, v in obj.items() if k in _CITATION_FIELDS})


def _coerce_citations(citations: CitationsArg) -> dict[str, Citation]:
    if citations is None:
        return {}
    items = citations.values() if isinstance(citations, Mapping) else citations
    out: dict[str, Citation] = {}
    for obj in items:
        c = _to_citation(obj)
        out[c.citation_id] = c
    return out


def load_citations(path: Path | None = None) -> dict[str, Citation]:
    """Canonical registry (``wapsi.citations``) overlaid with ``path``; only known IDs are returned."""
    reg = registry()
    path = path or CITATIONS_JSON
    if path.exists():
        for entry in json.loads(path.read_text(encoding="utf-8")):
            cid = entry.get("citation_id")
            if cid in reg:
                update = {k: v for k, v in entry.items() if k in _CITATION_FIELDS and k != "citation_id"}
                reg[cid] = reg[cid].model_copy(update=update)
    return {cid: c for cid, c in reg.items() if cid in KNOWN_IDS}


def read_chunks(path: Path) -> list[DocChunk]:
    chunks: list[DocChunk] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                chunks.append(DocChunk.model_validate_json(line))
    return chunks


def citation_chunks(citations: Mapping[str, Citation]) -> list[DocChunk]:
    """Fallback corpus: one chunk per citation that has a verbatim quote."""
    out: list[DocChunk] = []
    for cid, c in citations.items():
        if not c.quote:
            continue
        src = source_for_url(c.url)
        out.append(
            DocChunk(
                chunk_id=f"citation-{cid}",
                doc_id=src.id if src else "citation",
                section=c.section,
                section_title=c.source_title,
                page=c.page,
                page_end=c.page,
                citation_id=cid,
                citation_ids=[cid],
                text=c.quote,
                source_title=c.source_title,
            )
        )
    return out


class LocalDocSearch:
    def __init__(
        self,
        chunks: Iterable[DocChunk | Mapping[str, Any]],
        citations: CitationsArg = None,
        *,
        k1: float = K1,
        b: float = B,
        fallback: bool = False,
    ) -> None:
        self._chunks = [c if isinstance(c, DocChunk) else DocChunk.model_validate(c) for c in chunks]
        self._by_id = {c.chunk_id: c for c in self._chunks}
        self._citations = _coerce_citations(citations)
        self._bm25 = BM25([tokenize(f"{c.section} {c.section_title} {c.text}") for c in self._chunks], k1, b)
        self.fallback = fallback  # True when serving the citations-only corpus

    # ------------------------------------------------------------------ constructors
    @classmethod
    def from_chunks(
        cls, chunks: Iterable[DocChunk | Mapping[str, Any]], citations: CitationsArg = None, **kwargs: Any
    ) -> LocalDocSearch:
        return cls(chunks, citations, **kwargs)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        citations_path: str | Path | None = None,
        allow_fallback: bool = True,
    ) -> LocalDocSearch:
        """``path``: index directory or a ``chunks.jsonl`` file (default ``data/docs/index``)."""
        p = Path(path) if path is not None else INDEX_DIR
        chunks_file = p if p.suffix == ".jsonl" else p / "chunks.jsonl"
        if citations_path is None:
            sibling = chunks_file.parent.parent / "citations.json"
            citations_path = sibling if sibling.exists() else CITATIONS_JSON
        citations = load_citations(Path(citations_path))
        if chunks_file.exists():
            return cls(read_chunks(chunks_file), citations)
        if not allow_fallback:
            raise FileNotFoundError(f"{chunks_file} not found; run: uv run python -m wapsi.search.build")
        warnings.warn(
            f"{chunks_file} not found; serving citations-only corpus "
            "(run uv run python -m wapsi.search.fetch && uv run python -m wapsi.search.build)",
            stacklevel=2,
        )
        return cls(citation_chunks(citations), citations, fallback=True)

    # ------------------------------------------------------------------ DocSearch
    def search(self, query: str, k: int = 5, *, doc_ids: Iterable[str] | None = None) -> list[SearchHit]:
        return [
            SearchHit(
                citation_id=c.citation_id,
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                section=c.section,
                text=c.text,
                score=round(score, 6),
            )
            for c, score in self.search_chunks(query, k, doc_ids=doc_ids)
        ]

    def citation(self, citation_id: str) -> Citation | None:
        c = self._citations.get(citation_id)
        return c.model_copy() if c else None

    # ------------------------------------------------------------------ extras
    def search_chunks(
        self, query: str, k: int = 5, *, doc_ids: Iterable[str] | None = None
    ) -> list[tuple[DocChunk, float]]:
        if k <= 0:
            return []
        terms = tokenize(query)
        allowed = set(doc_ids) if doc_ids is not None else None
        scored = [
            (i, s)
            for i, s in self._bm25.scores(terms).items()
            if s > 0.0 and (allowed is None or self._chunks[i].doc_id in allowed)
        ]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [(self._chunks[i], s) for i, s in scored[:k]]

    def chunk(self, chunk_id: str) -> DocChunk | None:
        return self._by_id.get(chunk_id)

    def chunks_for_citation(self, citation_id: str) -> list[DocChunk]:
        return [c for c in self._chunks if citation_id in c.citation_ids]

    @property
    def chunks(self) -> list[DocChunk]:
        return list(self._chunks)

    @property
    def citation_ids(self) -> list[str]:
        return sorted(self._citations)

    def __len__(self) -> int:
        return len(self._chunks)
