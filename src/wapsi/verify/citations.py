"""Citation checks: never invent a clause.

- every ``[ID]`` token in the title/body is a known citation id and is declared in ``draft.citation_ids``;
- every declared id is known;
- with a ``DocSearch``, every cited id resolves to a non-empty verbatim quote (warning otherwise).
"""

from __future__ import annotations

import re

from wapsi.citations import KNOWN_IDS
from wapsi.models import CheckResult, Draft, DraftKind
from wapsi.ports import DocSearch
from wapsi.verify.numeric import ACCOUNT_RE, draft_text
from wapsi.verify.report import fail, ok, warn

NAME = "citations"

CITATION_TOKEN_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\]")

# Kinds that must rest on at least one legal basis.
_NEEDS_CITATION = {
    DraftKind.HOLD_INSTRUCTION,
    DraftKind.STR,
    DraftKind.RESTORATION_PACK,
    DraftKind.EDD_MEMO,
    DraftKind.OUTBOUND_REQUEST,
    DraftKind.CFCFRMS_UPDATE,
}


def cited_ids(text: str) -> list[str]:
    """Citation-shaped ``[ID]`` tokens in order of first appearance (bracketed account numbers excluded)."""
    ids = [m.group(1) for m in CITATION_TOKEN_RE.finditer(text) if not ACCOUNT_RE.fullmatch(m.group(1))]
    return list(dict.fromkeys(ids))


def check_citations(draft: Draft, search: DocSearch | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    tokens = cited_ids(draft_text(draft))
    declared = list(dict.fromkeys(draft.citation_ids))

    unknown_tokens = [t for t in tokens if t not in KNOWN_IDS]
    if unknown_tokens:
        results.append(
            fail(
                NAME,
                "unknown_citation",
                "cited ids not in the citation registry: " + ", ".join(unknown_tokens),
                [f"[{t}]" for t in unknown_tokens],
            )
        )
    undeclared = [t for t in tokens if t in KNOWN_IDS and t not in declared]
    if undeclared:
        results.append(
            fail(
                NAME,
                "citation_not_declared",
                "cited in the text but missing from citation_ids: " + ", ".join(undeclared),
                [f"[{t}]" for t in undeclared],
            )
        )
    unknown_declared = [c for c in declared if c not in KNOWN_IDS]
    if unknown_declared:
        results.append(
            fail(
                NAME,
                "unknown_citation",
                "citation_ids not in the citation registry: " + ", ".join(unknown_declared),
                unknown_declared,
            )
        )
    if not declared and draft.kind in _NEEDS_CITATION:
        results.append(warn(NAME, "no_citations", f"{draft.kind} draft cites no legal basis"))

    by_url: list[str] = []
    if search is not None:
        missing = []
        for cid in dict.fromkeys(c for c in [*declared, *tokens] if c in KNOWN_IDS):
            cit = search.citation(cid)
            if cit is None or not cit.quote.strip():
                if cited_by_url(cid, cit):
                    by_url.append(cid)  # a judgment cited by its URL by design: nothing to quote, nothing missing
                    continue
                missing.append(cid)
        if missing:
            results.append(
                warn(
                    NAME,
                    "citation_quote_missing",
                    "no verbatim quote in the document corpus for: " + ", ".join(missing),
                    missing,
                )
            )

    if not any(not r.passed and r.severity == "error" for r in results):
        note = f"; cited by URL (no quote by design): {', '.join(by_url)}" if by_url else ""
        results.append(ok(NAME, f"{len(tokens)} cited id(s), {len(declared)} declared, all in the registry{note}"))
    return results


def cited_by_url(citation_id: str, found: object | None = None) -> bool:
    """True for a registry entry that is cited by its URL with no stored quote, by design (the court judgments,
    ``CASE-*``: ``data/docs/citations.json`` keeps ``quote`` empty and gives the URL). Such an id is never reported
    as a missing quote."""
    from wapsi.citations import registry

    entry = found if found is not None else registry().get(citation_id)
    if entry is None:
        entry = registry().get(citation_id)
    quote = str(getattr(entry, "quote", "") or "").strip()
    url = str(getattr(entry, "url", "") or "").strip()
    if not quote and url:
        return True
    reg = registry().get(citation_id)  # the search index may hold a copy without the URL
    return bool(reg is not None and not str(reg.quote or "").strip() and str(reg.url or "").strip())
