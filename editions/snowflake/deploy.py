"""Deploy the Snowflake edition of Wapsi (no CoCo credit needed; warehouse / Cortex spend is the trial's).

    uv run python -m editions.snowflake.deploy --step all --dry-run          # print the plan + every SQL statement
    uv run python -m editions.snowflake.deploy --step bootstrap              # connection from WAPSI_SF_CONNECTION
    uv run python -m editions.snowflake.deploy --connection VJ22713 --step load,package,procs
    uv run python -m editions.snowflake.deploy --step governance --edition auto
    uv run python -m editions.snowflake.deploy --step cross-region           # optional, ACCOUNTADMIN

Steps (``all`` = bootstrap, load, package, procs, search, semantic, agent, app, governance):
  bootstrap     sql/00_account (ACCOUNTADMIN), 01_database, 10_raw, 20_casemgmt
  cross-region  sql/05_cross_region (optional; not part of ``all`` unless --cross-region)
  load          data/generated/{demo,showcase} -> WAPSI.RAW / WAPSI.RAW_SHOWCASE (+ labels -> WAPSI.ML) with
                write_pandas; golden cases -> WAPSI.ML.GOLDEN_CASES; row-count check (generates missing worlds)
  package       build dist/snowflake/wapsi.zip (+ wapsi_app.zip) and PUT wapsi.zip to @WAPSI.APP.CODE
  procs         sql/30_procs, CALL WAPSI.APP.WAPSI_VERSION(), then the showcase parity check
  parity        TRACE_CASE + BUILD_HOLD_PLAN on both showcase cases vs wapsi.fixtures.showcase; audit chain intact
  search        sql/60_docs, load the docs corpus + citation registry, sql/61_search, SEARCH_PREVIEW smoke
  semantic      sql/70_semantic_view + a SEMANTIC_VIEW() smoke query (over-hold must be 0)
  agent         sql/80_agent (+ DESCRIBE AGENT)
  app           upload the Streamlit files to @WAPSI.APP.SIS_STAGE/wapsi, sql/85_app
  governance    sql/90_governance; Enterprise also sql/91_governance_enterprise (--edition auto detects)
  eval          golden cases -> TRACE_CASE, BUILD_HOLD_PLAN, DRAFT (real Cortex AI_COMPLETE) -> metrics +
                one APP.EVAL_RUNS row (not part of ``all``: it spends AI credits; --eval-world, --limit;
                --no-draft only traces and plans, e.g. to fill the case tables for Cortex Analyst)

Every step is safe to re-run (IF NOT EXISTS / CREATE OR REPLACE for code objects / TRUNCATE-and-reload for RAW;
case history in CASEMGMT is never dropped). The Snowpark session comes from the named connection in
~/.snowflake/connections.toml (or config.toml); this script never reads or prints credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EDITION_DIR = Path(__file__).resolve().parent
SNOWPARK_DIR = EDITION_DIR / "snowpark"
APP_DIR = EDITION_DIR / "app"
for _p in (ROOT / "src", SNOWPARK_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.snowflake import sqlscript  # noqa: E402

CONNECTION_ENV = "WAPSI_SF_CONNECTION"
DEFAULT_CONNECTION = "VJ22713"
DATABASE = "WAPSI"
STEPS = ("bootstrap", "load", "package", "procs", "search", "semantic", "agent", "app", "governance")
EXTRA_STEPS = ("cross-region", "parity", "eval")
WORLDS: dict[str, tuple[str, bool]] = {"demo": ("RAW", True), "showcase": ("RAW_SHOWCASE", False)}
LOAD_ORDER = (
    "meta", "banks", "customers", "accounts", "gst_registry", "external_signals", "device_links",
    "transactions", "invoices", "complaints", "complaint_txns", "labels_accounts", "labels_flows",
    "labels_complaints",
)
APP_FILES = ("streamlit_app.py", "sf_data_source.py", "environment.yml")
CODE_STAGE = "@WAPSI.APP.CODE"
SIS_STAGE = "@WAPSI.APP.SIS_STAGE/wapsi"
CONTEXT_SQL = ("USE ROLE WAPSI_ADMIN", "USE WAREHOUSE WAPSI_WH", "USE DATABASE WAPSI")
EDITION_ERROR_HINTS = ("edition", "not supported", "unsupported feature", "upgrade", "not available")


class DeployError(RuntimeError):
    pass


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class Task:
    kind: str  # "sql" (a file in sql/) | "py" (a Python action)
    name: str  # file name or action name
    describe: str = ""
    optional_file: bool = False  # 91: failures mean "Standard edition" under --edition auto


@dataclass
class Plan:
    connection: str
    edition: str
    steps: list[tuple[str, list[Task]]] = field(default_factory=list)


def step_tasks(step: str, edition: str = "auto", worlds: Sequence[str] = ("demo", "showcase")) -> list[Task]:
    world_list = ", ".join(f"{w} -> {DATABASE}.{WORLDS[w][0]}" for w in worlds)
    if step == "cross-region":
        return [Task("sql", "05_cross_region.sql")]
    if step == "bootstrap":
        return [Task("sql", "00_account.sql"), Task("sql", "01_database.sql"), Task("sql", "10_raw.sql"),
                Task("sql", "20_casemgmt.sql")]
    if step == "load":
        return [Task("py", "load_worlds", f"write_pandas the generated worlds ({world_list}); labels of demo -> "
                                          f"{DATABASE}.ML; print Parquet vs Snowflake row counts"),
                Task("py", "load_golden", f"data/golden/<world>/cases.jsonl -> {DATABASE}.ML.GOLDEN_CASES")]
    if step == "package":
        return [Task("py", "build_zips", "build dist/snowflake/wapsi.zip and wapsi_app.zip (reproducible)"),
                Task("py", "upload_code", f"PUT dist/snowflake/wapsi.zip {CODE_STAGE} (overwrite)")]
    if step == "procs":
        return [Task("sql", "30_procs.sql"),
                Task("py", "smoke_version", "CALL WAPSI.APP.WAPSI_VERSION() -> holdable must equal the fixture"),
                *step_tasks("parity", edition, worlds)]
    if step == "parity":
        return [Task("py", "parity", "CALL TRACE_CASE + BUILD_HOLD_PLAN for 31009260000101 and 31109260000102; "
                                     "compare CASES / HOLD_PLAN with wapsi.fixtures.showcase; lien re-check; "
                                     "audit chain read back intact (CASE_SUMMARY)")]
    if step == "eval":
        return [Task("py", "eval", "golden cases through TRACE_CASE, BUILD_HOLD_PLAN and DRAFT (Cortex "
                                   "AI_COMPLETE); score holds / totals / verifier; insert APP.EVAL_RUNS")]
    if step == "search":
        return [Task("sql", "60_docs.sql"),
                Task("py", "load_docs", "docs index chunks -> DOCS.DOC_CHUNKS; citation registry -> DOCS.CITATIONS"),
                Task("sql", "61_search.sql"),
                Task("py", "smoke_search", "SEARCH_PREVIEW('hold disputed amount in payment aggregator pool')")]
    if step == "semantic":
        return [Task("sql", "70_semantic_view.sql"),
                Task("py", "smoke_semantic", "SEMANTIC_VIEW(held, recoverable, over-hold BY disposition)")]
    if step == "agent":
        return [Task("sql", "80_agent.sql")]
    if step == "app":
        return [Task("py", "build_zips", "build dist/snowflake/wapsi.zip and wapsi_app.zip (reproducible)"),
                Task("py", "upload_app", f"PUT {', '.join(APP_FILES)}, wapsi_app.zip -> {SIS_STAGE}"),
                Task("sql", "85_app.sql")]
    if step == "governance":
        tasks = [Task("sql", "90_governance.sql")]
        if edition in ("auto", "enterprise"):
            tasks.append(Task("sql", "91_governance_enterprise.sql", optional_file=edition == "auto"))
        return tasks
    raise DeployError(f"unknown step {step!r}")


def parse_steps(value: str, cross_region: bool = False) -> list[str]:
    out: list[str] = []
    for part in (p.strip().lower() for p in value.split(",") if p.strip()):
        if part == "all":
            out += (["cross-region"] if cross_region else []) + [s for s in STEPS]
        elif part in STEPS or part in EXTRA_STEPS:
            if part == "bootstrap" and cross_region and "cross-region" not in out:
                out.append("cross-region")
            out.append(part)
        else:
            raise DeployError(f"unknown step {part!r}; choose from {', '.join((*STEPS, *EXTRA_STEPS, 'all'))}")
    return list(dict.fromkeys(out))


def build_plan(steps: Iterable[str], *, connection: str, edition: str = "auto",
               worlds: Sequence[str] = ("demo", "showcase")) -> Plan:
    plan = Plan(connection=connection, edition=edition)
    for s in steps:
        plan.steps.append((s, step_tasks(s, edition, worlds)))
    return plan


def render_plan(plan: Plan, sql_dir: Path = sqlscript.SQL_DIR) -> str:
    """Deterministic text of everything the deploy would do (the --dry-run output)."""
    lines = [
        "Wapsi Snowflake deploy plan (dry run: nothing is sent to Snowflake)",
        f"connection: {plan.connection} (from ~/.snowflake/connections.toml; credentials are never read here)",
        f"edition: {plan.edition}",
        f"steps: {', '.join(s for s, _ in plan.steps)}",
    ]
    for step, tasks in plan.steps:
        lines += ["", f"=== step {step} " + "=" * max(3, 60 - len(step))]
        if step not in ("bootstrap", "cross-region"):
            lines += [f"-- context: {s};" for s in CONTEXT_SQL]
        for t in tasks:
            if t.kind == "py":
                lines.append(f"-- python: {t.name}: {t.describe}")
                continue
            stmts = sqlscript.load(t.name, sql_dir)
            note = " (Enterprise only; a Standard account stops here)" if t.optional_file else ""
            lines.append(f"-- sql file: {t.name}: {len(stmts)} statements{note}")
            for s in stmts:
                lines.append(("-- (optional)\n" if s.optional else "") + s.text + ";")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- data loading (used by tests too)


def read_frame(path: Path):
    """Parquet -> pandas with upper-case columns and naive-UTC timestamps (TIMESTAMP_NTZ holds UTC)."""
    import pandas as pd
    import pyarrow.parquet as pq

    df = pq.read_table(path).to_pandas()
    for col in df.columns:
        if isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_convert("UTC").dt.tz_localize(None)
    df.columns = [str(c).upper() for c in df.columns]
    return df


def world_dir(world: str, data_root: Path = ROOT / "data" / "generated") -> Path:
    return data_root / world


def ensure_world(world: str, data_root: Path = ROOT / "data" / "generated", *, generate: bool = True) -> Path:
    d = world_dir(world, data_root)
    if (d / "transactions.parquet").exists():
        return d
    if not generate:
        raise DeployError(f"{d} has no world; run: uv run python -m wapsi.synth --size {world} --seed 7 --out {d}")
    print(f"generating world {world} -> {d}")
    subprocess.run([sys.executable, "-m", "wapsi.synth", "--size", world, "--seed", "7", "--out", str(d)],
                   check=True, cwd=ROOT)
    return d


def _count(session: Any, fq: str) -> int:
    rows = session.sql(f"SELECT COUNT(*) AS N FROM {fq}").collect()
    return int(_first(rows))


def _first(rows: Sequence[Any]) -> Any:
    row = rows[0]
    if hasattr(row, "as_dict"):
        return list(row.as_dict().values())[0]
    return row[0]


def load_world(session: Any, wdir: Path, schema: str, *, labels: bool, database: str = DATABASE
               ) -> list[tuple[str, str, int, int]]:
    """TRUNCATE + write_pandas every table of ``wdir``. Returns (table, target, parquet rows, snowflake rows)."""
    out = []
    for table in LOAD_ORDER:
        f = wdir / f"{table}.parquet"
        if not f.exists():
            continue
        is_label = table.startswith("labels_")
        if is_label and not labels:
            continue
        target_schema = "ML" if is_label else schema
        df = read_frame(f)
        name = table.upper()
        fq = f"{database}.{target_schema}.{name}"
        session.sql(f"TRUNCATE TABLE IF EXISTS {fq}").collect()
        if len(df):
            session.write_pandas(df, name, database=database, schema=target_schema, quote_identifiers=True,
                                 auto_create_table=False, overwrite=False, use_logical_type=True)
        out.append((table, fq, len(df), _count(session, fq)))
    return out


def golden_rows(world: str, root: Path = ROOT / "data") -> list[dict[str, Any]]:
    for p in (root / "golden" / world / "cases.jsonl", root / "generated" / world / "golden" / "cases.jsonl"):
        if p.exists():
            rows = []
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    obj = json.loads(line)
                    rows.append({"case_id": obj["case_id"], "world": world, "ncrp_ids": obj.get("ncrp_ids", []),
                                 "expected": obj})
            return rows
    return []


def insert_json_rows(session: Any, fq: str, columns: Sequence[tuple[str, str]], rows: Sequence[dict]) -> None:
    """One INSERT ... SELECT FROM FLATTEN(PARSE_JSON(?)) (values are a bind, never SQL text)."""
    if not rows:
        return
    cast = {"VARIANT": "", "ARRAY": "::ARRAY", "OBJECT": "::OBJECT"}
    select = ", ".join(f'f.value:"{c}"{cast.get(t, "::" + t)}' for c, t in columns)
    sql = (f"INSERT INTO {fq} ({', '.join(c for c, _ in columns)}) SELECT {select} "
           "FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f")
    session.sql(sql, params=[json.dumps(list(rows), ensure_ascii=False, default=str)]).collect()


def load_golden(session: Any, worlds: Sequence[str], root: Path = ROOT / "data") -> dict[str, int]:
    out = {}
    cols = [("case_id", "STRING"), ("world", "STRING"), ("ncrp_ids", "ARRAY"), ("expected", "VARIANT")]
    for w in worlds:
        rows = golden_rows(w, root)
        session.sql(f"DELETE FROM {DATABASE}.ML.GOLDEN_CASES WHERE world = ?", params=[w]).collect()
        insert_json_rows(session, f"{DATABASE}.ML.GOLDEN_CASES", cols, rows)
        out[w] = len(rows)
    return out


def docs_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(chunk rows, citation rows) from the local docs index (or its citation-quote fallback) and registry."""
    import warnings

    from wapsi.citations import KNOWN_IDS, registry
    from wapsi.search.index import LocalDocSearch
    from wapsi.search.sources import sources_by_id

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        search = LocalDocSearch.load()
    srcs = sources_by_id()
    chunks = [{
        "chunk_id": c.chunk_id, "chunk_text": c.text,
        "citation_id": c.citation_id if c.citation_id in KNOWN_IDS else None,
        "doc_id": c.doc_id, "doc_title": c.source_title or (srcs[c.doc_id].title if c.doc_id in srcs else ""),
        "section": c.section, "source_url": srcs[c.doc_id].url if c.doc_id in srcs else "", "page": c.page,
    } for c in search.chunks]
    cites = [{"citation_id": c.citation_id, "source_title": c.source_title, "section": c.section,
              "quote": c.quote, "url": c.url, "page": c.page} for c in registry().values()]
    return chunks, cites


