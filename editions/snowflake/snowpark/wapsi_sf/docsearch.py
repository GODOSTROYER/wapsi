"""``CortexDocSearch``: ``wapsi.ports.DocSearch`` over Cortex Search (``WAPSI.DOCS.SOP_SEARCH``) and the
citation registry table ``WAPSI.DOCS.CITATIONS``.

``search`` calls ``SNOWFLAKE.CORTEX.SEARCH_PREVIEW`` with the request JSON as a bind; ``citation`` reads the
registry table once (cached). If the table is missing or unreadable, citations fall back to the packaged
``wapsi.citations.registry()`` (quotes ship inside the zip), so drafting and verification never depend on the
search service being up. Only ids in ``wapsi.citations.KNOWN_IDS`` are ever returned as citations; search hits
with an unknown id keep ``citation_id=None`` (context only, never citable).
"""

from __future__ import annotations

import json
import re
from typing import Any

from wapsi.adapters.snowflake_ledger import row_dict
from wapsi.citations import KNOWN_IDS, registry
from wapsi.models import Citation, SearchHit

SERVICE = "WAPSI.DOCS.SOP_SEARCH"
CITATIONS_TABLE = "WAPSI.DOCS.CITATIONS"
COLUMNS = ["chunk_id", "chunk_text", "citation_id", "doc_id", "doc_title", "section", "source_url", "page"]
_FQN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){2}$")


def search_request(query: str, k: int = 5, citation_id: str | None = None) -> str:
    req: dict[str, Any] = {"query": query, "columns": COLUMNS, "limit": max(1, min(int(k), 50))}
    if citation_id:
        req["filter"] = {"@eq": {"citation_id": citation_id}}
    return json.dumps(req, ensure_ascii=False)


class CortexDocSearch:
    def __init__(self, session: Any, service: str = SERVICE, citations_table: str = CITATIONS_TABLE) -> None:
        if not _FQN.match(service) or not _FQN.match(citations_table):
            raise ValueError("service and citations_table must be DB.SCHEMA.NAME identifiers")
        self.session = session
        self.service = service.upper()
        self.citations_table = citations_table.upper()
        self._citations: dict[str, Citation] | None = None
        self.errors: list[str] = []

    def search(self, query: str, k: int = 5, *, citation_id: str | None = None) -> list[SearchHit]:
        sql = f"SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('{self.service}', ?) AS R"
        rows = self.session.sql(sql, params=[search_request(query, k, citation_id)]).collect()
        if not rows:
            return []
        raw = list(row_dict(rows[0]).values())[0]
        data = json.loads(raw) if isinstance(raw, str) else raw
        hits = []
        for i, r in enumerate((data or {}).get("results", [])):
            r = {str(k).lower(): v for k, v in r.items()}
            cid = r.get("citation_id") or None
            score = (r.get("@scores") or {}).get("cosine_similarity") if isinstance(r.get("@scores"), dict) else None
            hits.append(SearchHit(
                citation_id=cid if cid in KNOWN_IDS else None,
                chunk_id=str(r.get("chunk_id") or f"hit-{i}"),
                doc_id=str(r.get("doc_id") or ""),
                section=str(r.get("section") or ""),
                text=str(r.get("chunk_text") or ""),
                score=round(float(score), 6) if score is not None else round(1.0 / (i + 1), 6),
            ))
        return hits

    def _load(self) -> dict[str, Citation]:
        if self._citations is None:
            reg = registry()
            try:
                rows = self.session.sql(
                    f"SELECT citation_id, source_title, section, quote, url, page FROM {self.citations_table}"
                ).collect()
                for r in map(row_dict, rows):
                    cid = r.get("citation_id")
                    if cid in reg:
                        update = {k: v for k, v in r.items() if k != "citation_id" and v not in (None, "")}
                        if "page" in update:
                            update["page"] = int(update["page"])
                        reg[cid] = reg[cid].model_copy(update=update)
            except Exception as exc:  # noqa: BLE001 - fall back to the packaged registry
                self.errors.append(f"citations table unavailable: {type(exc).__name__}")
            self._citations = {k: v for k, v in reg.items() if k in KNOWN_IDS}
        return self._citations

    def citation(self, citation_id: str) -> Citation | None:
        c = self._load().get(citation_id)
        return c.model_copy() if c else None
