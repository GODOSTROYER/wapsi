"""Check the pitch pack against the repository: every pitched number traces to a source, and form limits hold.

    uv run python pitch/check_numbers.py                # numbers, registered tokens, character limits
    uv run python pitch/check_numbers.py --list         # also print every resolved number
    uv run python pitch/check_numbers.py --fix-lengths  # after editing a limited block: restate its length

Four checks, all offline and read-only:

1. **Numbers.** Each row of the table in ``pitch/NUMBERS.md`` (``| ID | Pitched as | Value | Source | Locator |``)
   is resolved from its source and compared with ``Value``, rounded to the decimals ``Value`` is written with.
   Locators: ``json:a.b.c`` (JSON path; ``len:`` for a list or dict length), ``fact:FACT_ID``
   (``data/docs/facts.json``), ``py:<expr>`` (a restricted expression over the source module's names; no ``eval``),
   ``text:<literal>`` (the literal is in the source file and contains ``Value``), ``glob:<pattern>`` (file count),
   ``calc:<expr>`` (arithmetic over other row ids) and ``cmd:<command>`` (command output, checked by hand on the
   date given in the row; not re-run here, because some take minutes).
2. **Registered tokens.** Every rupee amount, percentage, dollar amount and GB figure written anywhere in
   ``pitch/**/*.md``, in ``README.md`` and in ``docs/index.md`` (the pages a judge reads first) must appear in the
   ``Pitched as`` column of ``NUMBERS.md``.
3. **Character limits.** Text between ``<!-- limit:N -->`` and ``<!-- /limit -->`` is at most N characters, and the
   file states its length as ``Length: X / N characters``.
4. **Open slots.** A figure that has not been measured yet is written ``TBD(slot_id)``, and ``slot_id`` is a row of
   the ``| Slot | ...`` table in ``NUMBERS.md``. A bare ``TBD`` fails, so does an unknown slot, and so does a slot
   id that is also a number row (a figure is either measured or open, never both). Slots carry no value.
"""

from __future__ import annotations

import argparse
import ast
import glob
import importlib
import json
import operator
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PITCH = Path(__file__).resolve().parent
ROOT = PITCH.parent
NUMBERS_MD = PITCH / "NUMBERS.md"

_NUM = r"\d+(?:,\d+)*(?:\.\d+)?"  # 1,42,848 / 35.26 / 0.83 (a trailing comma is punctuation, not part of it)
TOKEN_RE = re.compile(
    rf"₹\s?{_NUM}(?:\s?(?:Cr|crore|L|lakh)\b)?"  # rupees, optionally in crore / lakh
    rf"|\${_NUM}"  # dollars
    rf"|(?<![\w.]){_NUM}\s?%"  # percentages
    rf"|(?<![\w.]){_NUM}\s?GB\b"  # memory
)
LIMIT_RE = re.compile(r"<!--\s*limit:(\d+)\s*-->\n?(.*?)\n?<!--\s*/limit\s*-->", re.S)
TBD_RE = re.compile(r"\bTBD\b(?:\(([a-z0-9_]+)\))?")  # TBD(slot_id); group 1 is None for a bare TBD


@dataclass
class Row:
    rid: str
    pitched: str
    value: str
    source: str
    locator: str


def _cell(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1]
    return text.strip()


def parse_rows(path: Path | None = None) -> list[Row]:
    rows: list[Row] = []
    in_table = False
    for line in (path or NUMBERS_MD).read_text(encoding="utf-8").splitlines():
        if line.startswith("| ID |"):
            in_table = True
            continue
        if not in_table or not line.startswith("|"):
            in_table = in_table and line.startswith("|")
            continue
        cells = [c for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or set(cells[0].strip()) <= {"-", " ", ":"}:
            continue
        rid, pitched, value, source, locator = (_cell(c) for c in cells)
        rows.append(Row(rid, pitched, value, source, locator))
    return rows


def _decimals(value: str) -> int:
    return len(value.split(".", 1)[1]) if "." in value else 0


def _json_path(data: Any, path: str) -> Any:
    for key in path.split(".") if path else []:
        data = data[int(key)] if isinstance(data, list) else data[key]
    return data


def _module_for(source: str) -> Any:
    if str(ROOT) not in sys.path:  # sources outside src/ (editions/...) import as packages of the repository root
        sys.path.insert(0, str(ROOT))
    rel = Path(source).with_suffix("")
    parts = rel.parts[1:] if rel.parts and rel.parts[0] == "src" else rel.parts
    return importlib.import_module(".".join(parts))


_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"len": len, "sum": sum, "abs": abs, "round": round,
          "positive": lambda d: sum(1 for v in d.values() if v > 0)}