DOC_CHUNK_COLUMNS = [("chunk_id", "STRING"), ("chunk_text", "STRING"), ("citation_id", "STRING"),
                     ("doc_id", "STRING"), ("doc_title", "STRING"), ("section", "STRING"),
                     ("source_url", "STRING"), ("page", "NUMBER")]
CITATION_COLUMNS = [("citation_id", "STRING"), ("source_title", "STRING"), ("section", "STRING"),
                    ("quote", "STRING"), ("url", "STRING"), ("page", "NUMBER")]


def load_docs(session: Any) -> dict[str, int]:
    chunks, cites = docs_rows()
    session.sql(f"DELETE FROM {DATABASE}.DOCS.DOC_CHUNKS").collect()
    for i in range(0, len(chunks), 500):
        insert_json_rows(session, f"{DATABASE}.DOCS.DOC_CHUNKS", DOC_CHUNK_COLUMNS, chunks[i : i + 500])
    session.sql(f"DELETE FROM {DATABASE}.DOCS.CITATIONS").collect()
    insert_json_rows(session, f"{DATABASE}.DOCS.CITATIONS", CITATION_COLUMNS, cites)
    return {"DOC_CHUNKS": len(chunks), "CITATIONS": len(cites)}


# --------------------------------------------------------------------------- parity (showcase fixtures)


