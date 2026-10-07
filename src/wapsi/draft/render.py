"""Jinja rendering, skeletons, slot-usage tracking, citation extraction and free-text sanitising for drafts.

Positional binding (docs/draft-contract.md): a template is rendered once with *markers* in place of slot values.
The result, with each marker written as ``⟦slot_key⟧``, is the draft's skeleton; the body is the skeleton with
the slot values filled in, so the verifier can re-fill it with its own recomputed values and compare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, FileSystemLoader, PackageLoader, StrictUndefined

from wapsi import money
from wapsi.citations import KNOWN_IDS
from wapsi.draft.factsheet import is_contract_key
from wapsi.money import r2

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# The verifier's token shapes (docs/draft-contract.md, "Body rules"); money, accounts, UTRs and dates come from
# wapsi.money so the drafter sanitises exactly what the verifier checks.
CITATION_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\]")
ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}-\d{10}(?![0-9])")
PLACEHOLDER_RE = re.compile(r"⟦([^⟦⟧\n]{1,160})⟧")
# private-use sentinels that carry slot keys through Jinja rendering
MARK_OPEN, MARK_CLOSE = "", ""
_MARKER_RE = re.compile("([^\\n]{1,160})")

_SMALL = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
]
_ORDINAL = [
    "zeroth",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
]


def words(n: int, zero: str = "no") -> str:
    """Small counts as words so bodies carry no stray digits ("three accounts")."""
    if n == 0:
        return zero
    if 0 < n < len(_SMALL):
        return _SMALL[n]
    return "many"


def ordinal(n: int) -> str:
    return _ORDINAL[n] if 0 <= n < len(_ORDINAL) else "deeper"


def plural(n: int, word: str, suffix: str = "s") -> str:
    return word if n == 1 else word + suffix


def _loader() -> BaseLoader:
    """Templates from the package directory, or via PackageLoader when the package is imported from a zip archive."""
    if TEMPLATE_DIR.is_dir():
        return FileSystemLoader(str(TEMPLATE_DIR))
    return PackageLoader("wapsi.draft", "templates")


@lru_cache(maxsize=1)
def environment() -> Environment:
    return Environment(
        loader=_loader(),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _tidy(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def render(template_name: str, **context: Any) -> str:
    return _tidy(environment().get_template(template_name).render(**context))


class TrackingSlots(dict[str, str]):
    """Slot pool that records which keys a template or prose substitution actually read."""

    def __init__(self, data: dict[str, str]) -> None:
        super().__init__(data)
        self.used: set[str] = set()

    def __getitem__(self, key: str) -> str:
        value = super().__getitem__(key)
        self.used.add(key)
        return value

    def peek(self, key: str) -> str:
        return super().__getitem__(key)

    def used_slots(self) -> dict[str, str]:
        return {k: self.peek(k) for k in sorted(self.used)}


def marker(key: str) -> str:
    return f"{MARK_OPEN}{key}{MARK_CLOSE}"


class MarkerSlots(Mapping[str, str]):
    """What a template sees while rendering a skeleton: reading a slot records its use and yields a marker."""

    def __init__(self, slots: TrackingSlots) -> None:
        self.slots = slots

    def __getitem__(self, key: str) -> str:
        self.slots[key]  # noqa: B018 - raises KeyError for unknown keys and records the use
        return marker(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)


def neutralize(text: str) -> str:
    """Remove anything that could read as a placeholder or a marker from literal text."""
    return text.replace("⟦", "(").replace("⟧", ")").replace(MARK_OPEN, "").replace(MARK_CLOSE, "")


def to_markers(skeleton: str) -> str:
    """``⟦key⟧`` placeholders (e.g. in an LLM prose skeleton) as markers; other text neutralised."""
    parts, last = [], 0
    for m in PLACEHOLDER_RE.finditer(skeleton):
        parts += [neutralize(skeleton[last : m.start()]), marker(m.group(1))]
        last = m.end()
    parts.append(neutralize(skeleton[last:]))
    return "".join(parts)


def markers_to_skeleton(text: str, slots: Mapping[str, str]) -> str:
    """Rendered text with markers -> skeleton with ``⟦key⟧`` placeholders (literal ``⟦ ⟧`` neutralised)."""
    text = text.replace("⟦", "(").replace("⟧", ")")
    text = _MARKER_RE.sub(lambda m: f"⟦{m.group(1)}⟧" if m.group(1) in slots else "", text)
    return text.replace(MARK_OPEN, "").replace(MARK_CLOSE, "")


def fill(skeleton: str, slots: Mapping[str, str]) -> str:
    """Skeleton -> text: every ``⟦key⟧`` replaced by its slot value (reads are tracked)."""
    return PLACEHOLDER_RE.sub(lambda m: slots[m.group(1)], skeleton)


def unmark(text: str, slots: TrackingSlots) -> str:
    """Markers (from ``TextCleaner(markers=True)``) replaced by slot values, for structured payloads."""
    text = _MARKER_RE.sub(lambda m: slots.peek(m.group(1)) if m.group(1) in slots else "", text)
    return text.replace(MARK_OPEN, "").replace(MARK_CLOSE, "")


class Cleaned(str):
    """Text produced by ``TextCleaner``: its markers are genuine and survive ``sanitize_context``."""

    __slots__ = ()


def sanitize_context(obj: Any) -> Any:
    """Template context with every plain string neutralised, so data (names, labels) can never forge a slot."""
    if isinstance(obj, Cleaned):
        return obj
    if isinstance(obj, str):
        return neutralize(obj)
    if isinstance(obj, dict):
        return {k: sanitize_context(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [sanitize_context(v) for v in obj]
    return obj


def render_skeleton(
    template_name: str, slots: TrackingSlots, c: dict[str, Any], prose_skeleton: str = ""
) -> str:
    """The template's skeleton: literal text plus ``⟦key⟧`` wherever a slot value goes."""
    raw = (
        environment()
        .get_template(template_name)
        .render(s=MarkerSlots(slots), c=sanitize_context(c), prose=to_markers(prose_skeleton))
    )
    return _tidy(markers_to_skeleton(raw, slots))


