"""The edition's SQL: parsing, deterministic dry-run, object coverage, guard-hook compatibility, cost limits and
consistency with the Python layer and the templates."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from editions.snowflake import deploy, sqlscript

ROOT = Path(__file__).resolve().parents[2]
EDITION = ROOT / "editions" / "snowflake"
GUARD = EDITION / "hooks" / "pre_sql_guard.py"

ALL_STATEMENTS = [s for f in sqlscript.sql_files() for s in sqlscript.load(f.name)]
DEPLOYED_FILES = [f.name for f in sqlscript.sql_files() if not f.name.startswith("99_")]


def _guard():
    spec = importlib.util.spec_from_file_location("pre_sql_guard", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _created() -> set[str]:
    return {name for _, name in sqlscript.created_objects(ALL_STATEMENTS)}


# --------------------------------------------------------------------------- splitting


def test_splitter_respects_strings_comments_and_dollar_bodies():
    sql = """
    -- leading comment; with a semicolon
    CREATE ROLE IF NOT EXISTS R COMMENT = 'owner''s; rights';
    /* block; comment */ SELECT 1;
    CREATE PROCEDURE P() RETURNS VARIANT LANGUAGE PYTHON HANDLER = 'run' AS
    $$
    x = "a;b"; y = 'c;d'
    $$;
    -- @optional
    GRANT USAGE ON WAREHOUSE W TO ROLE R;
    SELECT 'SYSTEM$WAREHOUSE_RUNTIME'
    """
    stmts = sqlscript.split_statements(sql)
    assert [s.code.split()[0] for s in stmts] == ["CREATE", "SELECT", "CREATE", "GRANT", "SELECT"]
    assert "owner''s; rights" in stmts[0].text
    assert 'x = "a;b"' in stmts[2].text and stmts[2].text.rstrip().endswith("$$")
    assert [s.optional for s in stmts] == [False, False, False, True, False]


def test_every_sql_file_parses_into_statements():
    files = sqlscript.sql_files()
    assert [f.name for f in files] == sorted(f.name for f in files)
    for f in files:
        stmts = sqlscript.load(f.name)
        assert stmts, f.name
        for s in stmts:
            assert s.code, f"{f.name}: empty statement"
            assert s.text.count("$$") % 2 == 0, f"{f.name}: unbalanced $$"


# --------------------------------------------------------------------------- dry run


def test_dry_run_is_deterministic_and_covers_every_deployed_file(capsys):
    assert deploy.main(["--step", "all", "--dry-run", "--cross-region", "--connection", "X"]) == 0
    first = capsys.readouterr().out
    assert deploy.main(["--step", "all", "--dry-run", "--cross-region", "--connection", "X"]) == 0
    assert capsys.readouterr().out == first
    for name in DEPLOYED_FILES:
        assert f"-- sql file: {name}" in first, name
    assert "99_teardown.sql" not in first
    for task in ("load_worlds", "load_golden", "build_zips", "upload_code", "smoke_version", "parity", "load_docs",
                 "smoke_search", "smoke_semantic", "upload_app"):
        assert f"-- python: {task}" in first, task
    # every statement text is printed verbatim
    for s in sqlscript.load("30_procs.sql"):
        assert s.text + ";" in first


def test_dry_run_never_connects(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("dry run must not connect")

    monkeypatch.setattr(deploy, "connect", boom)
    assert deploy.main(["--step", "bootstrap,load", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "steps: bootstrap, load" in out


def test_connection_default_and_env(monkeypatch, capsys):
    monkeypatch.delenv(deploy.CONNECTION_ENV, raising=False)
    deploy.main(["--step", "agent", "--dry-run"])
    assert "connection: VJ22713 " in capsys.readouterr().out
    monkeypatch.setenv(deploy.CONNECTION_ENV, "other_conn")
    deploy.main(["--step", "agent", "--dry-run"])
    assert "connection: other_conn " in capsys.readouterr().out


def test_step_parsing():
    assert deploy.parse_steps("all") == list(deploy.STEPS)
    assert deploy.parse_steps("all", cross_region=True)[0] == "cross-region"
    assert deploy.parse_steps("bootstrap", cross_region=True) == ["cross-region", "bootstrap"]
    assert deploy.parse_steps("load, procs,load") == ["load", "procs"]
    assert deploy.parse_steps("parity") == ["parity"]
    with pytest.raises(deploy.DeployError):
        deploy.parse_steps("drop-everything")
    assert deploy.main(["--step", "bogus", "--dry-run"]) == 1


def test_governance_variants_per_edition():
    names = lambda ed: [t.name for t in deploy.step_tasks("governance", ed)]  # noqa: E731
    assert names("standard") == ["90_governance.sql"]
    assert names("enterprise") == ["90_governance.sql", "91_governance_enterprise.sql"]
    auto = deploy.step_tasks("governance", "auto")
    assert auto[1].optional_file and not deploy.step_tasks("governance", "enterprise")[1].optional_file


# --------------------------------------------------------------------------- object coverage

EXPECTED_OBJECTS = {
    # account
    "WAPSI_WH", "WAPSI_RM", "WAPSI_ADMIN", "WAPSI_APP_OWNER", "WAPSI_ANALYST", "WAPSI_SUPERVISOR", "WAPSI_AUDITOR",
    "WAPSI_LEA_READONLY", "WAPSI_ACTIONS_RUNNER", "WAPSI_ML",
    # database, schemas, stages
    "WAPSI", "WAPSI.RAW", "WAPSI.RAW_SHOWCASE", "WAPSI.ML", "WAPSI.CORE", "WAPSI.CASEMGMT", "WAPSI.DOCS", "WAPSI.APP",
    "WAPSI.RAW.WORLD_STAGE", "WAPSI.APP.CODE", "WAPSI.APP.SIS_STAGE", "WAPSI.DOCS.DOCS_STAGE",
    # case management
    "WAPSI.CASEMGMT.CASES", "WAPSI.CASEMGMT.CASE_BUNDLES", "WAPSI.CASEMGMT.TRAIL_EDGES", "WAPSI.CASEMGMT.ACCOUNT_STATE",
    "WAPSI.CASEMGMT.CLASSIFICATIONS", "WAPSI.CASEMGMT.HOLD_PLAN", "WAPSI.CASEMGMT.DRAFTS",
    "WAPSI.CASEMGMT.VERIFICATIONS", "WAPSI.CASEMGMT.APPROVALS", "WAPSI.CASEMGMT.ACTIONS", "WAPSI.CASEMGMT.CLOCKS",
    "WAPSI.CASEMGMT.AUDIT_LOG", "WAPSI.CASEMGMT.AUDIT_ANCHOR", "WAPSI.CASEMGMT.LIENS", "WAPSI.CASEMGMT.V_CLOCKS",
    "WAPSI.CASEMGMT.V_LEA_CHECKS",
    # procedures
    "WAPSI.CASEMGMT.TRACE_CASE", "WAPSI.CASEMGMT.BUILD_HOLD_PLAN", "WAPSI.CASEMGMT.CASE_SUMMARY",
    "WAPSI.CASEMGMT.DRAFT", "WAPSI.CASEMGMT.VERIFY", "WAPSI.CASEMGMT.APPROVE", "WAPSI.APP.WAPSI_VERSION",
    "WAPSI.CASEMGMT.REQUEST_ACCOUNT_LEVEL_HOLD", "WAPSI.CASEMGMT.APPROVE_ACCOUNT_LEVEL", "WAPSI.CASEMGMT.CHECK_LEA",
    "WAPSI.CASEMGMT.APPEND_AUDIT",
    # docs, search, semantic view, agent, app
    "WAPSI.DOCS.DOC_CHUNKS", "WAPSI.DOCS.CITATIONS", "WAPSI.DOCS.SOP_SEARCH", "WAPSI.APP.WAPSI_FRAUD_SV",
    "WAPSI.APP.WAPSI_OFFICER", "WAPSI.APP.WAPSI_APP", "WAPSI.APP.EVAL_RUNS", "WAPSI.APP.USER_ROLES",
    "WAPSI.APP.SETTINGS", "WAPSI.APP.DEMO_CLOCK", "WAPSI.APP.V_ACCOUNT_BRANCH",
    # governance
    "WAPSI.APP.V_CUSTOMERS", "WAPSI.APP.V_COMPLAINTS", "WAPSI.APP.V_CASES", "WAPSI.APP.V_HOLD_PLAN",
    "WAPSI.APP.V_TRAIL_EDGES", "WAPSI.APP.MASK_ACCOUNT_ID", "WAPSI.APP.MASK_PII",
    # ML
    "WAPSI.ML.LABELS_ACCOUNTS", "WAPSI.ML.LABELS_FLOWS", "WAPSI.ML.LABELS_COMPLAINTS", "WAPSI.ML.GOLDEN_CASES",
}
# Named in the long-form playbook but deferred past this submission (COCO_SPRINT.md "Deferred").
DEFERRED = {
    "WAPSI.APP.INJECT_DEMO_COMPLAINT", "WAPSI.APP.WAPSI_MCP", "WAPSI.CASEMGMT.INTAKE_QUEUE",
    "WAPSI.CASEMGMT.OPEN_CASE", "WAPSI.CASEMGMT.RUN_CLOCKS", "WAPSI.CASEMGMT.T_APPLY_ACTIONS",
    "WAPSI.CASEMGMT.T_OPEN_CASES", "WAPSI.CORE.ACCOUNT_FEATURES_24H", "WAPSI.ML.MULE_SCORES", "WAPSI.ML.MULE_XGB",
    "WAPSI.RAW.COMPLAINTS_STREAM",
}
# Names used only to show the guard hook blocking a DROP (they never exist) or by the old smoke schema.
PLACEHOLDERS = {"WAPSI.DOES_NOT_EXIST", "WAPSI.GUARD_TEST", "WAPSI.X", "WAPSI.SMOKE"}
REF_RE = re.compile(r"\bWAPSI\.[A-Z_]+(?:\.[A-Z_0-9]+)?\b")


def _refs(paths) -> set[str]:
    out = set()
    for p in paths:
        for m in REF_RE.findall(p.read_text(encoding="utf-8")):
            if m.startswith("WAPSI.INFORMATION_SCHEMA") or m.endswith("_") or "DEMO_HOLDBACK" in m:
                continue
            out.add(m)
    return out


def _known() -> set[str]:
    raw_tables = {f"WAPSI.{s}.{t.upper()}" for s in ("RAW", "RAW_SHOWCASE") for t in deploy.LOAD_ORDER
                  if not t.startswith("labels_")}
    return _created() | raw_tables


def test_sql_creates_every_expected_object():
    missing = EXPECTED_OBJECTS - _known()
    assert not missing, sorted(missing)


def test_every_object_the_skills_reference_exists():
    skills = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md")) + sorted(
        (ROOT / ".claude" / "skills").glob("*/scripts/*.py"))
    missing = _refs(skills) - _known()
    assert not missing, sorted(missing)


def test_every_object_the_playbook_and_sprint_reference_exists_or_is_deferred():
    docs = [EDITION / "COCO_PLAYBOOK.md", EDITION / "COCO_SPRINT.md", EDITION / "README.md",
            *sorted((EDITION / "templates").glob("*.md"))]
    missing = _refs(docs) - _known() - DEFERRED - PLACEHOLDERS
    assert not missing, sorted(missing)


# --------------------------------------------------------------------------- guard hook + cost limits


def test_guard_hook_blocks_nothing_but_the_teardown():
    guard = _guard()
    blocked = {}
    for f in sqlscript.sql_files():
        for s in sqlscript.load(f.name):
            hits = guard.evaluate(s.text, env={})
            if hits:
                blocked.setdefault(f.name, []).append(hits[0][0])
    assert blocked == {"99_teardown.sql": ["drop-db-schema"]}
    # whole files, as CoCo would send them in one call
    for name in DEPLOYED_FILES:
        assert guard.evaluate((EDITION / "sql" / name).read_text(encoding="utf-8"), env={}) == [], name


def test_warehouse_size_and_auto_suspend_limits():
    sizes = re.compile(r"WAREHOUSE_SIZE\s*=\s*'?([A-Z0-9-]+)", re.I)
    suspend = re.compile(r"AUTO_SUSPEND\s*=\s*(\d+)", re.I)
    for s in ALL_STATEMENTS:
        for m in sizes.findall(s.code):
            assert m.upper() in ("XSMALL", "X-SMALL", "SMALL"), s.code[:80]
        for m in suspend.findall(s.code):
            assert 60 <= int(m) <= 300, s.code[:80]
    assert any("STATEMENT_TIMEOUT_IN_SECONDS = 300" in s.code for s in ALL_STATEMENTS)


def test_ml_is_never_granted_to_app_roles():
    app_roles = ("WAPSI_ANALYST", "WAPSI_SUPERVISOR", "WAPSI_AUDITOR", "WAPSI_LEA_READONLY", "WAPSI_APP_OWNER")
    for s in ALL_STATEMENTS:
        code = " ".join(s.code.upper().split())
        if code.startswith("GRANT") and "WAPSI.ML" in code:
            assert not any(f"TO ROLE {r}" in code for r in app_roles), code


def test_case_history_tables_are_never_replaced_or_dropped():
    for s in ALL_STATEMENTS:
        if s.source.startswith("99_"):
            continue
        code = " ".join(s.code.upper().split())
        assert not re.search(r"CREATE OR REPLACE TABLE", code), code[:80]
        assert not re.search(r"\b(DROP|TRUNCATE)\b[^;]*CASEMGMT", code), code[:80]
        assert not re.search(r"\b(UPDATE|DELETE FROM|MERGE INTO)\s+WAPSI\.CASEMGMT\.(AUDIT_LOG|AUDIT_ANCHOR|APPROVALS)\b",
                             code)


@pytest.mark.parametrize("sql", [
    "UPDATE WAPSI.CASEMGMT.AUDIT_LOG SET hash = 'x' WHERE seq = 3",
    "DELETE FROM WAPSI.CASEMGMT.AUDIT_ANCHOR WHERE case_id = 'CASE-1'",
    "MERGE INTO CASEMGMT.AUDIT_ANCHOR a USING x ON 1 = 1 WHEN MATCHED THEN DELETE",
    "delete from audit_log where seq > 10",
])
def test_guard_keeps_the_audit_chain_append_only(sql):
    hits = _guard().evaluate(sql, env={"WAPSI_ALLOW_DESTRUCTIVE": "1"})
    assert [h[0] for h in hits] == ["audit-append-only"]


def test_audit_log_is_insert_only_for_every_role():
    """Grants on the audit chain are INSERT / SELECT only; the actions runner writes it only through APPEND_AUDIT."""
    for s in ALL_STATEMENTS:
        code = " ".join(s.code.upper().split())
        if code.startswith("GRANT") and re.search(r"CASEMGMT\.(AUDIT_LOG|AUDIT_ANCHOR)\b", code):
            privs = code.split(" ON ", 1)[0].removeprefix("GRANT ")
            assert {p.strip() for p in privs.split(",")} <= {"INSERT", "SELECT"}, code
            assert "WAPSI_ACTIONS_RUNNER" not in code, code


# --------------------------------------------------------------------------- consistency with Python / templates


def _table_columns(name: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for s in sqlscript.load(name):
        m = re.match(r"CREATE TABLE IF NOT EXISTS (\S+) \((.*)\)(?:\s*COMMENT.*)?$", s.code, re.S)
        if not m:
            continue
        cols = {}
        for line in m.group(2).splitlines():
            line = line.strip().rstrip(",")
            if line:
                col, typ = line.split(None, 1)
                cols[col.upper()] = typ.split(" NOT NULL")[0].split(" DEFAULT")[0].strip()
        out[m.group(1).upper()] = cols
    return out


def test_raw_ddl_matches_the_synth_skill_reference():
    spec = importlib.util.spec_from_file_location(
        "load_world", ROOT / ".claude" / "skills" / "synth-fraud-ledger" / "scripts" / "load_world.py")
    lw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lw)
    reference, _ = lw.parse_ddl()
    ours = _table_columns("10_raw.sql")
    for table, cols in reference.items():
        schema = "ML" if table.startswith("LABELS_") else "RAW"
        assert ours[f"WAPSI.{schema}.{table}"] == cols, table


def test_raw_ddl_matches_the_parquet_world_schema():
    from wapsi.synth.export import SCHEMAS

    ours = _table_columns("10_raw.sql")
    for table, schema in SCHEMAS.items():
        target = f"WAPSI.{'ML' if table.startswith('labels_') else 'RAW'}.{table.upper()}"
        assert set(ours[target]) == {f.name.upper() for f in schema}, table


def test_casemgmt_ddl_matches_the_case_store():
    from wapsi_sf.casestore import COLUMNS

    ours = _table_columns("20_casemgmt.sql")
    for table, cols in COLUMNS.items():
        ddl = ours[f"WAPSI.CASEMGMT.{table}"]
        assert list(ddl) == [c.upper() for c, _ in cols], table
        for c, t in cols:
            assert ddl[c.upper()] == ("VARCHAR" if t == "STRING" else t), (table, c)


# columns that the skills' SQL snippets read or write
SKILL_COLUMNS = {
    "HOLD_PLAN": {"account_id", "layer", "role", "disposition", "tainted_amount", "balance_now", "hold_amount",
                  "forward_hold_amount", "citation_ids", "reason_codes", "requires_human", "account_type"},
    "CASES": {"total_hold", "total_forward_hold", "total_recoverable", "over_hold_amount", "baseline_freeze_amount",
              "baseline_freeze_accounts", "restoration_process", "str_ground", "case_id"},
    "ACCOUNT_STATE": {"layer", "account_id", "bank_id", "account_type", "is_home_bank", "tainted_received_total",
                      "tainted_sent_total", "tainted_now_total", "balance_now", "terminal", "case_id"},
    "TRAIL_EDGES": {"depth", "ts", "from_account", "to_account", "channel", "utr", "amount", "tainted_total",
                    "via_consortium", "txn_id", "case_id"},
    "VERIFICATIONS": {"passed", "checks", "draft_id"},
    "DRAFTS": {"title", "body", "citation_ids", "llm_model", "draft_id"},
    "ACTIONS": {"action_id", "case_id", "kind", "payload", "status", "created_at"},
    "AUDIT_LOG": {"action", "details"},
}


def test_skill_sql_columns_exist():
    ours = _table_columns("20_casemgmt.sql")
    for table, cols in SKILL_COLUMNS.items():
        have = {c.lower() for c in ours[f"WAPSI.CASEMGMT.{table}"]}
        assert cols <= have, (table, sorted(cols - have))
    evals = _table_columns("20_casemgmt.sql")["WAPSI.APP.EVAL_RUNS"]
    assert {"WORLD", "GENERATED_AT", "CASES", "REPORT", "SOURCE"} <= set(evals)


def test_agent_sql_carries_the_template_instructions_and_real_tools():
    template = (EDITION / "templates" / "agent_instructions.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(.*?)```", template, re.S)
    agent = (EDITION / "sql" / "80_agent.sql").read_text(encoding="utf-8")
    for block in blocks[:2]:  # response + orchestration instructions
        for line in block.splitlines():
            if line.strip():
                assert line.strip() in agent, line
    procs = {n for k, n in sqlscript.created_objects(ALL_STATEMENTS) if k == "PROCEDURE"}
    for ident in re.findall(r'identifier: "([^"]+)"', agent):
        assert ident in procs, ident
    assert "WAPSI.CASEMGMT.APPROVE" not in agent  # the agent can never approve
    assert "WAPSI.CASEMGMT.REQUEST_ACCOUNT_LEVEL_HOLD" not in agent  # nor request an account-level exception
    assert 'identifier: "WAPSI.CASEMGMT.CHECK_LEA"' in agent and 'name: "check_lea_request"' in agent
    assert agent.count("execution_environment:") == 7  # analyst + 6 procedures
    assert '"WAPSI.DOCS.SOP_SEARCH"' in agent and '"WAPSI.APP.WAPSI_FRAUD_SV"' in agent


def _semantic_sources() -> dict[str, set[str]]:
    from wapsi_sf.casestore import COLUMNS

    view_cols = {}
    for s in sqlscript.load("20_casemgmt.sql"):
        m = re.match(r"CREATE OR REPLACE VIEW (\S+).*?AS\s+SELECT (.*)\s+FROM \S+(?:\s+\w+)?\s*$", s.code, re.S)
        if m:
            view_cols[m.group(1)] = {re.split(r"\s+AS\s+|\.", c.strip())[-1].strip().lower()
                                     for c in m.group(2).split(",\n")}
    return {
        "cases": {c for c, _ in COLUMNS["CASES"]},
        "holds": {c for c, _ in COLUMNS["HOLD_PLAN"]},
        "edges": {c for c, _ in COLUMNS["TRAIL_EDGES"]},
        "clocks": view_cols["WAPSI.CASEMGMT.V_CLOCKS"],
        "account_branch": view_cols["WAPSI.APP.V_ACCOUNT_BRANCH"],
    }


def test_semantic_view_expressions_reference_real_columns():
    sv = sqlscript.load("70_semantic_view.sql")[1].code
    assert sv.startswith("CREATE OR REPLACE SEMANTIC VIEW WAPSI.APP.WAPSI_FRAUD_SV")
    sources = _semantic_sources()
    defined = set(re.findall(r"^\s*((?:cases|holds|edges|clocks|account_branch)\.\w+)\s+AS\b", sv, re.M))
    body = sv.split("FACTS (", 1)[1]
    for alias, col in re.findall(r"\b(cases|holds|edges|clocks|account_branch)\.(\w+)", body):
        assert col in sources[alias] or f"{alias}.{col}" in defined, f"{alias}.{col}"
    for m in ("holds.held_amount", "holds.recoverable_amount", "holds.over_hold_amount", "cases.cases_open",
              "clocks.lapsing_holds_15d", "edges.cashed_out_amount", "holds.disposition_type",
              "account_branch.branch", "holds.account_level_exceptions", "holds.str_recommended_conduits",
              "clocks.forward_reviews_15d", "clocks.clock_notes"):
        assert m in defined, m


def test_packages_are_anaconda_names_and_in_sync():
    from build_zip import APP_PACKAGES, PROC_PACKAGES

    procs = (EDITION / "sql" / "30_procs.sql").read_text(encoding="utf-8")
    pkg_lists = re.findall(r"PACKAGES = \(([^)]*)\)", procs)
    assert len(pkg_lists) == 11
    for lst in pkg_lists:
        assert tuple(p.strip().strip("'") for p in lst.split(",")) == PROC_PACKAGES
    assert set(re.findall(r"RUNTIME_VERSION = '([\d.]+)'", procs)) == {"3.12"}
    env = (EDITION / "app" / "environment.yml").read_text(encoding="utf-8")
    deps = re.findall(r"^\s+- ([\w.=-]+)$", env, re.M)
    assert "python=3.11" in deps and set(APP_PACKAGES) <= set(deps)
    assert "channels:\n  - snowflake" in env
    assert "RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'" in (EDITION / "sql" / "85_app.sql").read_text(encoding="utf-8")