@dataclass(frozen=True)
class ParityRow:
    case_id: str
    item: str
    expected: float | str
    actual: float | str | None

    @property
    def ok(self) -> bool:
        if isinstance(self.expected, str) or isinstance(self.actual, str) or self.actual is None:
            return self.expected == self.actual
        return abs(float(self.expected) - float(self.actual)) <= 0.005


def expected_showcase() -> list[tuple[str, str, dict[str, Any]]]:
    """(ncrp_id, case_id, expectations) for the two showcase cases, from wapsi.fixtures.showcase."""
    from wapsi.fixtures.showcase import N1, N2, SHOWCASE_1, SHOWCASE_2

    out = []
    for ncrp, fx in ((N1, SHOWCASE_1), (N2, SHOWCASE_2)):
        case_id = f"CASE-{sorted(fx['ncrp_ids'])[0]}"
        hold = dict(fx["hold"])
        fwd = dict(fx.get("forward_hold", {}))
        outbound = dict(fx.get("outbound_request", {}))
        exp = {
            "total_hold": fx.get("total_hold", round(sum(hold.values()), 2)),
            "total_forward_hold": fx.get("total_forward_hold", round(sum(fwd.values()), 2)),
            "total_recoverable": fx.get("total_recoverable", round(sum(hold.values()) + sum(outbound.values()), 2)),
            "over_hold_amount": 0.0,
            "baseline_freeze_amount": fx["baseline_freeze_amount"],
            "baseline_freeze_accounts": fx["baseline_freeze_accounts"],
            "restoration_process": fx["process"].value,
            "hold": hold,
            "forward_hold": fwd,
            "dispositions": {a: d.value for a, d in fx["dispositions"].items()},
        }
        out.append((ncrp, case_id, exp))
    return out