def cited_ids(text: str) -> list[str]:
    """``[ID]`` tokens in order of first appearance (bracketed account numbers are not citations)."""
    ids = [m.group(1) for m in CITATION_RE.finditer(text) if not ACCOUNT_RE.fullmatch(m.group(1))]
    return list(dict.fromkeys(ids))


def checked_citations(text: str) -> list[str]:
    ids = cited_ids(text)
    unknown = [i for i in ids if i not in KNOWN_IDS]
    if unknown:
        raise ValueError(f"template cites unknown ids: {unknown}")
    return ids


def amount_index(slots: TrackingSlots) -> dict[float, str]:
    """rupee value -> slot key, for slots whose whole value is one amount. Contract keys win over extensions."""
    index: dict[float, str] = {}
    keys = sorted(slots, key=lambda k: (not is_contract_key(k), k))
    for key in keys:
        found = money.is_single_amount(slots.peek(key))
        if found is not None:
            index.setdefault(r2(found.value), key)
    return index


class TextCleaner:
    """Makes free text from other modules (classifier reasons, payer remarks, ATM labels) safe for a body.

    Amounts (in digits or words, ``wapsi.money.find_money_mentions``) become the matching slot (recorded as used;
    a marker when ``markers=True``, for skeleton rendering) or "an amount"; unknown account numbers, UTRs and
    dates (any spelling) are withheld; placeholder / template syntax and unknown citation tokens are removed.
    """

    def __init__(
        self,
        slots: TrackingSlots,
        accounts: Iterable[str],
        utrs: Iterable[str],
        dates: Iterable[str],
        limit: int = 300,
        *,
        markers: bool = False,
    ) -> None:
        self.slots = slots
        self.accounts = {a.upper() for a in accounts}
        self.utrs = {u.upper() for u in utrs}
        self.dates = set(dates)
        self.limit = limit
        self.markers = markers
        self._index = amount_index(slots)

    def _amount(self, value: float) -> str:
        key = self._index.get(r2(value))
        if key is None:
            return "an amount"
        text = self.slots[key]  # records the use
        return marker(key) if self.markers else text

    def _replacements(self, s: str) -> list[tuple[int, int, str]]:
        out: list[tuple[int, int, str]] = []

        def add(found: list[tuple[str, str | None]], keep: set[str], withheld: str, canonical: bool) -> None:
            pos = 0
            for span, canon in found:
                start = s.find(span, pos)
                if start < 0:
                    continue
                pos = start + len(span)
                if canon is not None and canon in keep:
                    out.append((start, pos, canon if canonical else span))
                else:
                    out.append((start, pos, withheld))

        add(money.find_account_ids(s), self.accounts, "(account withheld)", True)
        add(money.find_utrs(s), self.utrs, "(reference withheld)", False)
        add(money.find_dates(s), self.dates, "(date withheld)", False)
        out += [(m.start, m.end, self._amount(m.value)) for m in money.find_money_mentions(s)]
        return out

    def __call__(self, text: object) -> Cleaned:
        s = " ".join(str(text or "").split())
        if len(s) > self.limit:
            s = s[: self.limit - 1].rstrip() + "…"
        s = neutralize(s).replace("{{", "{ {").replace("{%", "{ %").replace("{#", "{ #")
        s = CITATION_RE.sub(lambda m: m.group(0) if m.group(1) in KNOWN_IDS else "", s)
        # identifiers and dates win over amounts that overlap them; earliest, then longest, span first
        chosen: list[tuple[int, int, str]] = []
        for start, end, rep in sorted(self._replacements(s), key=lambda r: (r[0], -(r[1] - r[0]))):
            if chosen and start < chosen[-1][1]:
                continue
            chosen.append((start, end, rep))
        for start, end, rep in reversed(chosen):
            s = s[:start] + rep + s[end:]
        return Cleaned(" ".join(s.split()))
