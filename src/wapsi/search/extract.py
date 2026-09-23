"""PDF / HTML -> cleaned page texts.

- PDF (pypdf, imported lazily): repeated running headers/footers and page-number lines are removed, page
  numbers are kept (1-based physical pages), and pages without a text layer are reported (no OCR).
- HTML: scripts/styles dropped, block tags become line breaks, optional start/end markers trim site chrome.
"""

from __future__ import annotations

import html as html_lib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from wapsi.search.sources import Source

PageStatus = Literal["text", "blank", "scanned", "sparse_with_images"]

_EDGE = 3  # lines at the top/bottom of a page inspected for running headers/footers
_PAGE_OF = re.compile(r"^\s*page\s+(\d{1,4})\s+of\s+\d{1,4}\s*$", re.I)
_BARE_NUM = re.compile(r"^\s*[-–]?\s*(\d{1,4})\s*[-–]?\s*$")


@dataclass
class Page:
    number: int  # 1-based physical page (HTML: always 1)
    text: str
    label: str | None = None  # printed page label, e.g. "9" for "Page 9 of 98"
    status: PageStatus = "text"
    chars: int = 0  # non-space characters after cleaning
    has_images: bool = False


@dataclass
class ExtractedDoc:
    doc_id: str
    kind: Literal["pdf", "html"]
    pages: list[Page]
    warnings: list[str] = field(default_factory=list)

    @property
    def paged(self) -> bool:
        return self.kind == "pdf"

    def full_text(self) -> str:
        if not self.paged:
            return self.pages[0].text if self.pages else ""
        return "\n".join(f"\n=== PAGE {p.number} ===\n{p.text}" for p in self.pages)

    def page_text(self, number: int) -> str:
        for p in self.pages:
            if p.number == number:
                return p.text
        return ""


def sniff(data: bytes) -> Literal["pdf", "html", "json", "unknown"]:
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if data[:1024].lstrip().startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"{", b"[")):
        return "json"
    if head.startswith((b"<!doctype", b"<html", b"<?xml", b"<head", b"<body")) or b"<html" in head:
        return "html"
    return "unknown"


# --------------------------------------------------------------------------- PDF


def _page_has_images(page) -> bool:
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
        if not xobjects:
            return False
        xobjects = xobjects.get_object()
        for ref in xobjects.values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form" and _page_has_images(obj):
                return True
    except Exception:  # noqa: BLE001 - defensive: malformed resources should not stop extraction
        return False
    return False


def _edge_key(line: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", line.strip().lower()))


def _edge_indices(lines: list[str]) -> list[int]:
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    return sorted(set(nonblank[:_EDGE] + nonblank[-_EDGE:]))


def clean_pdf_pages(raw_pages: list[str]) -> tuple[list[str], list[str | None]]:
    """Remove running headers/footers and page-number lines. Returns (texts, printed labels)."""
    split = [t.splitlines() for t in raw_pages]
    counts: Counter[str] = Counter()
    for lines in split:
        counts.update({_edge_key(lines[i]) for i in _edge_indices(lines)})
    n = len(split)
    threshold = max(3, int(0.5 * n))
    boilerplate = {k for k, c in counts.items() if c >= threshold and k and n >= 3}

    texts: list[str] = []
    labels: list[str | None] = []
    for lines in split:
        edges = set(_edge_indices(lines))
        label: str | None = None
        kept: list[str] = []
        for i, line in enumerate(lines):
            if i in edges:
                m = _PAGE_OF.match(line)
                if m:
                    label = label or m.group(1)
                    continue
                m = _BARE_NUM.match(line)
                if m:
                    label = label or m.group(1)
                    continue
                if _edge_key(line) in boilerplate:
                    continue
            kept.append(line.rstrip())
        texts.append("\n".join(kept).strip("\n"))
        labels.append(label)
    return texts, labels


def extract_pdf(data: bytes, source: Source) -> ExtractedDoc:
    import pypdf  # optional dependency (extra "docs"); only needed to build the index

    reader = pypdf.PdfReader(io.BytesIO(data))
    raw_pages: list[str] = []
    images: list[bool] = []
    warnings: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            raw_pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"page {i}: text extraction failed: {exc}")
            raw_pages.append("")
        images.append(_page_has_images(page))
    texts, labels = clean_pdf_pages(raw_pages)
    pages: list[Page] = []
    for i, (text, label, has_img) in enumerate(zip(texts, labels, images, strict=True), 1):
        text = _apply_drop_regex(text, source.drop_regex)
        chars = len(re.sub(r"\s", "", text))
        raw_chars = len(re.sub(r"\s", "", raw_pages[i - 1]))
        if raw_chars == 0:
            status: PageStatus = "scanned" if has_img else "blank"
        elif chars < 40 and has_img:
            status = "sparse_with_images"
        else:
            status = "text"
        if status in ("scanned", "sparse_with_images"):
            warnings.append(f"page {i}: no usable text layer ({status}); OCR is out of scope")
        pages.append(Page(number=i, text=text, label=label, status=status, chars=chars, has_images=has_img))
    return ExtractedDoc(doc_id=source.id, kind="pdf", pages=pages, warnings=warnings)


# --------------------------------------------------------------------------- HTML

_BLOCK_TAGS = re.compile(
    r"(?i)<br\s*/?>|</?(?:p|div|tr|li|ul|ol|table|h[1-6]|section|article|header|footer|button|dt|dd|td|th)\b[^>]*>"
)


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<!--.*?-->", " ", raw)
    text = re.sub(r"(?is)<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>", " ", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _apply_drop_regex(text: str, patterns: list[str]) -> str:
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.M | re.S)
    return text


def decode_html(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_html(data: bytes, source: Source) -> ExtractedDoc:
    text = html_to_text(decode_html(data))
    warnings: list[str] = []
    if source.start_marker:
        i = text.find(source.start_marker)
        if i < 0:
            warnings.append(f"start marker not found: {source.start_marker!r}")
        else:
            text = text[i:]
    if source.end_marker:
        j = text.find(source.end_marker)
        if j < 0:
            warnings.append(f"end marker not found: {source.end_marker!r}")
        else:
            text = text[:j]
    text = _apply_drop_regex(text, source.drop_regex).strip()
    chars = len(re.sub(r"\s", "", text))
    page = Page(number=1, text=text, status="text" if chars else "blank", chars=chars)
    return ExtractedDoc(doc_id=source.id, kind="html", pages=[page], warnings=warnings)


def extract(data: bytes, source: Source) -> ExtractedDoc:
    kind = sniff(data)
    if kind == "pdf":
        return extract_pdf(data, source)
    if kind == "html":
        return extract_html(data, source)
    raise ValueError(f"{source.id}: unsupported content ({kind})")