def compare_parity(case_id: str, exp: dict[str, Any], case_row: dict[str, Any],
                   hold_rows: Sequence[dict[str, Any]]) -> list[ParityRow]:
    rows = [ParityRow(case_id, k, exp[k], _num(case_row.get(k)))
            for k in ("total_hold", "total_forward_hold", "total_recoverable", "over_hold_amount",
                      "baseline_freeze_amount", "baseline_freeze_accounts")]
    rows.append(ParityRow(case_id, "restoration_process", exp["restoration_process"],
                          case_row.get("restoration_process")))
    by_acct = {str(r["account_id"]): r for r in hold_rows}
    for acct, amount in sorted(exp["hold"].items()):
        r = by_acct.get(acct, {})
        rows.append(ParityRow(case_id, f"hold {acct}", amount, _num(r.get("hold_amount"))))
        rows.append(ParityRow(case_id, f"disposition {acct}", exp["dispositions"][acct], r.get("disposition")))
    for acct, amount in sorted(exp["forward_hold"].items()):
        rows.append(ParityRow(case_id, f"forward hold {acct}", amount,
                              _num(by_acct.get(acct, {}).get("forward_hold_amount"))))
    return rows


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _lower(row: Any) -> dict[str, Any]:
    d = row.as_dict() if hasattr(row, "as_dict") else dict(row)
    return {str(k).lower(): v for k, v in d.items()}


