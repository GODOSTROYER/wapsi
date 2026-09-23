"""Document -> overlapping chunks with {doc_id, section, page, citation_id} metadata.

A chunk gets citation IDs from two signals:
1. it contains the verbatim quote of a citation sourced from this document (most specific), and
2. its section key (or a parent key) is mapped in ``Source.section_citations``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from wapsi.search.extract import ExtractedDoc
from wapsi.search.normalize import QuoteMatcher
from wapsi.search.sections import Section, candidate_keys, detect_sections, mapped_citations
from wapsi.search.sources import Source

MAX_CHARS = 900
OVERLAP_CHARS = 220  # >= the longest 25-word quote, so every quote fits inside at least one chunk
_SENTENCE_END = (".", ";", ":", ".”", '."')


class DocChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    doc_id: str
    section: str
    section_key: str = ""
    section_title: str = ""
    page: int | None = None
    page_end: int | None = None
    citation_id: str | None = None
    citation_ids: list[str] = Field(default_factory=list)
    text: str
    source_title: str = ""


@dataclass
class SectionWindow:
    text: str
    page: int
    page_end: int


def split_words(section: Section) -> list[tuple[str, int]]:
    return [(w, line.page) for line in section.lines for w in line.text.split()]


def window_section(
    section: Section, max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS
) -> list[SectionWindow]:
    """Greedy word packing (<= max_chars), snapping to a sentence end past 60 % of the window, with overlap."""
    words = split_words(section)
    out: list[SectionWindow] = []
    i = 0
    n = len(words)
    while i < n:
        j = i
        length = 0
        while j < n and length + len(words[j][0]) + (1 if j > i else 0) <= max_chars:
            length += len(words[j][0]) + (1 if j > i else 0)
            j += 1
        if j == i:  # a single token longer than max_chars
            j = i + 1
        if j < n:
            running = 0
            best = None
            for t in range(i, j):
                running += len(words[t][0]) + 1
                if running >= 0.6 * max_chars and words[t][0].endswith(_SENTENCE_END):
                    best = t + 1
            if best is not None:
                j = best
        chunk_words = words[i:j]
        out.append(
            SectionWindow(
                text=" ".join(w for w, _ in chunk_words), page=chunk_words[0][1], page_end=chunk_words[-1][1]
            )
        )
        if j >= n:
            break
        k = j
        tail = 0
        while k - 1 > i and tail + len(words[k - 1][0]) + 1 <= overlap:
            k -= 1
            tail += len(words[k][0]) + 1
        i = k if k > i else j
    return out


def order_citations(contained: list[str], mapped: list[str]) -> list[str]:
    first = [c for c in mapped if c in contained]
    rest_contained = [c for c in contained if c not in first]
    rest_mapped = [c for c in mapped if c not in first]
    out: list[str] = []
    for c in first + rest_contained + rest_mapped:
        if c not in out:
            out.append(c)
    return out


def indexable(section: Section, source: Source) -> bool:
    if not source.index_sections:
        return True
    return section.path[0] in set(source.index_sections)


def chunk_document(
    doc: ExtractedDoc,
    source: Source,
    quotes: dict[str, str] | None = None,
    *,
    max_chars: int = MAX_CHARS,
    overlap: int = OVERLAP_CHARS,
    sections: list[Section] | None = None,
) -> list[DocChunk]:
    """``quotes``: citation_id -> verbatim quote for citations sourced from this document."""
    quotes = {k: v for k, v in (quotes or {}).items() if v}
    sections = sections if sections is not None else detect_sections(doc, source)
    chunks: list[DocChunk] = []
    for section in sections:
        if not indexable(section, source):
            continue
        mapped = mapped_citations(section.path, source.section_citations)
        for win in window_section(section, max_chars, overlap):
            matcher = QuoteMatcher(win.text)
            contained = [cid for cid, q in quotes.items() if matcher.match(q)]
            cids = order_citations(contained, mapped)
            chunks.append(
                DocChunk(
                    chunk_id=f"{source.id}-{len(chunks):04d}",
                    doc_id=source.id,
                    section=section.label,
                    section_key=section.key,
                    section_title=section.title,
                    page=win.page if doc.paged else None,
                    page_end=win.page_end if doc.paged else None,
                    citation_id=cids[0] if cids else None,
                    citation_ids=cids,
                    text=win.text,
                    source_title=source.title,
                )
            )
    return chunks


def section_keys(sections: list[Section]) -> set[str]:
    keys: set[str] = set()
    for s in sections:
        keys.update(candidate_keys(s.path))
    return keys
