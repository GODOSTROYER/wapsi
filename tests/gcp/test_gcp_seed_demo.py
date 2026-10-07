"""Judging world (demo seed 7 + showcase), seeded showcase cases and the /demo routes of the judged deployment."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from editions.gcp.app import seed as seed_mod
from editions.gcp.app.config import GcpSettings
from editions.gcp.app.server import create_app
from editions.gcp.app.services import build_services, set_services

CASE1, CASE2 = seed_mod.SHOWCASE_CASE_IDS


@pytest.fixture(scope="module")
def judging_world(tmp_path_factory):
    out = tmp_path_factory.mktemp("judging")
    counts = seed_mod.build_judging_world(out)
    return out, counts


def test_judging_world_reproduces_both_golden_sets(judging_world):
    from evals import harness
    from wapsi.adapters.parquet_ledger import ParquetLedger

    path, counts = judging_world
    ledger = ParquetLedger(path)
    assert counts["banks"] == 3 and counts["complaints"] == 43 + 3
    assert ledger.meta["world"] == "judging" and ledger.home_bank_id() == "HB01"
    for world, limit in (("showcase", None), ("demo", 4)):
        rep = harness.evaluate(world, "fake", limit=limit, ledger=ledger)
        m = rep["metrics"]
        assert m["hold_exact_match_rate"] == 1.0 and m["disposition_accuracy"] == 1.0, (world, rep["failures"][:3])
        assert m["totals_abs_error_inr"] <= 0.05 * rep["cases"] and m["draft_verification_pass_rate"] == 1.0


def test_seed_cases_is_idempotent_and_keeps_the_story_time(demo_services):
    first = seed_mod.seed_cases(demo_services)
    assert first == {CASE1: "seeded", CASE2: "seeded"}
    b = demo_services.store.get_bundle(CASE1)
    assert b is not None and b.drafts and all(demo_services.store.get_bundle(CASE1).verifications.values())
    assert b.case.opened_at.isoformat().startswith("2026-09-10")  # the showcase notice time, not the wall clock
    assert seed_mod.seed_cases(demo_services) == {CASE1: "exists", CASE2: "exists"}
    with pytest.raises(ValueError):
        seed_mod.seed_cases(demo_services, "everything")
    with pytest.raises(ValueError):
        seed_mod.seed_choice("demo")
    assert seed_mod.seed_choice(" Showcase ") == "showcase" and seed_mod.seed_choice(None) == ""


def test_seed_skips_cases_missing_from_the_ledger(demo_services, monkeypatch):
    def missing(ncrp_id):
        raise KeyError(ncrp_id)

    monkeypatch.setattr(demo_services.ledger, "complaint", missing)
    assert set(seed_mod.seed_cases(demo_services).values()) == {"missing_in_ledger"}
    assert demo_services.store.list_cases() == []


def _client(monkeypatch, seed: str | None, **settings_kw):
    if seed is None:
        monkeypatch.delenv(seed_mod.SEED_ENV, raising=False)
    else:
        monkeypatch.setenv(seed_mod.SEED_ENV, seed)
    settings = GcpSettings(serve_adk_web=False, **{"demo_mode": True, **settings_kw})
    services = build_services(GcpSettings(demo_mode=True))
    return TestClient(create_app(settings, services=services))


def test_demo_routes_seed_and_serve_the_showcase_case(monkeypatch):
    try:
        with _client(monkeypatch, "showcase") as c:
            health = c.get("/healthz").json()
            assert health["seed"]["cases"] == "showcase"
            r = c.post("/demo/seed?wait_s=120")
            assert r.status_code == 200 and r.json()["seed"]["state"] == "done", r.text
            assert set(r.json()["cases"]) == {CASE1, CASE2}
            case = c.get(f"/demo/cases/{CASE1}").json()
            assert case["case_id"] == CASE1 and case["drafts_verified"] == len(case["drafts"]) > 0
            assert case["total_hold"].startswith("₹") and "Simulated CFCFRMS" in case["disclaimer"]
            assert case["prose_llm"]
            assert c.get("/demo/cases/CASE-1").status_code == 400
            assert c.get("/demo/cases/CASE-99999999999999").status_code == 404
            assert set(c.get("/demo/status").json()["cases"]) == {CASE1, CASE2}
            assert "role_signing_key" not in json.dumps(health)
    finally:
        set_services(None)


def test_demo_routes_are_off_for_a_private_cloud_deployment(monkeypatch):
    try:
        with _client(monkeypatch, None, demo_mode=False, project="p", otel_to_cloud=False) as c:
            assert c.get("/demo/status").status_code == 404
            assert c.post("/demo/seed").status_code in (404, 405)
            assert c.get("/healthz").json()["seed"] == {"cases": None, "state": "idle", "results": {}, "seconds": None}
    finally:
        set_services(None)


def test_loader_grants_dataset_level_readers(tmp_path):
    """``bigquery_ledger load --reader serviceAccount:...``: least-privilege dataset READER entries, idempotent."""
    from google.cloud import bigquery

    from wapsi.adapters.bigquery_ledger import grant_dataset_readers, load_world, main

    class DS:
        def __init__(self):
            self.access_entries = [bigquery.AccessEntry(role="OWNER", entity_type="userByEmail",
                                                        entity_id="me@example.com")]

    class Client:
        def __init__(self):
            self.ds = DS()
            self.updates = 0
            self.loads = []

        def create_dataset(self, ds, exists_ok=False):
            pass

        def get_dataset(self, dataset_id):
            assert dataset_id == "wapsi-test.wapsi"
            return self.ds

        def update_dataset(self, ds, fields):
            assert fields == ["access_entries"]
            self.updates += 1

        def load_table_from_file(self, fh, table_id, job_config=None):
            self.loads.append(table_id)
            return type("J", (), {"output_rows": 1, "result": lambda self: self})()

    c = Client()
    sa = "serviceAccount:wapsi-api@wapsi-test.iam.gserviceaccount.com"
    assert grant_dataset_readers(c, "wapsi-test.wapsi", [sa]) == ["wapsi-api@wapsi-test.iam.gserviceaccount.com"]
    assert grant_dataset_readers(c, "wapsi-test.wapsi", [sa]) == [] and c.updates == 1
    readers = [e.entity_id for e in c.ds.access_entries if e.role == "READER"]
    assert readers == ["wapsi-api@wapsi-test.iam.gserviceaccount.com"]
    with pytest.raises(ValueError):
        grant_dataset_readers(c, "wapsi-test.wapsi", ["serviceAccount:not-an-email"])
    with pytest.raises(SystemExit):
        main(["load", "--help"])
    from wapsi.synth.__main__ import main as synth

    synth(["--size", "showcase", "--out", str(tmp_path / "w"), "--golden-dir", ""])
    c2 = Client()
    load_world(tmp_path / "w", "wapsi-test", "wapsi", client=c2,
               readers=["serviceAccount:wapsi-ui@wapsi-test.iam.gserviceaccount.com"])
    assert c2.updates == 1 and "wapsi-test.wapsi.transactions" in c2.loads