RECHECK_SQL = """
SELECT
  COUNT_IF(hold_amount > tainted_amount + 0.005 AND disposition <> 'OUTBOUND_CFCFRMS_REQUEST') AS over_tainted,
  COUNT_IF(disposition IN ('HOLD_AMOUNT','HOLD_AMOUNT_NO_ESCALATION','HOLD_AMOUNT_INTERMEDIARY')
           AND hold_amount > balance_now + 0.005) AS over_balance,
  COUNT_IF(forward_hold_amount > 0 AND layer <> 1) AS fwd_not_layer1,
  COUNT_IF(account_type IN ('NODAL','POOL','ESCROW') AND disposition <> 'HOLD_AMOUNT_INTERMEDIARY') AS intermediary_wrong,
  COUNT_IF(disposition = 'OUTBOUND_CFCFRMS_REQUEST' AND hold_amount <> 0) AS outbound_with_hold,
  COUNT_IF(ARRAY_CONTAINS('ESCALATE_S106_CANDIDATE'::VARIANT, reason_codes) AND NOT requires_human) AS escalate_unreviewed
FROM WAPSI.CASEMGMT.HOLD_PLAN WHERE case_id = ?
"""


def run_parity(session: Any, out: Callable[[str], None] = print) -> bool:
    from wapsi.money import fmt_inr

    all_rows: list[ParityRow] = []
    for ncrp, case_id, exp in expected_showcase():
        for proc, arg in (("TRACE_CASE", ncrp), ("BUILD_HOLD_PLAN", case_id)):
            res = _first(session.sql(f"CALL WAPSI.CASEMGMT.{proc}(?)", params=[arg]).collect())
            data = json.loads(res) if isinstance(res, str) else res
            if isinstance(data, dict) and data.get("ok") is False:
                raise DeployError(f"{proc}({arg}) failed: {data.get('error')}")
        case_rows = [_lower(r) for r in session.sql(
            "SELECT * FROM WAPSI.CASEMGMT.CASES WHERE case_id = ?", params=[case_id]).collect()]
        hold_rows = [_lower(r) for r in session.sql(
            "SELECT account_id, disposition, hold_amount, forward_hold_amount FROM WAPSI.CASEMGMT.HOLD_PLAN "
            "WHERE case_id = ?", params=[case_id]).collect()]
        all_rows += compare_parity(case_id, exp, case_rows[0] if case_rows else {}, hold_rows)
        recheck = _lower(session.sql(RECHECK_SQL, params=[case_id]).collect()[0])
        bad = {k: v for k, v in recheck.items() if int(v or 0) != 0}
        all_rows.append(ParityRow(case_id, "lien re-check violations", 0.0, float(sum(int(v) for v in bad.values()))))
        # the case's AUDIT_LOG read back from Snowflake verifies against its AUDIT_ANCHOR checkpoint
        chain = _call(session, "CASE_SUMMARY", case_id).get("audit_chain") or {}
        all_rows.append(ParityRow(case_id, "audit chain", "intact",
                                  chain.get("status") if chain.get("anchored") else "not anchored"))

    def show(v: Any, item: str) -> str:
        if isinstance(v, float | int) and ("hold" in item or "recoverable" in item or "baseline_freeze_amount" in item):
            return fmt_inr(float(v))
        return str(v)

    out(f"{'case':<22}{'item':<40}{'expected':>18}{'snowflake':>18}  status")
    for r in all_rows:
        out(f"{r.case_id:<22}{r.item:<40}{show(r.expected, r.item):>18}{show(r.actual, r.item):>18}  "
            f"{'OK' if r.ok else 'MISMATCH'}")
    ok = all(r.ok for r in all_rows)
    out(f"parity: {sum(r.ok for r in all_rows)}/{len(all_rows)} OK" + ("" if ok else "  <-- MISMATCH"))
    return ok


# --------------------------------------------------------------------------- real-LLM eval (golden set)


def _call(session: Any, proc: str, *args: Any) -> dict[str, Any]:
    marks = ", ".join("?" for _ in args)
    res = _first(session.sql(f"CALL WAPSI.CASEMGMT.{proc}({marks})", params=list(args)).collect())
    data = json.loads(res) if isinstance(res, str) else res
    return data if isinstance(data, dict) else {"ok": False, "error": str(data)}


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(0.95 * (len(s) - 1) + 0.999))], 3)


