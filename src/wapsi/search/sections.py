"""Section structure detection for the legal corpus.

Each profile turns the cleaned lines of a document into :class:`Section` blocks with a hierarchical ``path``
rendered as a compact key:

- ``sop``   : ``2``, ``5(ix)``, ``9.1(ii)``, ``10.1``, ``11 Process 1(iv)``, ``12(x)``, ``Annexure-I``,
              ``Annexure-V Illustration 2``
- ``bnss``  : ``94``, ``106`` ... (body headings ``94. Title.—``; the arrangement of sections is ignored)
- ``pmlr``  : ``2``, ``2(1)(g)``, ``8``
- ``rbi_dir``: ``Introduction``, ``1`` ... ``70``, ``Footnotes`` (RBI Directions, 2026 format: paragraphs
              numbered straight through the chapters)
- ``faq``   : the question text
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from wapsi.search.extract import ExtractedDoc
from wapsi.search.sources import Source

ROMANS = [
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii", "xviii", "xix", "xx",
    "xxi", "xxii", "xxiii", "xxiv", "xxv",
]  # fmt: skip


@dataclass
class Line:
    page: int
    text: str


@dataclass
class Section:
    path: tuple[str, ...]
    title: str
    label_prefix: str  # used for numbered keys, e.g. "SOP Para"
    other_prefix: str = ""  # used for named keys, e.g. "SOP" -> "SOP Annexure-I"
    lines: list[Line] = field(default_factory=list)

    @property
    def key(self) -> str:
        return render_key(self.path)

    @property
    def label(self) -> str:
        key = self.key
        prefix = self.label_prefix if re.match(r"^\d", key) else self.other_prefix
        return f"{prefix} {key}".strip()

    @property
    def page(self) -> int | None:
        return self.lines[0].page if self.lines else None

    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)


def render_key(path: tuple[str, ...] | list[str]) -> str:
    if not path:
        return ""
    out = path[0]
    for part in path[1:]:
        if re.fullmatch(r"\d+(?:\.\d+)+", part):
            out = part  # a dotted number already contains its parent ("9" -> "9.1")
        elif part.startswith(("Process ", "Illustration ")):
            out = f"{out} {part}"
        else:
            out = f"{out}({part})"
    return out


def candidate_keys(path: tuple[str, ...] | list[str]) -> list[str]:
    """Most specific first: ("9", "9.1", "ii") -> ["9.1(ii)", "9.1", "9"]."""
    keys: list[str] = []
    for i in range(len(path), 0, -1):
        k = render_key(tuple(path[:i]))
        if k not in keys:
            keys.append(k)
    return keys


def mapped_citations(path: tuple[str, ...], mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key in candidate_keys(path):
        cid = mapping.get(key)
        if cid and cid not in out:
            out.append(cid)
    return out


def iter_lines(doc: ExtractedDoc, skip_pages: list[int] | None = None) -> Iterator[Line]:
    skip = set(skip_pages or [])
    for page in doc.pages:
        if page.number in skip:
            continue
        for raw in page.text.splitlines():
            text = raw.strip()
            if text:
                yield Line(page.number, text)


class _Builder:
    def __init__(self, label_prefix: str, other_prefix: str, front_matter: str) -> None:
        self.label_prefix = label_prefix
        self.other_prefix = other_prefix
        self.sections: list[Section] = []
        self.current = self._new((front_matter,), front_matter)

    def _new(self, path: tuple[str, ...], title: str) -> Section:
        return Section(path=path, title=title, label_prefix=self.label_prefix, other_prefix=self.other_prefix)

    def start(self, path: tuple[str, ...], title: str) -> None:
        if self.current.lines:
            self.sections.append(self.current)
        self.current = self._new(path, title)

    def add(self, line: Line) -> None:
        self.current.lines.append(line)

    def finish(self) -> list[Section]:
        if self.current.lines:
            self.sections.append(self.current)
        return self.sections


# --------------------------------------------------------------------------- SOP

_SOP_TOP = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Za-z].*)$")
_SOP_SUB = re.compile(r"^(\d{1,2})\.(\d{1,2})\s+([A-Z].*)$")
_SOP_ITEM = re.compile(r"^\(?([ivx]{1,5}|[a-z])[.)]\s+(\S.*)$")
_SOP_PROCESS = re.compile(r"^Process\s*(\d)\s*:\s*-?\s*(.*)$")
_SOP_ANNEX = re.compile(r"^annexure\s*[-–]?\s*(i{1,3}|iv|v|vi)\s*$", re.I)
_SOP_ILLUS = re.compile(r"^Illustration\s*(\d+)\s*:\s*(.*)$")
_SOP_PART = re.compile(r"^(?:section\s*-\s*(?:i{1,3}|iv)|annexures|background)\s*$", re.I)
_SOP_ABBR = re.compile(r"^abbreviations\s*$", re.I)


def detect_sop(doc: ExtractedDoc, source: Source) -> list[Section]:
    b = _Builder("SOP Para", "SOP", source.front_matter)
    top = 0
    top_title = ""
    sub: str | None = None
    sub_title = ""
    process: str | None = None
    process_title = ""
    annex: str | None = None
    last_roman = -1
    last_letter: str | None = None

    def base() -> tuple[str, ...]:
        if process:
            return (str(top), process)
        if sub:
            return (str(top), sub)
        return (str(top),)

    for line in iter_lines(doc, source.skip_pages):
        t = line.text
        if line.page < source.headings_from_page:
            if _SOP_ABBR.match(t):
                b.start(("Abbreviations",), "Abbreviations")
            b.add(line)
            continue
        if _SOP_PART.match(t):
            if t.lower().startswith("annexures"):
                annex = "Annexures"
                b.start(("Annexures",), "Annexures")
            continue
        m = _SOP_ANNEX.match(t)
        if m:
            annex = f"Annexure-{m.group(1).upper()}"
            b.start((annex,), annex)
            b.add(line)
            continue
        if annex:
            m = _SOP_ILLUS.match(t)
            if m:
                b.start(
                    (annex, f"Illustration {m.group(1)}"), f"Illustration {m.group(1)}: {m.group(2)}".strip()
                )
            b.add(line)
            continue
        if top == 0 and _SOP_ABBR.match(t):
            b.start(("Abbreviations",), "Abbreviations")
            b.add(line)
            continue
        m = _SOP_TOP.match(t)
        if m and int(m.group(1)) == top + 1:
            top, top_title = int(m.group(1)), m.group(2).strip()
            sub, process, last_roman, last_letter = None, None, -1, None
            b.start((str(top),), top_title)
            b.add(line)
            continue
        m = _SOP_SUB.match(t)
        if m and top and int(m.group(1)) == top:
            minor = int(m.group(2))
            cur_minor = int(sub.split(".")[1]) if sub else 0
            if minor == cur_minor + 1:
                sub, sub_title = f"{top}.{minor}", m.group(3).strip()
                process, last_roman, last_letter = None, -1, None
                b.start((str(top), sub), sub_title)
                b.add(line)
                continue
        m = _SOP_PROCESS.match(t)
        if m and top == 11 and sub is None:
            process = f"Process {m.group(1)}"
            process_title = f"{process}: {m.group(2)}".strip()
            last_roman, last_letter = -1, None
            b.start((str(top), process), process_title)
            b.add(line)
            continue
        m = _SOP_ITEM.match(t)
        if m and top:
            tok = m.group(1)
            roman_next = last_roman + 1 < len(ROMANS) and tok == ROMANS[last_roman + 1]
            letter_next = len(tok) == 1 and (
                (last_letter is None and tok == "a")
                or (last_letter is not None and ord(tok) == ord(last_letter) + 1)
            )
            if roman_next and not (letter_next and last_letter is not None):
                last_roman, last_letter = ROMANS.index(tok), None
                parent_title = process_title if process else (sub_title if sub else top_title)
                b.start((*base(), tok), parent_title)
            elif letter_next:
                last_letter = tok
        b.add(line)
    return b.finish()


# --------------------------------------------------------------------------- BNSS

_BNSS_HEAD = re.compile(r"^(\d{1,3})([A-Z]?)\.\s+(.+?)\s*\.\s*[—–]\s*(.*)$")
_BNSS_START = re.compile(r"^(\d{1,3})([A-Z]?)\.\s+[A-Z]")


_SUBHEADING = re.compile(r"^[A-Z]{1,2}\.\s*[—–]")  # "B.—Warrant of arrest" (arrangement sub-heads)
_TITLE_BREAK = re.compile(r"\.\s+[A-Z]")


def _num_suffix_after(num: int, suffix: str, last: tuple[int, str], max_jump: int) -> bool:
    """Numbering must increase and may skip at most ``max_jump`` numbers (the first heading counts from 0)."""
    return (num, suffix) > last and num <= last[0] + max_jump


def _numbered_heading(
    lines: list[Line], i: int, head: re.Pattern[str], start: re.Pattern[str]
) -> tuple[re.Match[str] | None, int]:
    """Match ``head`` on line i, or on lines i and i+1 joined (a heading whose ".—" wraps onto the next line)."""
    m = head.match(lines[i].text)
    if m:
        return m, 1
    if not start.match(lines[i].text) or i + 1 >= len(lines):
        return None, 1
    nxt = lines[i + 1].text
    if start.match(nxt) or _SUBHEADING.match(nxt):
        return None, 1
    m = head.match(f"{lines[i].text} {nxt}")
    if m and len(m.group(3)) < 200 and not _TITLE_BREAK.search(m.group(3)):
        return m, 2
    return None, 1


def detect_bnss(doc: ExtractedDoc, source: Source) -> list[Section]:
    b = _Builder("BNSS Section", "BNSS", source.front_matter)
    lines = list(iter_lines(doc, source.skip_pages))
    last: tuple[int, str] = (0, "")
    i = 0
    while i < len(lines):
        m, consumed = _numbered_heading(lines, i, _BNSS_HEAD, _BNSS_START)
        if m and _num_suffix_after(int(m.group(1)), m.group(2), last, max_jump=15):
            last = (int(m.group(1)), m.group(2))
            key = f"{m.group(1)}{m.group(2)}"
            b.start((key,), m.group(3).strip())
        for k in range(consumed):
            b.add(lines[i + k])
        i += consumed
    return b.finish()


# --------------------------------------------------------------------------- PML Rules

_PMLR_RULE = re.compile(r"^\[*\s*(\d{1,2})([A-Z]?)\.\s+(.+?)\s*\.?\s*[—–]\s*(.*)$")
_PMLR_START = re.compile(r"^\[*\s*(\d{1,2})([A-Z]?)\.\s+[A-Z]")
_PMLR_DEF = re.compile(r"^[\[(]*([a-z]{1,3})[)\]]\s*[\"“]")
_PMLR_SUBRULE = re.compile(r"^\[*\((\d{1,2})\)\s")


def detect_pmlr(doc: ExtractedDoc, source: Source) -> list[Section]:
    b = _Builder("PML Rules, Rule", "PML Rules,", source.front_matter)
    last: tuple[int, str] = (0, "")
    rule = ""
    rule_title = ""
    lines = list(iter_lines(doc, source.skip_pages))
    i = 0
    while i < len(lines):
        t = lines[i].text
        m, consumed = _numbered_heading(lines, i, _PMLR_RULE, _PMLR_START)
        if m and _num_suffix_after(int(m.group(1)), m.group(2), last, max_jump=6):
            last = (int(m.group(1)), m.group(2))
            rule, rule_title = f"{m.group(1)}{m.group(2)}", m.group(3).strip()
            b.start((rule,), rule_title)
        elif rule == "2":
            consumed = 1
            md = _PMLR_DEF.match(t)
            ms = _PMLR_SUBRULE.match(t)
            if md:
                b.start(("2", "1", md.group(1)), rule_title)
            elif ms and ms.group(1) != "1":
                b.start(("2", ms.group(1)), rule_title)
        else:
            consumed = 1
        for k in range(consumed):
            b.add(lines[i + k])
        i += consumed
    return b.finish()


# --------------------------------------------------------------------------- RBI Directions (2026 format)

_RBI_DIR_CHAPTER = re.compile(r"^Chapter\s+[IVX]+\s*[-–—]\s*(\S.*)$")
_RBI_DIR_HEAD = re.compile(r"^[A-H]\.\s+([A-Z].*)$")  # "C. EWS Framework for Other Banking / ..."
_RBI_DIR_PARA = re.compile(r"^(\d{1,3})\.\s+\S")
_RBI_DIR_FOOTNOTE = re.compile(r"^1\s+[A-Z][a-z]")  # "1 Refer to the judgement ..." after the signature


def detect_rbi_dir(doc: ExtractedDoc, source: Source) -> list[Section]:
    """RBI Directions, 2026 format: paragraphs 1..N run straight through Chapters I-X.

    Chapter and lettered headings open the next paragraph (so its chunks carry them) and give its title.
    Nothing before the "Introduction" line is a heading (the table of contents repeats them).
    """
    b = _Builder("RBI FRM Directions Para", "RBI FRM Directions", source.front_matter)
    para = 0
    in_body = False
    frozen = False  # footnotes after the signature
    heading = ""
    pending: list[Line] = []
    for line in iter_lines(doc, source.skip_pages):
        t = line.text
        if not in_body or frozen:
            if not in_body and t.lower() == "introduction":
                in_body = True
                b.start(("Introduction",), "Introduction")
            b.add(line)
            continue
        mc = _RBI_DIR_CHAPTER.match(t)
        mh = _RBI_DIR_HEAD.match(t) if len(t) < 200 else None
        if mc or mh:
            heading = (mc or mh).group(1).strip()
            pending.append(line)
            continue
        mp = _RBI_DIR_PARA.match(t)
        if mp and int(mp.group(1)) == para + 1:
            para += 1
            b.start((str(para),), heading)
        elif para and _RBI_DIR_FOOTNOTE.match(t):
            frozen = True
            b.start(("Footnotes",), "Footnotes")
        for held in pending:
            b.add(held)
        pending.clear()
        b.add(line)
    for held in pending:
        b.add(held)
    return b.finish()


# --------------------------------------------------------------------------- FAQ


def detect_faq(doc: ExtractedDoc, source: Source) -> list[Section]:
    b = _Builder("FIU-IND FAQ:", "FIU-IND FAQ:", source.front_matter)
    for line in iter_lines(doc, source.skip_pages):
        t = line.text
        if t.endswith("?") and len(t) < 200:
            b.start((t,), t)
        b.add(line)
    return b.finish()


DETECTORS: dict[str, Callable[[ExtractedDoc, Source], list[Section]]] = {
    "sop": detect_sop,
    "bnss": detect_bnss,
    "pmlr": detect_pmlr,
    "rbi_dir": detect_rbi_dir,
    "faq": detect_faq,
}


def detect_sections(doc: ExtractedDoc, source: Source) -> list[Section]:
    return DETECTORS[source.profile](doc, source)
