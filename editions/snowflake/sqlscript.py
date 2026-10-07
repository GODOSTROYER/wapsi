"""Read and split the numbered SQL files in ``editions/snowflake/sql``.

``split_statements`` understands single-quoted strings (``''`` escapes), double-quoted identifiers,
``$$ ... $$`` bodies (procedures, agent specifications), ``--`` line comments and ``/* */`` block comments,
so a ``;`` inside any of them never ends a statement. A statement preceded by a ``-- @optional`` comment line is
marked optional: deploy.py reports its failure and continues (used for grants whose syntax or availability
depends on the account).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"
OPTIONAL_MARK = "@optional"


@dataclass(frozen=True)
class Statement:
    text: str  # without the trailing ';', comments inside kept
    optional: bool = False
    source: str = ""  # file name

    @property
    def code(self) -> str:
        """The statement without comments (what the guard hook and the object inventory look at)."""
        return strip_comments(self.text).strip()


def strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments outside strings and $$ bodies."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("$$", i):
            j = sql.find("$$", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j])
            i = j
        elif ch in ("'", '"'):
            j = _end_quote(sql, i)
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _end_quote(sql: str, i: int) -> int:
    q = sql[i]
    j = i + 1
    n = len(sql)
    while j < n:
        if sql[j] == "\\" and q == "'":
            j += 2
            continue
        if sql[j] == q:
            if j + 1 < n and sql[j + 1] == q:  # doubled quote escape
                j += 2
                continue
            return j + 1
        j += 1
    return n


def split_statements(sql: str, source: str = "") -> list[Statement]:
    stmts: list[Statement] = []
    buf: list[str] = []
    optional = False
    i, n = 0, len(sql)

    def flush() -> None:
        nonlocal optional
        text = "".join(buf).strip()
        buf.clear()
        if strip_comments(text).strip():
            stmts.append(Statement(_trim_leading_comments(text), optional, source))
        optional = False

    while i < n:
        ch = sql[i]
        if sql.startswith("$$", i):
            j = sql.find("$$", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
        elif ch in ("'", '"'):
            j = _end_quote(sql, i)
            buf.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            comment = sql[i:j]
            if comment.strip("- ").strip().lower() == OPTIONAL_MARK and not strip_comments("".join(buf)).strip():
                optional = True
            buf.append(comment)
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(sql[i:j])
            i = j
        elif ch == ";":
            flush()
            i += 1
        else:
            buf.append(ch)
            i += 1
    flush()
    return stmts


def _trim_leading_comments(text: str) -> str:
    lines = text.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).strip()


def sql_files(sql_dir: Path = SQL_DIR) -> list[Path]:
    return sorted(sql_dir.glob("[0-9][0-9]_*.sql"))


def load(name: str, sql_dir: Path = SQL_DIR) -> list[Statement]:
    path = sql_dir / name
    return split_statements(path.read_text(encoding="utf-8"), source=path.name)


_OBJECT_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:SECURE\s+)?"
    r"(ROLE|WAREHOUSE|RESOURCE\s+MONITOR|DATABASE|SCHEMA|STAGE|TABLE|VIEW|PROCEDURE|CORTEX\s+SEARCH\s+SERVICE|"
    r"SEMANTIC\s+VIEW|AGENT|STREAMLIT|MASKING\s+POLICY)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Z0-9_$.]+)",
    re.I,
)


def created_objects(statements: list[Statement]) -> list[tuple[str, str]]:
    """(kind, name) for every CREATE statement, e.g. ('PROCEDURE', 'WAPSI.CASEMGMT.TRACE_CASE')."""
    out = []
    for s in statements:
        m = _OBJECT_RE.search(s.code)
        if m:
            out.append((" ".join(m.group(1).upper().split()), m.group(2).upper()))
    return out