def run_eval(session: Any, world: str = "showcase", limit: int | None = None, *, insert: bool = True,
             draft: bool = True, out: Callable[[str], None] = print, root: Path = ROOT / "data") -> dict[str, Any]:
    """Golden cases through TRACE_CASE -> BUILD_HOLD_PLAN -> DRAFT (Cortex AI_COMPLETE) inside Snowflake.

    Scores numbers against the golden set (holds, totals, over-hold) and the drafts against the verifier
    (verified first time, needs human, LLM prose accepted vs template fallback), with tokens and latency.
    Writes one APP.EVAL_RUNS row (source 'cortex-ai-complete') unless ``insert`` is False. ``draft=False``
    only traces and plans (fills the case tables for Cortex Analyst without any AI_COMPLETE call, no row).
    """
    import time

    cases = golden_rows(world, root)[: limit or None]
    if not cases:
        raise DeployError(f"no golden cases for world {world!r}")
    seen: set[str] = set()
    per_case = []
    for g in cases:
        exp = g["expected"]
        t0 = time.perf_counter()
        tr = _call(session, "TRACE_CASE", g["ncrp_ids"][0])
        if not tr.get("ok"):
            per_case.append({"case_id": g["case_id"], "error": tr.get("error")})
            continue
        cid = tr["case_id"]
        pl = _call(session, "BUILD_HOLD_PLAN", cid)
        t_plan = time.perf_counter() - t0
        dr = {"drafts": [], "usage": {}} if cid in seen or not draft else _call(session, "DRAFT", cid, "ALL")
        seen.add(cid)
        t_draft = time.perf_counter() - t0 - t_plan
        holds = {d["account_id"]: d["hold_amount"] for d in pl.get("decisions", [])}
        exp_hold = exp.get("expected_hold", {})
        totals_ok = all(abs(float(tr["totals"].get(k, 0)) - float(v)) <= 0.005
                        for k, v in exp.get("expected_totals", {}).items())
        drafts = dr.get("drafts", [])
        per_case.append({
            "case_id": g["case_id"], "adversarial": bool(exp.get("is_adversarial")),
            "hold_exact": all(abs(float(holds.get(a, 0.0)) - float(v)) <= 0.005 for a, v in exp_hold.items()),
            "totals_exact": totals_ok, "over_hold": float(pl.get("over_hold_amount") or 0.0),
            "drafts": len(drafts), "verified": sum(1 for d in drafts if d.get("passed")),
            "fallback": sum(1 for d in drafts if d.get("llm_fallback")),
            "tokens": int((dr.get("usage") or {}).get("total_tokens") or 0),
            "llm_calls": int((dr.get("usage") or {}).get("calls") or 0),
            "plan_s": round(t_plan, 3), "draft_s": round(t_draft, 3),
        })
    ok_cases = [c for c in per_case if "error" not in c]
    n_drafts = sum(c["drafts"] for c in ok_cases)
    metrics = {
        "cases": len(per_case), "errors": len(per_case) - len(ok_cases),
        "hold_exact_match": round(sum(c["hold_exact"] for c in ok_cases) / max(len(ok_cases), 1), 4),
        "totals_exact_match": round(sum(c["totals_exact"] for c in ok_cases) / max(len(ok_cases), 1), 4),
        "over_hold_inr": round(sum(c["over_hold"] for c in ok_cases), 2),
        "drafts": n_drafts,
        "verified_first_time_rate": round(sum(c["verified"] for c in ok_cases) / max(n_drafts, 1), 4),
        "needs_human": n_drafts - sum(c["verified"] for c in ok_cases),
        "llm_prose_fallbacks": sum(c["fallback"] for c in ok_cases),
        "llm_calls": sum(c["llm_calls"] for c in ok_cases),
        "tokens_total": sum(c["tokens"] for c in ok_cases),
        "tokens_per_case": round(sum(c["tokens"] for c in ok_cases) / max(len(ok_cases), 1), 1),
        "p95_time_to_hold_plan_s": _p95([c["plan_s"] for c in ok_cases]),
        "p95_draft_s": _p95([c["draft_s"] for c in ok_cases]),
        "adversarial_cases": sum(c["adversarial"] for c in ok_cases),
    }
    out(f"eval world {world}: {len(per_case)} golden cases through Snowflake (Cortex AI_COMPLETE)")
    for k, v in metrics.items():
        out(f"   {k:<28}{v}")
    report = {"world": world, "source": "cortex-ai-complete", "metrics": metrics, "cases": per_case}
    if insert and draft:
        session.sql("INSERT INTO WAPSI.APP.EVAL_RUNS (world, generated_at, cases, report, source) "
                    "SELECT ?, ?::TIMESTAMP_TZ, ?, PARSE_JSON(?), 'cortex-ai-complete'",
                    params=[world, _now_iso(), len(per_case), json.dumps(report)]).collect()
        out("   inserted WAPSI.APP.EVAL_RUNS (source cortex-ai-complete)")
    return report


def _now_iso() -> str:
    from wapsi.timeutil import utcnow

    return utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------------------- execution


