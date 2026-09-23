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
    assert s.demo_mode and s.location == "global" and s.region == "asia-south1" and s.model == "gemini-3.5-flash"
    assert s.memory_service_uri() is None and s.model_armor() is None

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


def test_cloud_services_wiring_is_lazy():
    from wapsi.adapters.bigquery_ledger import BigQueryLedger
    from wapsi.adapters.firestore_store import FirestoreCaseStore
    from wapsi.adapters.gemini_llm import GeminiLLM, PrivateGemmaLLM
    from wapsi.adapters.model_armor_guard import ModelArmorGuard

    settings = GcpSettings(demo_mode=False, project="wapsi-aibc", model_armor_template="wapsi-intake")
    svc = build_services(settings)
    assert isinstance(svc.ledger, BigQueryLedger) and svc.ledger._client is None
    assert isinstance(svc.store, FirestoreCaseStore) and svc.store._client is None
    assert isinstance(svc.llm, GeminiLLM) and svc.llm._client is None
    assert isinstance(svc.guard, ModelArmorGuard) and svc.guard._client is None
    assert svc.guard.template == "projects/wapsi-aibc/locations/asia-south1/templates/wapsi-intake"

    private = build_services(GcpSettings(demo_mode=False, project="wapsi-aibc",
                                         private_llm_url="https://gemma-x.a.run.app/v1"))
    assert isinstance(private.llm, PrivateGemmaLLM)
    assert private.llm._auth.audience == "https://gemma-x.a.run.app"