def safe_eval(expr: str, names: dict[str, Any]) -> Any:
    """Evaluate a small expression without ``eval``: numbers, strings, names from ``names``, + - * /, unary minus,
    subscripts, ``.values()`` and the helpers in ``_FUNCS``. Anything else is refused."""

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float | str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in names:
                return names[node.id]
            raise ValueError(f"unknown name {node.id!r}")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.Subscript):
            return ev(node.value)[ev(node.slice)]
        if isinstance(node, ast.Call) and not node.keywords:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "values" and not node.args:
                return ev(func.value).values()
            if isinstance(func, ast.Name) and func.id in _FUNCS:
                return _FUNCS[func.id](*(ev(a) for a in node.args))
        raise ValueError(f"expression not allowed: {ast.unparse(node)}")

    return ev(ast.parse(expr, mode="eval"))


def resolve(row: Row, known: dict[str, float]) -> float | None:
    """The number the row's locator points at (None for ``cmd:`` rows, which are checked by hand)."""
    kind, _, arg = row.locator.partition(":")
    src = ROOT / row.source
    if kind == "cmd":
        return None
    if kind == "calc":
        return float(safe_eval(arg, known))
    if kind == "fact":
        facts = json.loads(src.read_text(encoding="utf-8"))["facts"]
        return float(next(f["value"] for f in facts if f["fact_id"] == arg))
    if kind in ("json", "len"):
        data = _json_path(json.loads(src.read_text(encoding="utf-8")), arg)
        return float(len(data) if kind == "len" else data)
    if kind == "py":
        return float(safe_eval(arg, vars(_module_for(row.source))))
    if kind == "text":
        if arg not in src.read_text(encoding="utf-8"):
            raise ValueError(f"literal not found in {row.source}: {arg!r}")
        if row.value not in arg.replace(",", ""):
            raise ValueError(f"value {row.value} is not part of the literal {arg!r}")
        return float(row.value)
    if kind == "glob":
        return float(len(glob.glob(str(ROOT / arg))))
    raise ValueError(f"unknown locator kind {kind!r}")


def check_numbers(rows: list[Row] | None = None) -> tuple[list[str], dict[str, float | None]]:
    rows = parse_rows() if rows is None else rows
    errors: list[str] = []
    seen: set[str] = set()
    resolved: dict[str, float | None] = {}
    known: dict[str, float] = {}
    for row in rows:
        if row.rid in seen:
            errors.append(f"{row.rid}: duplicate id")
        seen.add(row.rid)
        if row.source != "derived" and not (ROOT / row.source).exists():
            errors.append(f"{row.rid}: source file missing: {row.source}")
    ordered = [r for r in rows if not r.locator.startswith("calc:")] + [r for r in rows if r.locator.startswith("calc:")]
    for row in ordered:
        try:
            got = resolve(row, known)
        except Exception as exc:  # noqa: BLE001 - report every broken row, not just the first
            errors.append(f"{row.rid}: cannot resolve {row.locator!r} in {row.source}: {exc}")
            resolved[row.rid] = None
            continue
        resolved[row.rid] = got
        if got is None:
            continue
        known[row.rid] = got
        want = float(row.value)
        if round(got, _decimals(row.value)) != want:
            errors.append(f"{row.rid}: NUMBERS.md says {row.value}, {row.source} gives {got!r}")
    return errors, resolved


def _norm(token: str) -> str:
    token = re.sub(r"\s+", "", token).replace("crore", "Cr").replace("lakh", "L")
    return re.sub(r"\.00(?=$|Cr|L)", "", token)


def pitch_files() -> list[Path]:
    return sorted(p for p in PITCH.rglob("*.md") if p != NUMBERS_MD)


# outside pitch/: the pages a judge reads first carry the same figures, so they are held to the same register
FRONT_PAGES = ("README.md", "docs/index.md")


def token_files() -> list[Path]:
    return pitch_files() + [ROOT / f for f in FRONT_PAGES if (ROOT / f).exists()]


def check_tokens(rows: list[Row] | None = None) -> list[str]:
    rows = parse_rows() if rows is None else rows
    registered = {_norm(t) for row in rows for t in TOKEN_RE.findall(row.pitched)}
    errors = []
    for path in token_files():
        text = path.read_text(encoding="utf-8")
        for match in TOKEN_RE.finditer(text):
            if _norm(match.group(0)) not in registered:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r} is not in NUMBERS.md 'Pitched as'")
    return errors