def connect(connection: str):
    try:
        from snowflake.snowpark import Session
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DeployError("snowflake-snowpark-python is missing: uv sync --all-extras") from exc
    try:
        session = Session.builder.config("connection_name", connection).create()
    except Exception as exc:  # noqa: BLE001 - never echo connection details
        raise DeployError(f"could not open connection {connection!r} (check `snow connection test -c "
                          f"{connection}`): {type(exc).__name__}") from exc
    session.sql("ALTER SESSION SET TIMEZONE = 'UTC'").collect()
    session.sql("ALTER SESSION SET QUERY_TAG = 'wapsi-deploy'").collect()
    return session


def _show(rows: Sequence[Any], out: Callable[[str], None], limit: int = 12) -> None:
    for row in rows[:limit]:
        d = _lower(row)
        keys = [k for k in ("name", "kind", "size", "state", "auto_suspend", "resource_monitor", "database_name",
                            "schema_name", "rows", "owner", "created_on") if k in d][:5]
        out("   " + " | ".join(f"{k}={d[k]}" for k in keys) if keys else "   " + str(d)[:200])
    if len(rows) > limit:
        out(f"   ... {len(rows) - limit} more")


def run_sql_file(session: Any, name: str, out: Callable[[str], None] = print, *, edition_probe: bool = False,
                 sql_dir: Path = sqlscript.SQL_DIR) -> str:
    """Run every statement; returns 'ok' or 'standard' (edition probe failed on a feature/edition error)."""
    stmts = sqlscript.load(name, sql_dir)
    out(f"-- {name}: {len(stmts)} statements")
    for i, s in enumerate(stmts, 1):
        first = s.code.splitlines()[0][:110] if s.code else ""
        try:
            rows = session.sql(s.text).collect()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).splitlines()[0][:300]
            if edition_probe and any(h in msg.lower() for h in EDITION_ERROR_HINTS):
                out(f"   [{i}] {first}\n   -> edition error: {msg}")
                return "standard"
            if s.optional:
                out(f"   [{i}] optional statement failed (continuing): {first}\n       {msg}")
                continue
            raise DeployError(f"{name} statement {i} failed: {first}\n  {msg}") from exc
        if s.code.upper().startswith(("SHOW", "DESCRIBE")):
            out(f"   [{i}] {first}")
            _show(rows, out)
    return "ok"


def build_zips(out: Callable[[str], None] = print):
    from build_zip import APP_ZIP_NAME, DIST, ZIP_NAME, build_zip

    reps = [build_zip(DIST / ZIP_NAME), build_zip(DIST / APP_ZIP_NAME, include_ui=True)]
    for r in reps:
        out("   " + r.summary())
    return reps


def put_file(session: Any, local: Path, stage: str) -> None:
    session.file.put(str(local), stage, auto_compress=False, overwrite=True)


