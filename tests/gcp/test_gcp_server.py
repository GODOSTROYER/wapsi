from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from editions.gcp.app.config import GcpSettings, load_settings
from editions.gcp.app.identity import ROLE_KEY, ROLE_SIG_KEY, verified_role
from editions.gcp.app.server import create_app
from editions.gcp.app.services import build_services
from wapsi.fixtures import showcase as sc

HOLD_A = f"DRF-CASE-{sc.N1}-HOLD_INSTRUCTION-{sc.A}"
APP = "wapsi_officer"


@pytest.fixture()
def client():
    settings = GcpSettings(demo_mode=True, serve_adk_web=False, supervisors=("ravi@bank.example",))
    services = build_services(settings)
    app = create_app(settings, services=services)
    with TestClient(app) as c:
        c.services = services
        yield c
    from editions.gcp.app.services import set_services

    set_services(None)


def _session(client, user, body=None, headers=None):
    r = client.post(f"/apps/{APP}/users/{user}/sessions", json=body, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def _run(client, user, sid, text, state_delta=None, headers=None):
    body = {"app_name": APP, "user_id": user, "session_id": sid,
            "new_message": {"role": "user", "parts": [{"text": text}]}}
    if state_delta is not None:
        body["state_delta"] = state_delta
    r = client.post("/run", json=body, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def _tool_response(events, name):
    for ev in events:
        for p in ev.get("content", {}).get("parts", []):
            fr = p.get("functionResponse") or p.get("function_response")
            if fr and fr["name"] == name:
                return fr["response"]
    raise AssertionError(f"no {name} response")


def test_healthz_and_apps(client):
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["settings"]["demo_mode"] is True
    assert health["settings"]["guard"] == "rules" and "role_signing_key" not in json.dumps(health)
    assert client.get("/list-apps").json() == ["case_pipeline", "wapsi_officer"]


def test_client_cannot_grant_itself_a_role(client):
    forged = {"state": {ROLE_KEY: "supervisor", ROLE_SIG_KEY: "0" * 64, "user:wapsi_name": "Boss", "note": 1},
              "events": [{"author": "user", "invocationId": "x",
                          "actions": {"stateDelta": {ROLE_KEY: "supervisor"}}}]}
    s = client.post(f"/apps/{APP}/users/asha/sessions", json=forged)
    assert s.status_code == 200, s.text
    state = s.json()["state"]
    assert state["note"] == 1 and state[ROLE_KEY] == "analyst" and state["user:wapsi_name"] == "asha"
    assert verified_role(state, "asha") == "analyst"

    events = _run(client, "asha", s.json()["id"], f"approve {HOLD_A}",
                  state_delta={ROLE_KEY: "supervisor", ROLE_SIG_KEY: "f" * 64})
    assert _tool_response(events, "approve_draft")["status"] == "refused"

    patched = client.patch(f"/apps/{APP}/users/asha/sessions/{s.json()['id']}",
                           json={"state_delta": {ROLE_KEY: "supervisor", "other": "x"}})
    if patched.status_code == 200:
        assert patched.json()["state"][ROLE_KEY] == "analyst"
        assert patched.json()["state"]["other"] == "x"


def test_deprecated_create_with_id_is_scrubbed(client):
    r = client.post(f"/apps/{APP}/users/asha/sessions/fixed-id", json={ROLE_KEY: "supervisor", "k": "v"})
    if r.status_code == 200:
        assert r.json()["state"][ROLE_KEY] == "analyst" and r.json()["state"]["k"] == "v"


def test_demo_role_header_enables_supervisor_flow(client):
    analyst = _session(client, "asha")
    events = _run(client, "asha", analyst["id"], f"Open case {sc.N1}")
    assert _tool_response(events, "hold_plan")["status"] == "ok"

    sup = _session(client, "ravi", headers={"X-Wapsi-Role": "supervisor"})
    assert sup["state"][ROLE_KEY] == "supervisor"
    events = _run(client, "ravi", sup["id"], f"approve {HOLD_A}")
    assert _tool_response(events, "approve_draft")["status"] == "approved"
    assert client.services.store.get_bundle(f"CASE-{sc.N1}").approvals[-1].approver == "ravi"

    by_name = _session(client, "supervisor-meena")
    assert by_name["state"][ROLE_KEY] == "supervisor"


def test_iap_identity_binds_user_and_role(client):
    iap = {"X-Goog-Authenticated-User-Email": "accounts.google.com:Ravi@Bank.example"}
    assert client.post(f"/apps/{APP}/users/someone/sessions", json={}, headers=iap).status_code == 403
    s = _session(client, "ravi@bank.example", headers={**iap, "X-Wapsi-Role": "auditor"})
    assert s["state"][ROLE_KEY] == "supervisor"  # IAP wins over the demo header
    other = {"X-Goog-Authenticated-User-Email": "accounts.google.com:asha@bank.example"}
    assert _session(client, "asha@bank.example", headers=other)["state"][ROLE_KEY] == "analyst"
    r = client.post("/run", headers=iap, json={"app_name": APP, "user_id": "mallory@bank.example",
                                               "session_id": s["id"], "new_message": {"role": "user",
                                                                                      "parts": [{"text": "hi"}]}})
    assert r.status_code == 403
    # reads and deletes are bound to the signed-in user too
    assert client.get("/apps/wapsi_officer/users/asha@bank.example/sessions", headers=iap).status_code == 403
    assert client.delete(f"/apps/wapsi_officer/users/asha@bank.example/sessions/{s['id']}",
                         headers=iap).status_code == 403
    assert client.get("/apps/wapsi_officer/users/ravi@bank.example/sessions", headers=iap).status_code == 200


def test_invalid_json_rejected(client):
    r = client.post(f"/apps/{APP}/users/asha/sessions", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_pubsub_trigger_runs_case_pipeline(client):
    data = base64.b64encode(json.dumps({"ncrp_ids": [sc.N2, sc.N3]}).encode()).decode()
    r = client.post("/apps/case_pipeline/trigger/pubsub",
                    json={"message": {"data": data, "messageId": "1", "attributes": {"source": "cfcfrms"}},
                          "subscription": "projects/p/subscriptions/wapsi-notices"})
    assert r.status_code == 200 and r.json()["status"] == "success"
    assert [c.case_id for c in client.services.store.list_cases()] == [f"CASE-{sc.N2}"]
    assert client.services.sink.actions[-1].payload["channel"] == "supervisors"


def test_settings_from_env():
    s = GcpSettings.from_env({})
    assert s.demo_mode and s.location == "global" and s.region == "asia-south1" and s.model == "gemini-3.8-flash"
    assert s.memory_service_uri() is None and s.model_armor() is None
    assert s.gemini_backend == "aistudio" and s.ledger == "bigquery" and s.adk_store == "memory"

    env = {"WAPSI_PROJECT": "wapsi-aibc", "WAPSI_BQ_DATASET": "ledger", "WAPSI_MODEL": "gemini-3.6-flash",
           "WAPSI_MODEL_ARMOR_TEMPLATE": "wapsi-intake", "WAPSI_MEMORY_BANK_ID": "789",
           "WAPSI_SUPERVISORS": "Ravi@bank.example, meena@bank.example", "WAPSI_PRIVATE_LLM_URL": "https://g.run.app"}
    c = load_settings(env)
    assert not c.demo_mode and not c.serve_adk_web and c.bq_dataset == "ledger"
    assert c.model_armor() == ("wapsi-aibc", "asia-south1", "wapsi-intake")
    assert c.agent_engine_resource() == "projects/wapsi-aibc/locations/asia-south1/reasoningEngines/789"
    assert c.supervisors == ("ravi@bank.example", "meena@bank.example")
    full = GcpSettings.from_env({**env, "WAPSI_MODEL_ARMOR_TEMPLATE": "projects/x/locations/us/templates/t"})
    assert full.model_armor() == ("x", "us", "t")
    with pytest.raises(ValueError):
        GcpSettings.from_env({**env, "WAPSI_MODEL_ARMOR_TEMPLATE": "a/b"}).model_armor()
    with pytest.raises(ValueError):
        load_settings({"WAPSI_DEMO_MODE": "0"})
    assert GcpSettings.from_env({"WAPSI_PROJECT": "p", "WAPSI_DEMO_MODE": "1"}).demo_mode
    summary = c.public_summary()
    assert summary["ledger"] == "bigquery" and summary["guard"] == "model_armor+rules"
    assert "ravi" not in json.dumps(summary)
    assert summary["gemini_backend"] == "aistudio" and summary["context_cache"] is False


def test_free_trial_defaults_are_off_for_paid_features(monkeypatch):
    key = "AIzaSy-healthz-must-not-leak"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    c = load_settings({"WAPSI_PROJECT": "wapsi-aibc"})
    assert c.gemini_backend == "aistudio" and c.model == "gemini-3.8-flash"
    assert c.private_llm_url is None  # Gemma on Cloud Run GPU: off
    assert c.memory_bank_id is None and c.memory_service_uri() is None  # Memory Bank: off
    assert c.model_armor_template is None and c.model_armor() is None  # Model Armor: optional
    assert c.ledger == "bigquery" and not c.context_cache_enabled() and c.otel_enabled()
    summary = c.public_summary()
    assert summary["gemini_api_key_set"] is True and key not in json.dumps(summary)
    assert summary["private_llm"] is False and summary["memory"] == "in_memory" and summary["guard"] == "rules"
    assert key not in repr(c)

    vertex = load_settings({"WAPSI_PROJECT": "p", "WAPSI_GEMINI_BACKEND": "vertex", "WAPSI_CONTEXT_CACHE": "0",
                            "WAPSI_OTEL_TO_CLOUD": "0", "WAPSI_ADK_STORE": "firestore"})
    assert vertex.gemini_backend == "vertex" and not vertex.context_cache_enabled() and not vertex.otel_enabled()
    assert vertex.public_summary()["gemini_api_key_set"] is None and vertex.memory_kind() == "firestore"
    assert load_settings({"WAPSI_PROJECT": "p", "WAPSI_GEMINI_BACKEND": "vertex"}).context_cache_enabled()
    parquet = load_settings({"WAPSI_PROJECT": "p", "WAPSI_LEDGER": "parquet", "WAPSI_WORLD": "/app/data/world"})
    assert parquet.ledger == "parquet" and parquet.public_summary()["ledger"] == "parquet"
    for bad in ({"WAPSI_GEMINI_BACKEND": "openai"}, {"WAPSI_LEDGER": "csv"}, {"WAPSI_ADK_STORE": "redis"},
                {"WAPSI_LEDGER": "parquet"}):
        with pytest.raises(ValueError):
            load_settings({"WAPSI_PROJECT": "p", **bad})


def test_cloud_services_wiring_is_lazy():
    from wapsi.adapters.bigquery_ledger import BigQueryLedger
    from wapsi.adapters.firestore_store import FirestoreCaseStore
    from wapsi.adapters.gemini_llm import GeminiLLM, PrivateGemmaLLM
    from wapsi.adapters.model_armor_guard import ModelArmorGuard

    settings = GcpSettings(demo_mode=False, project="wapsi-aibc", model_armor_template="wapsi-intake")
    svc = build_services(settings)  # no Gemini API key needed until the first call
    assert isinstance(svc.ledger, BigQueryLedger) and svc.ledger._client is None
    assert isinstance(svc.store, FirestoreCaseStore) and svc.store._client is None
    assert isinstance(svc.llm, GeminiLLM) and svc.llm._client is None
    assert svc.llm.backend == "aistudio" and svc.llm.name == "gemini-api:gemini-3.8-flash"
    vertex = build_services(GcpSettings(demo_mode=False, project="wapsi-aibc", gemini_backend="vertex"))
    assert vertex.llm.backend == "vertex" and vertex.llm.project == "wapsi-aibc"
    assert isinstance(svc.guard, ModelArmorGuard) and svc.guard._client is None
    assert svc.guard.template == "projects/wapsi-aibc/locations/asia-south1/templates/wapsi-intake"

    private = build_services(GcpSettings(demo_mode=False, project="wapsi-aibc",
                                         private_llm_url="https://gemma-x.a.run.app/v1"))
    assert isinstance(private.llm, PrivateGemmaLLM)
    assert private.llm._auth.audience == "https://gemma-x.a.run.app"


def test_parquet_ledger_replaces_bigquery(tmp_path):
    from wapsi.adapters.parquet_ledger import ParquetLedger
    from wapsi.synth.__main__ import main as synth_main

    world = tmp_path / "world"
    assert synth_main(["--size", "showcase", "--seed", "7", "--out", str(world), "--golden-dir", ""]) == 0
    svc = build_services(GcpSettings(demo_mode=False, project="wapsi-aibc", ledger="parquet",
                                     world_path=str(world)))
    assert isinstance(svc.ledger, ParquetLedger)
    with pytest.raises(ValueError, match="BAKE_WORLD"):
        build_services(GcpSettings(demo_mode=False, project="wapsi-aibc", ledger="parquet",
                                   world_path=str(tmp_path / "missing")))
