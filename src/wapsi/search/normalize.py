"""Text normalisation shared by chunking and quote verification.

Quote verification compares text after :func:`normalize` (NFKC, typographic quotes and dashes folded to
ASCII, whitespace collapsed). Because pypdf sometimes splits words ("fra udulent", "2 nd June"), a second,
whitespace-free comparison (:func:`despace`) is accepted too; it still requires every non-space character
to match in order.
"""

from __future__ import annotations

import re
import unicodedata

# code points (not literal characters, several of which are invisible) -> replacement
_FOLD_MAP = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'",  # single quotes, prime
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x2033: '"',  # double quotes, double prime
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-", 0x2212: "-",  # dashes
    0x00A0: " ", 0x2007: " ", 0x202F: " ",  # no-break spaces
    0x200B: "", 0x200C: "", 0x200D: "", 0xFEFF: "", 0x00AD: "",  # zero-width chars, BOM, soft hyphen
}  # fmt: skip
_FOLD = str.maketrans({chr(cp): repl for cp, repl in _FOLD_MAP.items()})
_WS = re.compile(r"\s+")
SEGMENT_SEP = chr(0x2026)  # "…" separates non-contiguous parts of one quote
MAX_SEGMENT_GAP = 600  # max normalised characters between two quote segments


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    return _WS.sub(" ", text).strip()


def despace(text: str) -> str:
    return _WS.sub("", normalize(text))


def quote_segments(quote: str) -> list[str]:
    return [s.strip() for s in quote.split(SEGMENT_SEP) if s.strip()]


_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def quote_word_count(quote: str) -> int:
    """Strict word count: runs of letters/digits, so "first-layer" and "50,000" count as two words each.

    Never lower than a plain whitespace split (ignoring the "…" separator).
    """
    runs = len(_WORD.findall(quote))
    tokens = sum(1 for tok in quote.split() if any(ch.isalnum() for ch in tok))
    return max(runs, tokens)


def _find_in_order(segments: list[str], hay: str) -> bool:
    pos = 0
    first = True
    for seg in segments:
        idx = hay.find(seg, pos)
        if idx < 0:
            return False
        if not first and idx - pos > MAX_SEGMENT_GAP:
            return False
        pos = idx + len(seg)
        first = False
    return True


def match_quote(quote: str, text: str) -> str | None:
    """Return "exact" / "despaced" if every segment of ``quote`` occurs (in order) in ``text``, else None."""
    segments = quote_segments(quote)
    if not segments:
        return None
    if _find_in_order([normalize(s) for s in segments], normalize(text)):
        return "exact"
    if _find_in_order([despace(s) for s in segments], despace(text)):
        return "despaced"
    return None


class QuoteMatcher:
    """Pre-normalises a haystack once so many quotes can be tested cheaply."""

    def __init__(self, text: str) -> None:
        self.norm = normalize(text)
        self.flat = _WS.sub("", self.norm)

    def match(self, quote: str) -> str | None:
        segments = quote_segments(quote)
        if not segments:
            return None
        if _find_in_order([normalize(s) for s in segments], self.norm):
            return "exact"
        if _find_in_order([despace(s) for s in segments], self.flat):
            return "despaced"
        return None