def run_python(session: Any, task: Task, ctx: dict[str, Any], out: Callable[[str], None] = print) -> bool:
    name = task.name
    if name == "load_worlds":
        for w in ctx["worlds"]:
            schema, labels = WORLDS[w]
            d = ensure_world(w, ctx["data_root"])
            res = load_world(session, d, schema, labels=labels)
            out(f"   world {w}: {d} -> {DATABASE}.{schema}")
            out(f"   {'table':<20}{'target':<34}{'parquet':>10}{'snowflake':>11}  status")
            bad = 0
            for table, fq, n_pq, n_sf in res:
                bad += n_pq != n_sf
                out(f"   {table:<20}{fq:<34}{n_pq:>10}{n_sf:>11}  {'OK' if n_pq == n_sf else 'MISMATCH'}")
            if bad:
                raise DeployError(f"row count mismatch in {bad} table(s) of world {w}")
        return True
    if name == "load_golden":
        counts = load_golden(session, ctx["worlds"])
        out("   golden cases: " + ", ".join(f"{w} {n}" for w, n in counts.items()))
        return True
    if name == "build_zips":
        ctx["zips"] = build_zips(out)
        return True
    if name == "upload_code":
        from build_zip import DIST, ZIP_NAME

        put_file(session, DIST / ZIP_NAME, CODE_STAGE)
        out(f"   uploaded {ZIP_NAME} -> {CODE_STAGE}")
        return True
    if name == "upload_app":
        from build_zip import APP_ZIP_NAME, DIST

        for f in [*(APP_DIR / n for n in APP_FILES), DIST / APP_ZIP_NAME]:
            put_file(session, f, SIS_STAGE)
            out(f"   uploaded {f.name} -> {SIS_STAGE}")
        return True
    if name == "smoke_version":
        res = _first(session.sql("CALL WAPSI.APP.WAPSI_VERSION()").collect())
        data = json.loads(res) if isinstance(res, str) else res
        out(f"   WAPSI_VERSION: {json.dumps(data, sort_keys=True)}")
        if not data.get("matches_fixture"):
            raise DeployError("WAPSI_VERSION: the core inside Snowflake does not reproduce the showcase fixture")
        return True
    if name == "parity":
        if not run_parity(session, out):
            raise DeployError("parity check failed (see the table above)")
        return True
    if name == "eval":
        report = run_eval(session, ctx.get("eval_world", "showcase"), ctx.get("limit") or None,
                          draft=not ctx.get("no_draft"), out=out)
        m = report["metrics"]
        if m["errors"] or m["over_hold_inr"] > 0.005:
            raise DeployError(f"eval: {m['errors']} case errors, over-hold {m['over_hold_inr']}")
        return True
    if name == "load_docs":
        counts = load_docs(session)
        out("   " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return True
    if name == "smoke_search":
        req = json.dumps({"query": "hold disputed amount in payment aggregator pool account",
                          "columns": ["citation_id", "section"], "limit": 5})
        res = _first(session.sql("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW('WAPSI.DOCS.SOP_SEARCH', ?)",
                                 params=[req]).collect())
        data = json.loads(res) if isinstance(res, str) else res
        ids = [r.get("citation_id") for r in (data or {}).get("results", [])]
        out(f"   SOP_SEARCH citation ids: {ids}")
        return True
    if name == "smoke_semantic":
        rows = session.sql(
            "SELECT * FROM SEMANTIC_VIEW(WAPSI.APP.WAPSI_FRAUD_SV METRICS holds.held_amount, "
            "holds.recoverable_amount, holds.over_hold_amount DIMENSIONS holds.disposition_type)"
        ).collect()
        over = 0.0
        for r in rows:
            d = _lower(r)
            out("   " + " | ".join(f"{k}={v}" for k, v in d.items()))
            over += float(d.get("over_hold_amount") or 0)
        if over > 0.005:
            raise DeployError(f"semantic view reports over-hold {over}; must be 0")
        return True
    raise DeployError(f"unknown python task {name}")


def execute(plan: Plan, session: Any, *, worlds: Sequence[str], data_root: Path,
            out: Callable[[str], None] = print, eval_world: str = "showcase", limit: int = 0,
            no_draft: bool = False) -> int:
    ctx: dict[str, Any] = {"worlds": list(worlds), "data_root": data_root, "eval_world": eval_world,
                           "limit": limit, "no_draft": no_draft}
    ctx_rows = session.sql("SELECT CURRENT_ROLE() AS R, CURRENT_WAREHOUSE() AS W, CURRENT_REGION() AS G").collect()
    info = _lower(ctx_rows[0])
    out(f"connected: role {info.get('r')}, warehouse {info.get('w')}, region {info.get('g')}")
    for step, tasks in plan.steps:
        out(f"\n=== step {step}")
        if step not in ("bootstrap", "cross-region"):
            for s in CONTEXT_SQL:
                session.sql(s).collect()
        for t in tasks:
            if t.kind == "sql":
                result = run_sql_file(session, t.name, out, edition_probe=t.optional_file)
                if result == "standard":
                    out("   Standard edition detected: masking policies unavailable; secure views + RBAC "
                        "(90_governance.sql) are in force.")
                    break
            else:
                out(f"-- python: {t.name}")
                run_python(session, t, ctx, out)
    out("\ndeploy finished OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m editions.snowflake.deploy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connection", default=os.environ.get(CONNECTION_ENV) or DEFAULT_CONNECTION,
                    help=f"connection name (default: ${CONNECTION_ENV} or {DEFAULT_CONNECTION})")
    ap.add_argument("--step", default="all", help="comma-separated steps, or all")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and every SQL statement; no connection")
    ap.add_argument("--edition", choices=("auto", "standard", "enterprise"), default="auto")
    ap.add_argument("--cross-region", action="store_true", help="include sql/05_cross_region.sql (ACCOUNTADMIN)")
    ap.add_argument("--worlds", default="demo,showcase", help="worlds to load (demo, showcase)")
    ap.add_argument("--data-root", default=str(ROOT / "data" / "generated"))
    ap.add_argument("--eval-world", default="showcase", choices=sorted(WORLDS), help="golden set for --step eval")
    ap.add_argument("--limit", type=int, default=0, help="--step eval: first N golden cases (0 = all)")
    ap.add_argument("--no-draft", action="store_true", help="--step eval: trace + plan only (no AI_COMPLETE)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # ₹ and IST text on Windows consoles / pipes
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    try:
        steps = parse_steps(args.step, cross_region=args.cross_region)
        worlds = [w.strip() for w in args.worlds.split(",") if w.strip()]
        unknown = [w for w in worlds if w not in WORLDS]
        if unknown:
            raise DeployError(f"unknown world(s) {unknown}; choose from {sorted(WORLDS)}")
        plan = build_plan(steps, connection=args.connection, edition=args.edition, worlds=worlds)
        if args.dry_run:
            sys.stdout.write(render_plan(plan))
            return 0
        session = connect(args.connection)
        try:
            return execute(plan, session, worlds=worlds, data_root=Path(args.data_root),
                           eval_world=args.eval_world, limit=args.limit, no_draft=args.no_draft)
        finally:
            session.close()
    except DeployError as exc:
        print(f"deploy failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
