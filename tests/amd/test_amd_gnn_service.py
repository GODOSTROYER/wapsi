import json
import math
from datetime import datetime

import pytest
import test_amd_gnn_kit as kit
from fastapi.testclient import TestClient

from editions.amd.gnn.graph import NODE_COLUMNS, trace_seed_features
from editions.amd.gnn.model import save_checkpoint
from editions.amd.gnn.scorer import GnnScorer
from editions.amd.gnn.service import create_app, resolve_checkpoint
from wapsi.classify.features import FEATURE_COLUMNS
from wapsi.fixtures import showcase as sc


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    ledger, traces, graphs = kit.showcase_graphs()
    root = tmp_path_factory.mktemp("svc")
    ck = save_checkpoint(
        kit.tiny_model(graphs, seed=5),
        root / "ckpts" / "m1",
        {"name": "svc-test", "node_columns": list(NODE_COLUMNS)},
    )
    client = TestClient(create_app(GnnScorer.load(ck, ledger=ledger)))
    return ledger, traces, ck, client


def _features_payload(frame):
    """Exactly what editions/amd/app/gnn_client.py sends (orient=index JSON of the classify feature frame)."""
    by_id = json.loads(frame.to_json(orient="index"))
    ids = [str(i) for i in frame.index]
    return {
        "account_ids": ids,
        "feature_columns": list(frame.columns),
        "features": [{"account_id": i, **by_id[i]} for i in ids],
    }


def test_healthz(ctx):
    *_, client = ctx
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["model"] == "gnn:svc-test" and body["parameters"] > 0


def test_score_with_classify_features(ctx):
    ledger, traces, _, client = ctx
    feats = trace_seed_features(ledger, traces[0])
    r = client.post("/score", json=_features_payload(feats))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["scores"]) == set(feats.index)
    assert (
        datetime.fromisoformat(body["anchor"]["as_of"]) == traces[0].window_start
    )  # inferred from complaints
    for acct, p in body["scores"].items():
        assert 0.0 <= p <= 1.0
        row = body["contributions"][acct]
        assert set(row) == {*FEATURE_COLUMNS, "bias"}
        # FEATURE_COLUMNS + bias stay additive in log-odds (graph terms are folded into bias)
        p_clip = min(max(p, 1e-6), 1 - 1e-6)
        assert sum(row.values()) == pytest.approx(math.log(p_clip / (1 - p_clip)), abs=2e-3)
        assert set(body["graph"][acct]) == {
            "graph_neighbourhood",
            "degree_stats",
            "ig_residual",
            "isolated_bias",
        }
    try:  # contract check against the app's client, when that module is present
        from editions.amd.app.gnn_client import _parse
    except ImportError:
        return
    scores, contribs, model = _parse(body)
    assert model == "gnn:svc-test" and set(scores) == set(feats.index) and set(contribs) == set(feats.index)


def test_score_by_ncrp_and_by_accounts(ctx):
    _, traces, _, client = ctx
    r = client.post("/score", json={"ncrp_ids": traces[0].ncrp_ids, "top_k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["scores"]) == set(traces[0].accounts)
    assert all(len(v) == 4 for v in body["contributions"].values())  # top 3 + bias
    r = client.post(
        "/score",
        json={
            "account_ids": [sc.A, sc.B],
            "as_of": traces[0].window_start.isoformat(),
            "contributions": False,
        },
    )
    assert r.status_code == 200, r.text
    assert list(r.json()["scores"]) == [sc.A, sc.B] and "contributions" not in r.json()


def test_bad_requests(ctx):
    *_, client = ctx
    assert client.post("/score", json={}).status_code == 422
    assert client.post("/score", json={"account_ids": ["X" * 200]}).status_code == 422
    r = client.post("/score", json={"account_ids": [sc.A], "features": [{"account_id": sc.B}]})
    assert r.status_code == 422


def test_missing_checkpoint_reports_503(tmp_path):
    client = TestClient(create_app(checkpoint=str(tmp_path / "none"), world=str(tmp_path), device="cpu"))
    assert client.get("/healthz").status_code == 503
    assert client.post("/score", json={"account_ids": ["HB01-1"]}).status_code == 503


def test_resolve_checkpoint_picks_named_or_newest(ctx, tmp_path):
    _, _, ck, _ = ctx
    assert resolve_checkpoint(ck) == ck
    assert resolve_checkpoint(ck.parent) == ck
    assert resolve_checkpoint(ck.parent, "m1") == ck
    with pytest.raises(FileNotFoundError):
        resolve_checkpoint(tmp_path)
