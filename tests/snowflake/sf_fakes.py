"""Offline stand-in for a Snowpark ``Session`` (no network, no credentials).

``SqliteSession`` executes the Snowflake SQL that Wapsi's Snowflake layer sends on SQLite after a small, explicit
dialect translation (``WAPSI.<schema>.<table>`` -> attached databases, ``FLATTEN(PARSE_JSON(?))`` ->
``json_each``, ``f.value:"col"::TYPE`` -> ``json_extract``, ``TO_TIMESTAMP_NTZ(?)`` -> the bound UTC string,
Snowflake functions as SQLite UDFs). Tables are created from the edition's own ``sql/*.sql`` DDL, so a column the
Python code writes but the DDL lacks fails here exactly as it would in Snowflake. Cortex calls (AI_COMPLETE,
SEARCH_PREVIEW) and ``CALL`` statements go to registered Python handlers. Result rows mimic Snowpark: upper-case
keys, ``Decimal`` money, naive ``datetime`` for TIMESTAMP_NTZ, VARIANT / ARRAY as JSON text.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from editions.snowflake import sqlscript

SCHEMAS = ("RAW", "RAW_SHOWCASE", "ML", "CORE", "CASEMGMT", "DOCS", "APP")
TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
TS_COLS = {"ts", "opened_at", "reported_at", "ordered_at", "delivered_at", "first_seen", "first_tainted_at",
           "created_at", "updated_at", "verified_at", "decided_at", "event_at", "start_ts", "due", "warn_at",
           "placed_at", "released_at", "executed_at", "claimed_at", "generated_at", "now_ts", "first_reached_at",
           "anchored_at", "checked_at"}
DATE_COLS = {"dob", "registered_at"}
BOOL_COLS = {"is_home", "is_adversarial", "is_home_bank", "via_consortium", "requires_human", "passed",
             "business_days", "sup", "app"}
MONEY_COLS = {"amount", "opening_balance", "amount_claimed", "balance", "hold_amount", "forward_hold_amount",
              "tainted_amount", "balance_now", "tainted_total", "total_hold", "total_forward_hold",
              "total_recoverable", "over_hold_amount", "baseline_freeze_amount", "stolen", "holdable",
              "cashed_out", "outbound", "intermediary", "merchant_settled", "unlocated", "priority_amount",
              "tainted_now_total", "tainted_received_total", "tainted_sent_total", "tainted_amount_truth"}
ROLE_PARENTS = {"WAPSI_SUPERVISOR": {"WAPSI_ANALYST"},
                "WAPSI_ADMIN": {"WAPSI_SUPERVISOR", "WAPSI_ANALYST", "WAPSI_APP_OWNER", "WAPSI_AUDITOR",
                                "WAPSI_LEA_READONLY", "WAPSI_ACTIONS_RUNNER", "WAPSI_ML"}}

Handler = Callable[["SqliteSession", str, list[Any]], list[dict[str, Any]]]


class FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def as_dict(self) -> dict[str, Any]:
        return dict(self._d)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return list(self._d.values())[key]
        return self._d[key.upper()]

    def __repr__(self) -> str:
        return f"Row({self._d})"


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def collect(self) -> list[FakeRow]:
        return [FakeRow(r) for r in self._rows]

    def to_pandas(self):
        import pandas as pd

        return pd.DataFrame(self._rows)


class FakeFileOps:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str]] = []

    def put(self, local: str, stage: str, **kwargs: Any) -> list[Any]:
        self.puts.append((Path(local).name, stage))
        return []


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v: Any) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


def _to_ts(v: Any) -> Any:
    if isinstance(v, str):
        s = v.replace("T", " ").replace("Z", "")
        for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:26], fmt)
            except ValueError:
                continue
    return v


def _convert(col: str, v: Any) -> Any:
    if v is None:
        return None
    c = col.lower()
    if c in TS_COLS:
        return _to_ts(v)
    if c in DATE_COLS and isinstance(v, str):
        return date.fromisoformat(v[:10])
    if c in BOOL_COLS:
        return bool(v)
    if c in MONEY_COLS and isinstance(v, int | float):
        return Decimal(str(round(float(v), 2)))
    return v


def bind_value(v: Any) -> Any:
    if isinstance(v, datetime):
        v = v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo else v
        return v.strftime(TS_FORMAT)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return int(v)
    if hasattr(v, "item"):  # numpy scalar
        return v.item()
    return v


_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bTRUNCATE\s+TABLE\s+(?:IF\s+EXISTS\s+)?", re.I), "DELETE FROM "),
    (re.compile(r"\bWAPSI\.(?=[A-Z_]+\.)", re.I), ""),
    (re.compile(r"TABLE\(FLATTEN\(INPUT\s*=>\s*PARSE_JSON\(\?\)\)\)\s+f", re.I), "json_each(?) f"),
    (re.compile(r'f\.value:"(\w+)"(?:::[A-Z_]+(?:\(\d+\s*,\s*\d+\))?)?', re.I), r"json_extract(f.value, '$.\1')"),
    (re.compile(r"PARSE_JSON\(\?\)", re.I), "?"),
    (re.compile(r"TO_TIMESTAMP_NTZ\(\?\)", re.I), "?"),
    (re.compile(r"::[A-Z_]+(?:\(\d+\s*,\s*\d+\))?", re.I), ""),
]


def translate(sql: str) -> str:
    out = sql
    for pat, rep in _RULES:
        out = pat.sub(rep, out)
    return out


class SqliteSession:
    """Snowpark-shaped session over SQLite. ``roles``: roles in session (inheritance per ROLE_PARENTS)."""

    def __init__(self, *, user: str = "TESTER", roles: Sequence[str] = ("WAPSI_ADMIN",)) -> None:
        self.conn = sqlite3.connect(":memory:")
        for s in SCHEMAS:
            self.conn.execute(f"ATTACH DATABASE ':memory:' AS {s}")
        self.user = user
        self.set_roles(roles)
        c = self.conn
        c.create_function("CURRENT_USER", 0, lambda: self.user)
        c.create_function("CURRENT_ROLE", 0, lambda: self.primary_role)
        c.create_function("IS_ROLE_IN_SESSION", 1, lambda r: int(str(r).upper() in self.roles))
        c.create_function("IFF", 3, lambda cond, a, b: a if cond else b)
        c.create_function("CONTAINS", 2, lambda a, b: int(a is not None and b is not None and b in a))
        c.create_function("SYSDATE", 0, lambda: datetime.now(UTC).replace(tzinfo=None).strftime(TS_FORMAT))
        c.create_function("ARRAY_CONTAINS", 2,
                          lambda v, arr: int(arr is not None and v in (json.loads(arr) if isinstance(arr, str)
                                                                       else arr)))
        c.create_aggregate("COUNT_IF", 1, _CountIf)
        self.handlers: list[tuple[re.Pattern[str], Handler]] = []
        self.log: list[tuple[str, list[Any]]] = []
        self.file = FakeFileOps()
        self.closed = False

    # ------------------------------------------------------------------ setup
    def set_roles(self, roles: Sequence[str]) -> None:
        expanded: set[str] = set()
        todo = [r.upper() for r in roles]
        while todo:
            r = todo.pop()
            if r not in expanded:
                expanded.add(r)
                todo += list(ROLE_PARENTS.get(r, ()))
        self.roles = expanded
        self.primary_role = roles[0].upper() if roles else ""

    def on(self, pattern: str, handler: Handler) -> None:
        """Route SQL matching ``pattern`` (regex, case-insensitive) to ``handler(session, sql, params)``."""
        self.handlers.insert(0, (re.compile(pattern, re.I | re.S), handler))

    def apply_ddl(self, *files: str) -> None:
        """CREATE TABLE statements of the given sql/ files (other statements are Snowflake-only)."""
        for name in files:
            for st in sqlscript.load(name):
                code = st.code
                m = re.match(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\S+)\s+LIKE\s+(\S+)$", code, re.I | re.S)
                if m:
                    self.conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {translate(m.group(1))} AS "
                        f"SELECT * FROM {translate(m.group(2))} WHERE 0"
                    )
                    continue
                if re.match(r"CREATE\s+TABLE\b", code, re.I):
                    ddl = re.sub(r"\)\s*COMMENT\s*=\s*'(?:[^']|'')*'\s*$", ")", code, flags=re.S)
                    self.conn.execute(translate(ddl))

    # ------------------------------------------------------------------ Snowpark surface
    def sql(self, query: str, params: Sequence[Any] | None = None) -> FakeResult:
        binds = [bind_value(p) for p in (params or [])]
        self.log.append((query, binds))
        for pat, handler in self.handlers:
            if pat.search(query):
                return FakeResult(handler(self, query, binds))
        stripped = query.strip().upper()
        if stripped.startswith(("USE ", "ALTER SESSION", "SET ", "SHOW ", "DESCRIBE ", "GRANT ")):
            return FakeResult([])
        cur = self.conn.execute(translate(query), binds)
        if cur.description is None:
            self.conn.commit()
            return FakeResult([])
        cols = [d[0] for d in cur.description]
        rows = [{c.upper(): _convert(c, v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]
        return FakeResult(rows)

    def write_pandas(self, df: Any, table_name: str, *, database: str = "WAPSI", schema: str = "RAW",
                     **kwargs: Any) -> None:
        cols = [str(c).upper() for c in df.columns]
        marks = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {schema}.{table_name} ({', '.join(cols)}) VALUES ({marks})"
        rows = []
        for rec in df.itertuples(index=False, name=None):
            rows.append([None if _is_null(v) else bind_value(v.to_pydatetime() if hasattr(v, "to_pydatetime")
                                                            else v) for v in rec])
        self.conn.executemany(sql, rows)
        self.conn.commit()

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ helpers for tests
    def rows(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return [{k.lower(): v for k, v in r.as_dict().items()} for r in self.sql(sql, params).collect()]


def _is_null(v: Any) -> bool:
    if v is None:
        return True
    try:
        import pandas as pd

        return bool(pd.isna(v)) if not isinstance(v, list | dict | tuple) else False
    except (TypeError, ValueError):
        return False
