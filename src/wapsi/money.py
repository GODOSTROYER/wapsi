"""Money helpers shared by the drafter and the verifier (both must format and parse identically).

Besides formatting, this module owns the *token shapes* both sides must agree on (docs/draft-contract.md,
"Body rules"): money mentions (digits or words, any currency marker or scale word), account numbers,
UTR-like references and dates. All finders work on ``normalize_text`` output, which folds every Unicode digit
(Devanagari, full-width, ...), full-width ASCII, dashes and spaces to ASCII without changing offsets.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from wapsi.timeutil import IST

EPSILON = 0.005  # rupees; two amounts equal if they round to the same paisa


def r2(x: float) -> float:
    """Round to paise. Adds a tiny nudge so 0.125 -> 0.13 consistently."""
    return round(x + (1e-9 if x >= 0 else -1e-9), 2)


def eq(a: float, b: float) -> bool:
    return abs(r2(a) - r2(b)) < EPSILON


def _group_indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_inr(x: float, symbol: bool = True, paise: bool = True) -> str:
    """480000 -> '₹4,80,000.00' (Indian digit grouping)."""
    v = r2(x)
    sign = "-" if v < 0 else ""
    v = abs(v)
    whole = int(v)
    frac = int(round((v - whole) * 100))
    if frac == 100:
        whole, frac = whole + 1, 0
    body = _group_indian(whole) + (f".{frac:02d}" if paise else "")
    return f"{sign}{'₹' if symbol else ''}{body}"


# --------------------------------------------------------------------------- normalisation

_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")
_DASHES = frozenset("‐‑‒–—―−﹘﹣")


def _fold_char(m: re.Match[str]) -> str:
    c = m.group(0)
    d = unicodedata.decimal(c, None)
    if d is not None:
        return str(d)
    o = ord(c)
    if 0xFF01 <= o <= 0xFF5E:  # full-width ASCII
        return chr(o - 0xFEE0)
    if c in _DASHES:
        return "-"
    if c.isspace():
        return " "
    return c


def normalize_text(text: str) -> str:
    """Fold Unicode digits, full-width ASCII, dashes and spaces to ASCII, one character for one character, so
    offsets into the result are offsets into ``text``."""
    return _NON_ASCII_RE.sub(_fold_char, text or "")


# --------------------------------------------------------------------------- number and currency words

UNIT_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
TENS_WORDS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
# Hindi numbers written in Latin script (Hinglish)
HINGLISH_WORDS: dict[str, int] = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "pandrah": 15, "bees": 20,
    "pachees": 25, "tees": 30, "chalis": 40, "chaalis": 40, "pachas": 50, "pachaas": 50, "saath": 60,
    "sattar": 70, "assi": 80, "nabbe": 90,
}  # fmt: skip
# Hinglish number words that are also ordinary English words or surnames: numbers only next to another
# number word, a scale word or a currency word ("do lakh rupaye", but not "do not hold" or "Mr Das")
AMBIGUOUS_NUMBER_WORDS = frozenset({"do", "das", "teen", "char", "saat", "bees", "tees", "saath", "assi"})
SCALE_WORDS: dict[str, float] = {
    "hundred": 1e2, "hundreds": 1e2, "sau": 1e2,
    "thousand": 1e3, "thousands": 1e3, "hazaar": 1e3, "hazar": 1e3, "hajar": 1e3, "hazzar": 1e3, "k": 1e3,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
    "million": 1e6, "millions": 1e6, "mn": 1e6,
    "crore": 1e7, "crores": 1e7, "cr": 1e7, "crs": 1e7, "karod": 1e7, "karor": 1e7,
    "billion": 1e9, "billions": 1e9, "bn": 1e9, "arab": 1e9,
}  # fmt: skip
_SHORT_SCALES = frozenset({"k"})  # only directly after digits: "₹5k"
NUMBER_WORDS: dict[str, int] = {**UNIT_WORDS, **TENS_WORDS, **HINGLISH_WORDS}
_RUPEE_WORDS = frozenset(
    {
        "rupee",
        "rupees",
        "rupaye",
        "rupaiye",
        "rupaya",
        "rupaiya",
        "rupiya",
        "rupiye",
        "rupay",
        "rupye",
        "rupaiyaa",
    }
)
CURRENCY_WORDS = frozenset({*_RUPEE_WORDS, "rs", "inr", "paise", "paisa"})  # "re." (singular) handled apart
_PRE_CURRENCY = frozenset({*_RUPEE_WORDS, "rs", "inr"})
_POST_CURRENCY: dict[str, float] = {**{w: 1.0 for w in _RUPEE_WORDS}, "rs": 1.0, "inr": 1.0, "paise": 0.01,
                                    "paisa": 0.01}  # fmt: skip
# nouns that make a number a count, not money ("three accounts", "1,200 transactions")
COUNT_NOUNS = frozenset(
    {
        "layer", "layers", "account", "accounts", "victim", "victims", "day", "days", "hour", "hours",
        "minute", "minutes", "seconds", "transfer", "transfers", "complaint", "complaints", "transaction",
        "transactions", "txns", "credit", "credits", "debit", "debits", "payment", "payments", "withdrawal",
        "withdrawals", "device", "devices", "bank", "banks", "week", "weeks", "month", "months", "year",
        "years", "time", "times", "report", "reports", "holder", "holders", "mule", "mules", "person",
        "persons", "people", "case", "cases", "instance", "instances", "occasion", "occasions", "step",
        "steps", "notice", "notices", "order", "orders", "invoice", "invoices", "merchant", "merchants",
        "customer", "customers", "entry", "entries", "hop", "hops",
    }
)  # fmt: skip


# --------------------------------------------------------------------------- money mentions


@dataclass(frozen=True)
class MoneyMention:
    """A rupee amount written in the text: ``text`` is the exact span (``text[start:end]`` of the input)."""

    text: str
    value: float
    start: int
    end: int
    in_words: bool = False


_TOKEN_RE = re.compile(
    r"(?P<num>\d(?:[\d,]*\d)?(?:\.\d+)?)"
    r"|(?P<word>[A-Za-z]+)"
    r"|(?P<sym>₹|₨|रुप(?:ये|ए|या|यों)|रु\.?|/-)"
)
_GROUPED_RE = re.compile(r"\d{1,3}(?:,\d{2})*,\d{3}(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_PRE_GAP_RE = re.compile(r"[ \t.:-]{0,4}")
_WORD_GAP_RE = re.compile(r"[ \t]{1,3}|[ \t]?-[ \t]?")
_IDENT_GAP = frozenset({"-", "/", ":", "."})


@dataclass(frozen=True)
class _Tok:
    kind: str  # num | word | sym
    text: str
    low: str
    start: int
    end: int


def _tokens(norm: str) -> list[_Tok]:
    return [
        _Tok(m.lastgroup or "", m.group(0), m.group(0).lower(), m.start(), m.end())
        for m in _TOKEN_RE.finditer(norm)
    ]


def _num_class(t: _Tok) -> str | None:
    """num | unit | tens | scale | None."""
    if t.kind == "num":
        return "num"
    if t.kind != "word":
        return None
    if t.low in TENS_WORDS:
        return "tens"
    if t.low in UNIT_WORDS or t.low in HINGLISH_WORDS:
        return "unit"
    if t.low in SCALE_WORDS:
        return "scale"
    return None


def _is_currency_before(toks: list[_Tok], i: int, norm: str) -> bool:
    """Is token ``i`` a currency marker that may directly precede an amount?"""
    t = toks[i]
    if t.kind == "sym":
        return t.text != "/-"
    if t.kind != "word":
        return False
    if t.low in _PRE_CURRENCY:
        return True
    return t.low == "re" and norm[t.end : t.end + 1] == "."  # "Re. 1"


def _phrase(toks: list[_Tok], i: int, norm: str, currency_before: bool):
    """The number phrase starting at token ``i``: (last index, value, in_words, big, grouped, ambiguous) or None.

    ``big``: scaled by a thousand or more; ``grouped``: a single comma-grouped digit number; ``ambiguous``: a
    single word from ``AMBIGUOUS_NUMBER_WORDS``.
    """
    t = toks[i]
    cls = _num_class(t)
    if cls is None and t.kind == "word" and t.low in ("a", "an") and i + 1 < len(toks):
        nxt = toks[i + 1]
        if _num_class(nxt) == "scale" and nxt.low not in _SHORT_SCALES and norm[t.end : nxt.start] == " ":
            cls = "a"
    if cls is None or cls == "scale":
        return None
    if cls == "num" and not currency_before:
        # part of an identifier, a date or a time ("HB01", "TX-001", "10-09-2026", "11:02")?
        if i > 0:
            prev = toks[i - 1]
            gap = norm[prev.end : t.start]
            if prev.kind in ("word", "num") and (gap == "" or gap in _IDENT_GAP):
                return None
        if i + 1 < len(toks):
            nxt = toks[i + 1]
            gap = norm[t.end : nxt.start]
            if nxt.kind in ("word", "num") and (gap == "" or gap in _IDENT_GAP):
                if not (nxt.kind == "word" and (nxt.low in SCALE_WORDS or nxt.low in _POST_CURRENCY)):
                    return None
    if cls == "a":
        values: list[tuple[str, float]] = [("unit", 1.0)]
    elif cls == "num":
        values = [("num", float(t.text.replace(",", "")))]
    else:
        values = [(cls, float(NUMBER_WORDS[t.low]))]
    last, k, after_and = i, i + 1, False
    while k < len(toks):
        nxt = toks[k]
        gap = norm[toks[k - 1].end : nxt.start]
        if nxt.kind == "word" and nxt.low == "and":
            if after_and or values[-1][0] != "scale" or not _WORD_GAP_RE.fullmatch(gap):
                break
            after_and, k = True, k + 1
            continue
        ncls = _num_class(nxt)
        prev_cls = values[-1][0]
        if ncls is None:
            break
        if ncls == "scale" and nxt.low in _SHORT_SCALES:
            ok = prev_cls == "num" and gap == ""
        elif gap == "":
            ok = prev_cls == "num" and ncls == "scale"
        elif not _WORD_GAP_RE.fullmatch(gap):
            ok = False
        elif ncls == "scale":
            ok = not after_and
        elif prev_cls == "tens":
            ok = ncls == "unit" and 0 < UNIT_WORDS.get(nxt.low, 0) < 10
        else:  # a digit number or a unit takes only a scale; a scale (or "and") takes any number
            ok = prev_cls == "scale" and ncls in ("unit", "tens", "num")
        if not ok:
            break
        if (
            ncls == "scale"
            and k + 1 < len(toks)
            and toks[k + 1].start == nxt.end
            and toks[k + 1].kind != "sym"
        ):
            break  # "01CR5": the scale word is part of an identifier
        if ncls == "scale":
            value = SCALE_WORDS[nxt.low]
        elif ncls == "num":
            value = float(nxt.text.replace(",", ""))
        else:
            value = float(NUMBER_WORDS[nxt.low])
        values.append((ncls, value))
        last, k, after_and = k, k + 1, False
    total, current, last_big, big = 0.0, 0.0, 0.0, False
    for vcls, v in values:
        if vcls != "scale":
            current += v
        elif v < 1000:
            current = (current or 1.0) * v
        else:
            big = True
            if current == 0 and total > 0 and v > last_big:
                total *= v
            else:
                total += (current or 1.0) * v
            last_big, current = max(last_big, v), 0.0
    in_words = any(vcls != "num" for vcls, _ in values)
    grouped = len(values) == 1 and cls == "num" and bool(_GROUPED_RE.fullmatch(t.text))
    ambiguous = len(values) == 1 and cls == "unit" and t.low in AMBIGUOUS_NUMBER_WORDS
    return last, total + current, in_words, big, grouped, ambiguous


def _count_noun_follows(toks: list[_Tok], j: int, norm: str) -> bool:
    for k in range(j + 1, min(j + 3, len(toks))):
        t = toks[k]
        if t.kind != "word" or not _WORD_GAP_RE.fullmatch(norm[toks[k - 1].end : t.start]):
            return False
        if t.low in COUNT_NOUNS:
            return True
        if t.low in CURRENCY_WORDS or _num_class(t) is not None:
            return False
    return False


def find_money_mentions(text: str) -> list[MoneyMention]:
    """Every rupee amount in ``text``, however it is written.

    Grammar (on ``normalize_text``): an *amount phrase* is a digit number (``4,80,000.00``, ``480,000``,
    ``480000``) or number words (English ``nine lakh fifty thousand``, Hinglish ``do lakh``), optionally mixed
    (``9 lakh 50 thousand``) and scaled by ``hundred / thousand / k / lakh / lac / crore / cr / million / billion
    / arab``. A phrase is money when it is next to a currency marker (before: ``₹ ₨ रु Rs Rs. INR Re. rupee(s)
    rupaye``; after: ``rupee(s) rupaye Rs INR /- paise``, optionally followed by ``only``), or when it has a scale
    of a thousand or more, or is a comma-grouped number, and is not a count ("1,200 transactions").
    """
    norm = normalize_text(text)
    toks = _tokens(norm)
    out: list[MoneyMention] = []
    i = 0
    while i < len(toks):
        currency_before = (
            i > 0
            and _is_currency_before(toks, i - 1, norm)
            and bool(_PRE_GAP_RE.fullmatch(norm[toks[i - 1].end : toks[i].start]))
        )
        ph = _phrase(toks, i, norm, currency_before)
        if ph is None:
            i += 1
            continue
        j, value, in_words, big, grouped, ambiguous = ph
        start = toks[i - 1].start if currency_before else toks[i].start
        end, k, factor, currency_after = toks[j].end, j + 1, 1.0, False
        if k < len(toks):
            t = toks[k]
            gap = norm[end : t.start]
            if t.kind == "sym" and t.text == "/-" and gap.strip() == "":
                currency_after, end, k = True, t.end, k + 1
            elif t.kind == "word" and t.low in _POST_CURRENCY and (gap == "" or _WORD_GAP_RE.fullmatch(gap)):
                currency_after, end, k, factor = True, t.end, k + 1, _POST_CURRENCY[t.low]
                if k < len(toks) and toks[k].kind == "sym" and toks[k].text == "/-" and toks[k].start == end:
                    end, k = toks[k].end, k + 1
        currency = currency_before or currency_after
        if currency and k < len(toks):
            t = toks[k]
            if t.kind == "word" and t.low == "only" and _WORD_GAP_RE.fullmatch(norm[end : t.start]):
                end, k = t.end, k + 1
        if currency or (not ambiguous and (big or grouped) and not _count_noun_follows(toks, j, norm)):
            out.append(MoneyMention(text[start:end], r2(value * factor), start, end, in_words))
            i = k
        else:
            i = j + 1
    return out


def find_amounts(text: str) -> list[tuple[str, float]]:
    """Every rupee amount in ``text`` (see ``find_money_mentions``): [(matched_text, value)]."""
    return [(m.text, m.value) for m in find_money_mentions(text)]


def parse_inr(s: str) -> float:
    found = find_money_mentions(s)
    if found:
        return found[0].value
    return float(normalize_text(s).replace(",", "").strip())


def is_single_amount(value: str) -> MoneyMention | None:
    """The mention if ``value`` is exactly one amount (e.g. a rendered slot value), else None."""
    found = find_money_mentions(value)
    if len(found) == 1 and found[0].text == value.strip():
        return found[0]
    return None


# --------------------------------------------------------------------------- identifiers and dates

# account numbers: HB01-1000000001, any case, "-" / " " / no separator
_ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{2})(?:[- ]?(\d{10})|-(\d{8,12}))(?![0-9])")
# UTR-like references: 12-digit RRN (also written in groups: "4253 1000 0001"), and alphanumeric references of
# 12-22 characters with at least 9 digits and at most 8 letters (NEFT / RTGS UTRs such as HB01N26026123456,
# N260261234567890, ATM000000004)
_RRN_RE = re.compile(r"(?<![A-Za-z0-9])\d{12}(?![A-Za-z0-9])")
_GROUPED_RRN_RE = re.compile(r"(?<![\w.,/:-])\d{2,6}(?:[ -]\d{2,6}){1,5}(?![\w/:-]|[.,]\d)")
_ALNUM_REF_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{12,22}(?![A-Za-z0-9])")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}  # fmt: skip
_MON = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_ORD = r"(?:st|nd|rd|th)?"
_DATE_RES = (
    # 10-09-2026, 10/09/2026, 10.09.2026 (day first)
    re.compile(
        r"(?<![\w.,/-])(?P<d>\d{1,2})(?P<sep>[-/.])(?P<m>\d{1,2})(?P=sep)(?P<y>\d{4})(?![\w]|[-/.]\d)"
    ),
    # 2026-09-10, 2026-09-10T11:02:00+05:30
    re.compile(
        r"(?<![\w.,/-])(?P<y>\d{4})(?P<sep>[-/.])(?P<m>\d{1,2})(?P=sep)(?P<d>\d{1,2})"
        r"(?:[T ](?P<time>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?P<tz>Z|[+-]\d{2}:?\d{2})?)?(?![\w]|[-/.:]\d)"
    ),
    # 10 Sep 2026, 10th of September, 2026, 10-Sep-2026
    re.compile(rf"(?<![\w.])(?P<d>\d{{1,2}}){_ORD}(?:\s+of)?[\s-]+{_MON}[\s,-]+(?P<y>\d{{4}})(?!\w)", re.I),
    # September 10, 2026 / Sep 10th 2026
    re.compile(rf"(?<![\w]){_MON}[\s-]+(?P<d>\d{{1,2}}){_ORD},?[\s-]+(?P<y>\d{{4}})(?!\w)", re.I),
)


def find_account_ids(text: str) -> list[tuple[str, str]]:
    """Account-number-shaped tokens: [(span, canonical 'HB01-1000000001')]."""
    norm = normalize_text(text)
    return [
        (text[m.start() : m.end()], f"{m.group(1).upper()}-{m.group(2) or m.group(3)}")
        for m in _ACCOUNT_RE.finditer(norm)
    ]


def find_utrs(text: str) -> list[tuple[str, str]]:
    """UTR-like references: [(span, canonical upper-case form without separators)]."""
    norm = normalize_text(text)
    out: list[tuple[int, int, str]] = []
    for m in _RRN_RE.finditer(norm):
        out.append((m.start(), m.end(), m.group(0)))
    for m in _GROUPED_RRN_RE.finditer(norm):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 12:
            out.append((m.start(), m.end(), digits))
    for m in _ALNUM_REF_RE.finditer(norm):
        tok = m.group(0)
        n_digits = sum(ch.isdigit() for ch in tok)
        n_letters = len(tok) - n_digits
        if 1 <= n_letters <= 8 and n_digits >= 9 and not _ACCOUNT_RE.fullmatch(tok):
            out.append((m.start(), m.end(), tok.upper()))
    out.sort()
    result, last_end = [], -1
    for s, e, canon in out:
        if s < last_end:
            continue
        result.append((text[s:e], canon))
        last_end = e
    return result


def _valid_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[str, str | None]]:
    """Date-shaped tokens: [(span, 'DD-MM-YYYY' or None when the date does not exist)].

    Timestamps with a time zone are converted to IST (the zone drafts display).
    """
    norm = normalize_text(text)
    found: list[tuple[int, int, str | None]] = []
    for rx in _DATE_RES:
        for m in rx.finditer(norm):
            gd = m.groupdict()
            month = _MONTHS[gd["mon"][:3].lower()] if gd.get("mon") else int(gd["m"])
            day = _valid_date(int(gd["y"]), month, int(gd["d"]))
            canon = None
            if day is not None:
                if gd.get("time") and gd.get("tz"):
                    try:
                        ts = datetime.fromisoformat(m.group(0).replace(" ", "T").replace("Z", "+00:00"))
                        day = ts.astimezone(IST).date()
                    except ValueError:
                        day = None
                canon = day.strftime("%d-%m-%Y") if day else None
            found.append((m.start(), m.end(), canon))
    found.sort(key=lambda x: (x[0], -x[1]))
    out, last_end = [], -1
    for s, e, canon in found:
        if s < last_end:
            continue
        out.append((text[s:e], canon))
        last_end = e
    return out
