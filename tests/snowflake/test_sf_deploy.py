"""deploy.py against the offline session: loading, golden set, docs, parity, SQL execution rules."""

from __future__ import annotations

import json

import pytest

from editions.snowflake import deploy
from wapsi.citations import KNOWN_IDS


class ScriptSession:
    """Accepts any SQL, failing the statements whose text contains a configured marker."""

    def __init__(self, fail_on=(), message="SQL compilation error"):
        self.fail_on, self.message, self.sent = fail_on, message, []

    def sql(self, text, params=None):
        self.sent.append(text)
        fail = any(m in text for m in self.fail_on)
        message = self.message

        class R:
            def collect(self_inner):
                if fail:
                    raise RuntimeError(message)
                return []

        return R()


def test_load_world_reports_matching_row_counts(sf_session, tmp_path):
    from sf_helpers import showcase_world

    res = deploy.load_world(sf_session, showcase_world(tmp_path / "again"), "RAW_SHOWCASE", labels=False)
    assert res and all(n_pq == n_sf for _, _, n_pq, n_sf in res)
    assert ("transactions", "WAPSI.RAW_SHOWCASE.TRANSACTIONS", 21, 21) in res
    ts = sf_session.rows("SELECT ts FROM RAW_SHOWCASE.TRANSACTIONS WHERE txn_id = 'TX-001'")[0]["ts"]
    assert ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-10 05:32:00"  # stored as UTC wall-clock time


def test_load_golden_replaces_one_world(sf_session, tmp_path):
    (tmp_path / "golden" / "showcase").mkdir(parents=True)
    (tmp_path / "golden" / "showcase" / "cases.jsonl").write_text(
        json.dumps({"case_id": "CASE-1", "ncrp_ids": ["1"], "expected_hold": {"A": 1.0}}) + "\n", encoding="utf-8")
    assert deploy.load_golden(sf_session, ["showcase"], tmp_path) == {"showcase": 1}
    assert deploy.load_golden(sf_session, ["showcase"], tmp_path) == {"showcase": 1}
    rows = sf_session.rows("SELECT case_id, world, ncrp_ids, expected FROM ML.GOLDEN_CASES")
    assert len(rows) == 1 and json.loads(rows[0]["ncrp_ids"]) == ["1"]
    assert json.loads(rows[0]["expected"])["expected_hold"] == {"A": 1.0}


def test_committed_golden_sets_are_found():
    assert len(deploy.golden_rows("showcase")) == 2


def test_docs_load_only_registered_citation_ids(sf_session):
    chunks = sf_session.rows("SELECT citation_id, chunk_text FROM DOCS.DOC_CHUNKS")
    assert chunks and all(c["chunk_text"] for c in chunks)
    assert {c["citation_id"] for c in chunks if c["citation_id"]} <= KNOWN_IDS
    cites = sf_session.rows("SELECT citation_id FROM DOCS.CITATIONS")
    assert {c["citation_id"] for c in cites} == set(KNOWN_IDS)


def test_parity_passes_on_the_showcase(sf_session):
    lines: list[str] = []
    assert deploy.run_parity(sf_session, lines.append)
    text = "\n".join(lines)
    assert "MISMATCH" not in text and text.splitlines()[-1].startswith("parity: ")
    for amount in ("₹1,42,848.00", "₹96,000.00", "₹3,34,848.00", "₹43,200.00", "₹27,648.00", "₹48,000.00",
                   "₹4,800.00", "₹19,200.00", "₹1,20,000.00"):
        assert amount in text, amount
    chain = [line for line in lines if "audit chain" in line]
    assert len(chain) == 2 and all(line.rstrip().endswith("OK") and "intact" in line for line in chain)


def test_parity_comparison_flags_a_wrong_hold():
    (_, case_id, exp), _ = deploy.expected_showcase()
    case_row = {k: exp[k] for k in ("total_hold", "total_forward_hold", "total_recoverable", "over_hold_amount",
                                    "baseline_freeze_amount", "baseline_freeze_accounts", "restoration_process")}
    holds = [{"account_id": a, "hold_amount": v, "disposition": exp["dispositions"][a],
              "forward_hold_amount": exp["forward_hold"].get(a, 0.0)} for a, v in exp["hold"].items()]
    assert all(r.ok for r in deploy.compare_parity(case_id, exp, case_row, holds))
    holds[0] = {**holds[0], "hold_amount": holds[0]["hold_amount"] + 1}
    bad = [r for r in deploy.compare_parity(case_id, exp, case_row, holds) if not r.ok]
    assert len(bad) == 1 and bad[0].item.startswith("hold ")
    assert exp["total_hold"] == 142848.0 and exp["total_recoverable"] == 334848.0


