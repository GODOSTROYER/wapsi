"""Pure-Python Okapi BM25 (no third-party dependencies, so it runs inside Snowflake / Cloud Run as is).

Tokenizer: NFKC + lowercase, Indian legal abbreviations normalised (``s.``/``sec.``/``u/s`` -> ``section``,
BNSS / PMLA / CrPC spellings, ``A/c`` -> ``account``, ``Rs.``/``₹`` -> ``rs``), digit grouping commas removed
(``50,000`` -> ``50000``), ``tip-off`` / ``tipping off`` -> ``tipoff``, hyphenated words emitted as parts plus
the joined form, English stopwords dropped, and a light suffix stemmer (``_stem``).

Scoring: ``idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5))`` (non-negative) and
``tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))``; each distinct query term counts once.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence

K1 = 1.5
B = 0.75

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each either else etc few for from further had
    has have having he her here hers herself him himself his how i if in into is it its itself just may me
    might more most must my myself no nor not of off on once only or other ought our ours ourselves out over
    own per same shall she should so some such than that the their theirs them themselves then there thereof
    these they this those through thus to too under until up upon very via was we were what when where
    whether which while who whom whose why will with within without would you your yours yourself yourselves
    """.split()
)

_ABBREVIATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\btip(?:ping)?[\s-]*off\b"), " tipoff "),
    (re.compile(r"\bu/ss?\b\.?"), " section "),
    (re.compile(r"\br/w\b"), " read with "),
    (re.compile(r"\b(?:secs?|ss?)\.\s*(?=\d)"), " section "),
    (re.compile(r"\bsections?\b"), " section "),
    (re.compile(r"\bb\.\s?n\.\s?s\.\s?s\b\.?"), " bnss "),
    (re.compile(r"\bbharatiya\s+nagarik\s+suraksha\s+sanhita\b"), " bnss "),
    (re.compile(r"\bp\.\s?m\.\s?l\.\s?a\b\.?"), " pmla "),
    (re.compile(r"\bprevention\s+of\s+money[\s-]*laundering\s+act\b"), " pmla "),
    (re.compile(r"\bpml\s+act\b"), " pmla "),
    (re.compile(r"\bprevention\s+of\s+money[\s-]*laundering\s+\(maintenance\s+of\s+records\)\s+rules\b"),
     " pml rules "),
    (re.compile(r"\bpmlr\b"), " pml rules "),
    (re.compile(r"\bcr\.\s?p\.\s?c\b\.?"), " crpc "),
    (re.compile(r"\bcode\s+of\s+criminal\s+procedure\b"), " crpc "),
    (re.compile(r"\ba/cs?\b\.?"), " account "),
    (re.compile(r"\bacc(?:oun)?ts?\b\.?"), " account "),
    (re.compile(r"\brs\b\.?\s*(?=\d)"), " rs "),
    (re.compile(chr(0x20B9)), " rs "),  # rupee sign
    (re.compile(r"(?<=\d),(?=\d)"), ""),
]  # fmt: skip

_DASHES = re.compile("[" + chr(0x2010) + "-" + chr(0x2015) + chr(0x2212) + "]")
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


_VOWELS = frozenset("aeiouy")


def _strip_plural(tok: str) -> str:
    if tok.endswith("ies") and not tok.endswith(("eies", "aies")):
        return tok[:-3] + "y"
    if tok.endswith(("sses", "xes", "ches", "shes", "zzes")):
        return tok[:-2]
    if tok.endswith("es") and not tok.endswith(("aes", "ees", "oes")):
        return tok[:-1]
    if tok.endswith("s") and not tok.endswith(("us", "ss", "is")):
        return tok[:-1]
    return tok


def _strip_verbal(tok: str) -> str:
    for suffix, min_len in (("ing", 6), ("ed", 5)):
        if tok.endswith(suffix) and len(tok) >= min_len:
            stem = tok[: -len(suffix)]
            if len(stem) < 3 or not _VOWELS & set(stem):
                return tok
            if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS and stem[-1] not in "lsz":
                stem = stem[:-1]  # putting -> put, tipping -> tip
            return stem
    return tok


def _stem(tok: str) -> str:
    """Light suffix stripping, applied identically to documents and queries:
    plural, then -ing/-ed, then a final -e (holds/holding -> hold, freeze/freezing -> freez)."""
    if len(tok) <= 3 or not tok.isalpha():
        return tok
    tok = _strip_verbal(_strip_plural(tok))
    if tok.endswith("e") and len(tok) > 4:
        tok = tok[:-1]
    return tok


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace(chr(0x2019), "'").replace(chr(0x2018), "'")
    text = _DASHES.sub("-", text)
    for pattern, repl in _ABBREVIATIONS:
        text = pattern.sub(repl, text)
    return text


def _emit(tok: str) -> Iterable[str]:
    if tok in STOPWORDS:
        return
    if len(tok) == 1 and not tok.isdigit():
        return
    yield _stem(tok)


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.finditer(normalize_text(text)):
        tok = m.group(0)
        if "-" in tok:
            parts = [p for p in tok.split("-") if p]
            for p in parts:
                out.extend(_emit(p))
            out.extend(_emit("".join(parts)))
        else:
            out.extend(_emit(tok))
    return out


class BM25:
    """Okapi BM25 over pre-tokenised documents with an inverted index."""

    def __init__(self, docs: Sequence[Sequence[str]], k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.doc_len = [len(d) for d in docs]
        self.n_docs = len(docs)
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, doc in enumerate(docs):
            for term, tf in Counter(doc).items():
                self.postings[term].append((i, tf))
        self.df = {t: len(p) for t, p in self.postings.items()}

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def scores(self, query_terms: Iterable[str]) -> dict[int, float]:
        acc: dict[int, float] = defaultdict(float)
        avgdl = self.avgdl or 1.0
        for term in dict.fromkeys(query_terms):  # distinct terms, query order kept
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            for i, tf in postings:
                denom = tf + self.k1 * (1.0 - self.b + self.b * self.doc_len[i] / avgdl)
                acc[i] += idf * tf * (self.k1 + 1.0) / denom
        return acc

    def score(self, query_terms: Iterable[str], doc_index: int) -> float:
        return self.scores(query_terms).get(doc_index, 0.0)

    def top_k(self, query_terms: Iterable[str], k: int) -> list[tuple[int, float]]:
        if k <= 0:
            return []
        ranked = sorted(self.scores(query_terms).items(), key=lambda kv: (-kv[1], kv[0]))
        return [(i, s) for i, s in ranked[:k] if s > 0.0]