def parse_slots(path: Path | None = None) -> dict[str, str]:
    """Open measurement slots: ``{slot_id: what it will be pitched as}`` from every ``| Slot | ...`` table."""
    slots: dict[str, str] = {}
    in_table = False
    for line in (path or NUMBERS_MD).read_text(encoding="utf-8").splitlines():
        if line.startswith("| Slot |"):
            in_table = True
            continue
        if not in_table or not line.startswith("|"):
            in_table = in_table and line.startswith("|")
            continue
        cells = [_cell(c) for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and not set(cells[0]) <= {"-", " ", ":"}:
            slots[cells[0]] = cells[1]
    return slots


def check_slots(rows: list[Row] | None = None) -> tuple[list[str], dict[str, int]]:
    """Every unmeasured figure in the pitch is ``TBD(slot_id)`` for a slot listed in NUMBERS.md."""
    rows = parse_rows() if rows is None else rows
    slots = parse_slots()
    errors = [f"NUMBERS.md: {sid} is both a number row and an open slot" for sid in slots if sid in {r.rid for r in rows}]
    used: dict[str, int] = dict.fromkeys(slots, 0)
    for path in pitch_files():
        text = path.read_text(encoding="utf-8")
        for match in TBD_RE.finditer(text):
            where = f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
            slot = match.group(1)
            if slot == "slot_id":  # the notation itself, explained in prose
                continue
            if slot is None:
                errors.append(f"{where}: bare TBD; write TBD(slot_id) with a slot from NUMBERS.md")
            elif slot not in slots:
                errors.append(f"{where}: TBD({slot}) is not a slot in NUMBERS.md")
            else:
                used[slot] += 1
    return errors, used


def fix_lengths() -> None:
    """Rewrite each block's ``Length: X / N characters`` line (the first one after the block) with the real count."""
    for path in pitch_files():
        text = path.read_text(encoding="utf-8")
        out, pos = [], 0
        for match in LIMIT_RE.finditer(text):
            limit, n = int(match.group(1)), len(match.group(2).strip())
            stated = re.compile(rf"Length: [\d,]+ / {limit:,} characters")
            found = stated.search(text, match.end())
            if found:
                out.append(text[pos:found.start()] + f"Length: {n:,} / {limit:,} characters")
                pos = found.end()
        new = "".join(out) + text[pos:]
        if new != text:
            path.write_text(new, encoding="utf-8")


def check_limits() -> tuple[list[str], list[tuple[str, int, int]]]:
    errors: list[str] = []
    found: list[tuple[str, int, int]] = []
    for path in pitch_files():
        text = path.read_text(encoding="utf-8")
        for match in LIMIT_RE.finditer(text):
            limit, body = int(match.group(1)), match.group(2).strip()
            n = len(body)
            found.append((str(path.relative_to(ROOT)), n, limit))
            if n > limit:
                errors.append(f"{path.relative_to(ROOT)}: {n} characters, limit {limit}")
            stated = f"Length: {n:,} / {limit:,} characters"
            if stated not in text:
                errors.append(f"{path.relative_to(ROOT)}: state the length as '{stated}'")
    return errors, found


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print every resolved number")
    ap.add_argument("--fix-lengths", action="store_true", help="rewrite the 'Length: X / N characters' lines first")
    args = ap.parse_args(argv)
    if args.fix_lengths:
        fix_lengths()
    rows = parse_rows()
    num_errors, resolved = check_numbers(rows)
    tok_errors = check_tokens(rows)
    lim_errors, limits = check_limits()
    slot_errors, slots = check_slots(rows)
    if args.list:
        for row in rows:
            got = resolved.get(row.rid)
            print(f"{row.rid:28} {row.value:>16}  {'(by hand)' if got is None else 'ok'}  {row.source}")
        for slot, uses in slots.items():
            print(f"{slot:28} {'open slot':>16}  used {uses}x in the pitch")
    manual = sum(1 for r in rows if r.locator.startswith("cmd:"))
    bad = {e.split(":", 1)[0] for e in num_errors}
    auto = [r for r in rows if not r.locator.startswith("cmd:")]
    print(f"numbers: {sum(1 for r in auto if r.rid not in bad)}/{len(auto)} resolved and equal to the source; "
          f"{manual} command outputs checked by hand")
    for path, n, limit in limits:
        print(f"limit: {path}: {n:,} / {limit:,} characters")
    print(f"slots: {len(slots)} open AMD measurement slots (no value, not claims); "
          f"{sum(1 for n in slots.values() if n)} referenced in the pitch")
    errors = num_errors + tok_errors + lim_errors + slot_errors
    for e in errors:
        print("FAIL", e)
    print("PASS" if not errors else f"{len(errors)} problem(s)")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