def test_run_sql_file_optional_statements_and_edition_probe():
    lines: list[str] = []
    s = ScriptSession(fail_on=("EXECUTE MANAGED TASK",))
    assert deploy.run_sql_file(s, "00_account.sql", lines.append) == "ok"
    assert any("optional statement failed" in line for line in lines)
    with pytest.raises(deploy.DeployError):
        deploy.run_sql_file(ScriptSession(fail_on=("CREATE WAREHOUSE",)), "00_account.sql", lines.append)
    probe = ScriptSession(fail_on=("CREATE MASKING POLICY",), message="Unsupported feature 'masking policy'.")
    assert deploy.run_sql_file(probe, "91_governance_enterprise.sql", lines.append, edition_probe=True) == "standard"
    with pytest.raises(deploy.DeployError):
        deploy.run_sql_file(probe, "91_governance_enterprise.sql", lines.append, edition_probe=False)


def test_execute_runs_steps_in_order_with_context(sf_session, capsys):
    plan = deploy.build_plan(["parity"], connection="test")
    sf_session.on(r"CURRENT_REGION", lambda s, q, p: [{"R": "WAPSI_ADMIN", "W": "WAPSI_WH", "G": "AWS_AP_SOUTHEAST_1"}])
    assert deploy.execute(plan, sf_session, worlds=["showcase"], data_root=deploy.ROOT) == 0
    out = capsys.readouterr().out
    assert "connected: role WAPSI_ADMIN, warehouse WAPSI_WH, region AWS_AP_SOUTHEAST_1" in out
    assert "parity: " in out and "deploy finished OK" in out
    sent = [q for q, _ in sf_session.log]
    assert sent.index("USE ROLE WAPSI_ADMIN") < next(i for i, q in enumerate(sent) if q.startswith("CALL"))


def test_package_and_app_uploads(sf_session, tmp_path, monkeypatch):
    import build_zip

    monkeypatch.setattr(build_zip, "DIST", tmp_path)
    ctx = {"worlds": [], "data_root": tmp_path}
    for name in ("build_zips", "upload_code", "upload_app"):
        deploy.run_python(sf_session, deploy.Task("py", name), ctx, lambda _: None)
    assert ("wapsi.zip", "@WAPSI.APP.CODE") in sf_session.file.puts
    assert {n for n, st in sf_session.file.puts if st == "@WAPSI.APP.SIS_STAGE/wapsi"} == {
        "streamlit_app.py", "sf_data_source.py", "environment.yml", "wapsi_app.zip"}


def test_smoke_version_through_call(sf_session):
    lines: list[str] = []
    assert deploy.run_python(sf_session, deploy.Task("py", "smoke_version"), {}, lines.append)
    assert '"matches_fixture": true' in lines[0]


def test_real_llm_eval_scores_the_golden_set_and_records_a_run(sf_session):
    lines: list[str] = []
    report = deploy.run_eval(sf_session, "showcase", out=lines.append)
    m = report["metrics"]
    assert m["cases"] == 2 and m["errors"] == 0
    assert m["hold_exact_match"] == 1.0 and m["totals_exact_match"] == 1.0 and m["over_hold_inr"] == 0.0
    assert m["drafts"] > 0 and m["verified_first_time_rate"] == 1.0 and m["needs_human"] == 0
    assert m["llm_calls"] > 0 and m["tokens_total"] > 0
    row = sf_session.rows("SELECT world, cases, source, report FROM APP.EVAL_RUNS")[0]
    assert (row["world"], row["cases"], row["source"]) == ("showcase", 2, "cortex-ai-complete")
    assert json.loads(row["report"])["metrics"]["hold_exact_match"] == 1.0
    assert any("verified_first_time_rate" in line for line in lines)
    assert "eval" not in deploy.parse_steps("all")


def test_eval_without_drafts_only_fills_case_tables(sf_session):
    report = deploy.run_eval(sf_session, "showcase", draft=False, out=lambda _: None)
    assert report["metrics"]["hold_exact_match"] == 1.0 and report["metrics"]["llm_calls"] == 0
    assert sf_session.rows("SELECT COUNT(*) AS n FROM APP.EVAL_RUNS")[0]["n"] == 0
    assert sf_session.rows("SELECT COUNT(*) AS n FROM CASEMGMT.CASES")[0]["n"] == 2
    assert not sf_session.ai_prompts
