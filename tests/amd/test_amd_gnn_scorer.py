import numpy as np
import pandas as pd
import pytest
import test_amd_gnn_kit as kit
import torch

from editions.amd.gnn.graph import NODE_COLUMNS, anchor_from_trace, graph_for_trace, trace_seed_features
from editions.amd.gnn.model import save_checkpoint
from editions.amd.gnn.scorer import CONTRIBUTION_COLUMNS, GRAPH_CONTEXT, IG_RESIDUAL, GnnScorer
from wapsi.classify import Scorer, classify_trace
from wapsi.classify.scoring import BIAS
from wapsi.config import ClassifyConfig
from wapsi.fixtures import showcase as sc
from wapsi.models import Role


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    ledger, traces, graphs = kit.showcase_graphs()
    model = kit.tiny_model(graphs, seed=11)
    # a few steps so scores are not all ~0.5 (mechanics only; not a quality claim)
    opt = torch.optim.Adam(model.parameters(), lr=0.01)
    model.train()
    for _ in range(15):
        for g in graphs:
            mask = g.y >= 0
            logit = model(g.x, g.edge_index, g.edge_attr, g.edge_time)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(logit[mask], g.y[mask])
            opt.zero_grad()
            loss.backward()
            opt.step()
    model.eval()
    ck = save_checkpoint(
        model,
        tmp_path_factory.mktemp("gnn") / "showcase",
        {
            "name": "showcase-test",
            "node_columns": list(NODE_COLUMNS),
        },
    )
    return ledger, traces, ck


def test_protocol(setup):
    ledger, _, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    assert isinstance(scorer, Scorer)
    assert scorer.name == "gnn:showcase-test"


def test_classify_trace_with_gnn_scorer(setup):
    ledger, traces, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    for trace in traces:
        res = classify_trace(ledger, trace, ClassifyConfig(), model=scorer)
        assert set(res) == set(trace.accounts)
        for r in res.values():
            assert 0.0 <= r.score <= 1.0 and r.reasons
            assert r.model.endswith("gnn:showcase-test")
        for v in (sc.V1, sc.V2, sc.V3):
            if v in res:
                assert res[v].role == Role.VICTIM
    res = classify_trace(ledger, traces[0], ClassifyConfig(), model=scorer)
    assert res[sc.K].role == Role.MERCHANT and res[sc.P].role == Role.INTERMEDIARY  # hard rules still first
    assert res[sc.A].score > 0.6 and res[sc.A].role == Role.MULE


def test_complaint_anchor_matches_trace_and_graph_is_identical(setup):
    ledger, traces, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    for trace in traces:
        a = anchor_from_trace(trace)
        feats = trace_seed_features(ledger, trace)
        inferred = scorer._anchor_from_complaints(set(feats.index))
        assert inferred.as_of == a.as_of and inferred.end == a.end and inferred.trail == a.trail
        scorer.score(feats)
        g_scorer = scorer.last_graph()
        g_train = graph_for_trace(ledger, trace)  # what the trainer builds for this case
        assert g_scorer.node_ids == g_train.node_ids
        assert torch.equal(g_scorer.x, g_train.x) and torch.equal(g_scorer.edge_index, g_train.edge_index)
        bound = GnnScorer.for_trace(ck, ledger, trace).score(feats)
        pd.testing.assert_series_equal(bound, scorer.score(feats))


def test_contributions_are_additive_log_odds(setup):
    ledger, traces, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger, ig_steps=64)
    feats = trace_seed_features(ledger, traces[0])
    scores = scorer.score(feats)
    contrib = scorer.contributions(feats)
    assert list(contrib.columns) == list(CONTRIBUTION_COLUMNS) and list(contrib.index) == list(feats.index)
    logit = scorer.logits(feats)
    np.testing.assert_allclose(scores.to_numpy(), 1 / (1 + np.exp(-logit.to_numpy())))
    np.testing.assert_allclose(contrib.sum(axis=1).to_numpy(), logit.to_numpy(), atol=1e-4)
    feat_part = contrib.drop(columns=[GRAPH_CONTEXT, IG_RESIDUAL, BIAS]).sum(axis=1)
    assert (contrib[IG_RESIDUAL].abs() <= 0.05 * (1 + feat_part.abs())).all()  # IG completeness holds
    assert contrib[BIAS].nunique() == 1  # isolated mean-feature account: one number for all rows
    assert contrib[GRAPH_CONTEXT].abs().sum() > 0  # the neighbourhood moves the logit


def test_without_ledger_falls_back_to_isolated_nodes(setup):
    ledger, traces, ck = setup
    feats = trace_seed_features(ledger, traces[0])
    scorer = GnnScorer.load(ck, ledger=None)
    s = scorer.score(feats)
    assert s.between(0, 1).all() and scorer.warnings
    assert scorer.last_graph().num_edges == 0


def test_score_accounts_service_paths(setup):
    ledger, traces, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    trace = traces[0]
    s1, c1, f1, a1 = scorer.score_accounts(ncrp_ids=trace.ncrp_ids)
    assert set(s1.index) == set(trace.accounts) and a1.as_of == anchor_from_trace(trace).as_of
    assert c1 is not None and list(c1.index) == list(s1.index)
    ref = scorer.score(trace_seed_features(ledger, trace))
    pd.testing.assert_series_equal(s1.sort_index(), ref.sort_index(), check_names=False)
    s2, c2, _, a2 = scorer.score_accounts([sc.A, sc.B], with_contributions=False)
    assert list(s2.index) == [sc.A, sc.B] and c2 is None and a2 is not None
    s3, _, _, a3 = scorer.score_accounts([sc.A], as_of=trace.window_start)
    assert a3.as_of == trace.window_start and 0 <= float(s3.iloc[0]) <= 1


def test_checkpoint_feature_drift_is_rejected(setup):
    ledger, _, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    with pytest.raises(ValueError):
        GnnScorer(scorer.model, {**scorer.config, "node_columns": ["x"]}, ledger)


def test_classify_trace_hook_binds_a_copy_to_the_trace(setup, monkeypatch):
    ledger, traces, ck = setup
    scorer = GnnScorer.load(ck, ledger=ledger)
    trace = traces[0]
    feats = trace_seed_features(ledger, trace)
    bound = scorer.bind_trace(ledger, trace)
    assert bound is not scorer and bound.model is scorer.model and bound.ledger is ledger
    assert bound._bound == anchor_from_trace(trace)
    assert scorer._bound is None  # the shared scorer is untouched
    pd.testing.assert_series_equal(bound.score(feats), GnnScorer.for_trace(ck, ledger, trace).score(feats))
    assert scorer.last_graph() is None
    inferred = scorer.score(feats).to_dict()  # unbound: the window is inferred from the complaints

    def no_inference(self, idset):
        raise AssertionError("classify_trace should hand the scorer its trace, not make it infer the window")

    monkeypatch.setattr(GnnScorer, "_anchor_from_complaints", no_inference)
    res = classify_trace(ledger, trace, ClassifyConfig(), model=scorer)
    assert {a: r.score for a, r in res.items()} == pytest.approx(inferred, abs=1e-4)
