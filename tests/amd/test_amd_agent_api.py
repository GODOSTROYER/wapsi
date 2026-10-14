"""The AMD FastAPI app's agent endpoints, the CLI and the serving flags for native tool calls (offline)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import replace

import pytest

pytest.importorskip("fastapi", reason="amd-app extra not installed")
pytest.importorskip("google.adk", reason="agent extra not installed (uv sync --extra agent)")
pytest.importorskip("litellm", reason="agent extra not installed (uv sync --extra agent)")

from editions.amd.app import agent as agent_mod  # noqa: E402
from editions.amd.app import agent_demo  # noqa: E402
from editions.shared.fake_openai import OFFLINE_KEY, FakeOpenAIBackend  # noqa: E402
from wapsi.fixtures import showcase as sc  # noqa: E402

CASE1 = f"CASE-{sc.N1}"
HOLD_A = f"DRF-{CASE1}-HOLD_INSTRUCTION-{sc.A}"
PIPELINE = ["open_case", "trace_money", "hold_plan", "verification_report", "draft_documents"]


@pytest.fixture()
def client(demo_services, monkeypatch):
    from fastapi.testclient import TestClient

    from editions.amd.app.api import app

    monkeypatch.setenv("WAPSI_AGENT_BACKEND", "offline")
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- API


def test_agent_info_and_healthz_describe_the_agent_without_secrets(client):
    info = client.get("/agent").json()
    assert info["available"] is True and info["backend"] == "offline" and info["framework"] == "google-adk"
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["agent"]["available"] is True
    assert health["agent"]["backend"] == "offline"


def test_agent_run_returns_reply_tool_sequence_and_sealed_trace(client, demo_services):
    r = client.post("/agent/run", json={"message": f"Open case {sc.N1}", "user": "asha"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["tools"] == PIPELINE and out["case_ids"] == [CASE1]
    assert out["reply_source"] == "model" and out["grounded"] is True and out["role"] == "analyst"
    assert "₹3,34,848.00" in out["reply"] and "₹5,03,50,000.00" in out["reply"]
    assert out["summary"]["tool_calls"] == 5 and out["summary"]["blocked"] == 0 and out["summary"]["model_turns"] == 6
    assert out["summary"]["verifier_failed"] == 0 and out["wall_ms"] > 0
    assert out["agent"]["backend"] == "offline" and out["agent"]["tool_mode"] == "native"
    assert [s["name"] for s in out["trace"]["steps"] if s["kind"] == "tool"] == PIPELINE
    assert out["trace"]["source"] == "adk-endpoint-offline"
    assert out["trace"]["model"] == "openai/wapsi-offline-planner"  # the offline planner, named as such
    assert OFFLINE_KEY not in r.text

    # the same run, read back from the case's hash-chained audit log
    stored = client.get("/agent/traces", params={"case_id": CASE1}).json()
    assert stored["source"] == "audit_log" and stored["count"] == 1
    assert stored["audit_chain"] == {"ok": True, "events": len(demo_services.store.get_bundle(CASE1).audit),
                                     "reason": "", "sealed_trace_events": 1}
    assert stored["traces"][0]["trace_id"] == out["trace_id"]
    assert stored["summaries"][0]["tools"] == PIPELINE and stored["summaries"][0]["provider"] == "offline fake endpoint"
    recent = client.get("/agent/traces").json()
    assert recent["source"] == "process" and recent["count"] == 1 and recent["audit_chain"] is None

    # a follow-up in the case room: the case in view is passed to the agent
    why = client.post("/agent/run", json={"message": f"Why is {sc.L} held? Explain the account.", "user": "asha",
                                          "case_id": CASE1, "include_trace": False}).json()
    assert why["tools"] == ["explain_account"] and "trace" not in why and "HOLD_AMOUNT_NO_ESCALATION" in why["reply"]
    assert client.get("/agent/traces", params={"case_id": CASE1, "limit": 1}).json()["count"] == 1

    metrics = client.get("/metrics").text
    assert "wapsi_agent_runs_total 2" in metrics and "wapsi_agent_tool_calls_total 6" in metrics
    assert "wapsi_agent_failures_total 0" in metrics


def test_agent_run_validates_input_and_unknown_cases(client):
    assert client.post("/agent/run", json={"message": ""}).status_code == 422
    assert client.post("/agent/run", json={"message": "hi", "role": "root"}).status_code == 422
    assert client.post("/agent/run", json={"message": "hi", "user": "a/b"}).status_code == 422
    assert client.post("/agent/run", json={"message": "hi", "case_id": "CASE-404"}).status_code == 404
    assert client.post("/agent/run", json={"message": "hi", "case_id": "../etc"}).status_code == 400
    assert client.get("/agent/traces", params={"case_id": "CASE-404"}).status_code == 404
    assert client.get("/agent/traces", params={"limit": 0}).status_code == 422


def test_agent_role_needs_an_authenticated_caller_outside_demo_mode(client, demo_services):
    assert client.post("/agent/run", json={"message": f"Open case {sc.N1}", "user": "asha"}).status_code == 200
    approve = {"message": f"approve {HOLD_A} CHECKS_PASSED", "user": "ravi", "role": "supervisor", "case_id": CASE1}

    demo_services.settings = replace(demo_services.settings, demo_mode=False)  # not demo, no API token configured
    downgraded = client.post("/agent/run", json=approve).json()
    assert downgraded["role"] == "analyst" and "WAPSI_API_TOKEN" in downgraded["role_note"]
    assert downgraded["blocked"] == 1 and "supervisor" in downgraded["reply"]  # refused by maker-checker
    assert not demo_services.store.get_bundle(CASE1).approvals

    demo_services.settings = replace(demo_services.settings, api_token="s3cret")
    assert client.post("/agent/run", json=approve).status_code == 401
    assert client.post("/agent/run", json=approve, headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/agent/run", json=approve, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json()["role"] == "supervisor" and "role_note" not in ok.json()
    assert any(a.approver == "ravi" for a in demo_services.store.get_bundle(CASE1).approvals)
    assert client.get("/agent/traces", params={"case_id": CASE1}).json()["audit_chain"]["ok"] is True
    assert client.get("/agent").status_code == 200  # reads stay open (the port is bound to localhost)


def test_agent_run_reports_a_missing_extra(client, monkeypatch):
    monkeypatch.setattr(agent_mod, "missing_dependencies", lambda: ["google-adk", "litellm"])
    r = client.post("/agent/run", json={"message": "hi"})
    assert r.status_code == 503 and "uv sync --extra agent" in r.json()["detail"]
    assert client.get("/agent").json()["available"] is False
    assert client.get("/healthz").json()["agent"]["available"] is False  # the rest of the API keeps working


# --------------------------------------------------------------------------- CLI


def test_cli_runs_the_showcase_case_offline_and_verifies_the_outcome(tmp_path, capsys, caplog):
    with caplog.at_level(logging.INFO):  # a host that logs at INFO must not get log lines mixed into the JSON
        code = agent_demo.main(["--case", sc.N1, "--json", "--state-dir", str(tmp_path / "state")])
    out = json.loads(capsys.readouterr().out)
    assert code == agent_demo.EXIT_OK and out["live"] is False
    o = out["outcome"]
    assert o["verified"] is True and o["case_id"] == CASE1 and o["case_status"] == "VERIFIED"
    assert o["plan_policy_check"] is True and o["drafts_verified"] == o["drafts"] > 0
    assert o["audit_chain_ok"] is True and o["agent_traces_sealed"] == 1
    assert (o["replies_grounded"], o["replies_checked"], o["replies_replaced"]) == (1, 1, 0)
    assert o["total_recoverable"] == "₹3,34,848.00" and o["whole_account_freeze_baseline"] == "₹5,03,50,000.00"
    assert out["runs"][0]["tools"] == PIPELINE and out["runs"][0]["grounded"] is True
    m = out["measured"]
    assert m["label"].startswith("offline fake endpoint") and "not model measurements" in m["label"]
    assert m["model_calls"] == 6 and m["tool_mode"] == "native" and m["tool_result_role"] == "tool"
    assert out["endpoint"]["provider"] == "offline fake endpoint" and OFFLINE_KEY not in json.dumps(out)
    assert (tmp_path / "state" / "cases" / f"{CASE1}.json").exists()  # the store the UI would read


def test_cli_demo_script_prints_the_trace_and_the_stops(tmp_path, capsys):
    code = agent_demo.main(["--case", f"CASE-{sc.N1}", "--demo", "--fake-tools", "reject",
                            "--state-dir", str(tmp_path)])
    text = capsys.readouterr().out
    assert code == agent_demo.EXIT_OK
    assert "OFFLINE RUN" in text and "Google ADK" in text and "model wapsi-offline-planner" in text
    assert "Gemma" not in text  # an offline run never borrows a real model's name
    for tool in (*PIPELINE, "explain_account"):
        assert f"tool   {tool}" in text
    assert "approve_draft        refused" in text and "tool_policy:refused" in text
    assert "search_sop           blocked" in text and "tool_args:blocked" in text and "input:rewritten" in text
    assert "reply_grounding:passed" in text and "policy_check:passed" in text
    assert "VERIFIED" in text and "NOT VERIFIED" not in text and "4 agent run(s) sealed" in text
    assert "tool mode json" in text and "--enable-auto-tool-choice" in text  # why the fallback was chosen


def test_cli_exit_codes(tmp_path, capsys):
    assert agent_demo.main(["--live", "--state-dir", str(tmp_path)]) == agent_demo.EXIT_CONFIG
    assert "WAPSI_LLM_BASE_URL" in capsys.readouterr().err
    assert agent_demo.main(["--case", "99999999999999", "--state-dir", str(tmp_path)]) == agent_demo.EXIT_CONFIG
    assert "unknown complaint" in capsys.readouterr().err
    code = agent_demo.main(["--case", sc.N1, "--json", "--state-dir", str(tmp_path)],
                           fake=FakeOpenAIBackend(tools="garbage"))
    out = json.loads(capsys.readouterr().out)
    assert code == agent_demo.EXIT_NOT_VERIFIED and out["outcome"]["verified"] is False
    assert out["runs"][0]["ok"] is False and out["runs"][0]["tools"] == []


# --------------------------------------------------------------------------- serving flags for native tool calls


def _print(bash, amd_dir, profile, **env):
    inherited = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "WAPSI_"))}
    res = subprocess.run([bash, (amd_dir / "serving" / "start_llm.sh").as_posix(), "print", profile],
                         capture_output=True, text=True, env={**inherited, **env}, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_start_llm_adds_the_gemma4_tool_call_flags_for_the_agent(amd_dir, bash):
    off = _print(bash, amd_dir, "gemma4-base")
    assert "--enable-auto-tool-choice" not in off and "--tool-call-parser" not in off
    on = _print(bash, amd_dir, "gemma4-base", VLLM_ENABLE_TOOLS="1")
    assert "--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4" in on
    assert "--chat-template" not in on
    custom = _print(bash, amd_dir, "gemma4-merged", VLLM_ENABLE_TOOLS="1", VLLM_TOOL_CALL_PARSER="pythonic",
                    VLLM_REASONING_PARSER="none", VLLM_CHAT_TEMPLATE="examples/tool_chat_template_gemma4.jinja")
    assert "--enable-auto-tool-choice --tool-call-parser pythonic" in custom and "--reasoning-parser" not in custom
    assert "--chat-template examples/tool_chat_template_gemma4.jinja" in custom
    qwen = _print(bash, amd_dir, "qwen-fallback", VLLM_ENABLE_TOOLS="1")
    assert "--enable-auto-tool-choice --tool-call-parser qwen3_coder" in qwen


def test_compose_and_env_template_pass_the_agent_settings_through(amd_dir):
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((amd_dir / "docker-compose.yml").read_text(encoding="utf-8"))
    llm_env = compose["services"]["llm"]["environment"]
    for key in ("VLLM_ENABLE_TOOLS", "VLLM_TOOL_CALL_PARSER", "VLLM_REASONING_PARSER", "VLLM_CHAT_TEMPLATE"):
        assert key in llm_env, key
    app_env = compose["services"]["app"]["environment"]
    for key in ("WAPSI_AGENT_BACKEND", "WAPSI_AGENT_TOOL_MODE", "WAPSI_AGENT_MODEL"):
        assert key in app_env, key
    template = (amd_dir / ".env.example").read_text(encoding="utf-8")
    for key in ("VLLM_ENABLE_TOOLS", "WAPSI_AGENT_TOOL_MODE", "WAPSI_AGENT_MIN_INTERVAL_S"):
        assert key in template, key
    assert "WAPSI_LLM_API_KEY=" not in template and "WAPSI_AGENT_API_KEY=" not in template  # keys stay in the shell

    dockerfile = (amd_dir / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "--extra agent" in dockerfile and "COPY editions/shared ./editions/shared" in dockerfile
    gcp = (amd_dir.parent / "gcp" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY editions/shared ./editions/shared" in gcp  # the shared agent code ships in both images
