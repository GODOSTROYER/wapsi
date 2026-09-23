import math

import numpy as np
import pandas as pd
import pytest
import test_amd_gnn_kit as kit
import torch

from editions.amd.gnn.graph import (
    EDGE_COLUMNS,
    GRAPH_COLUMNS,
    NODE_COLUMNS,
    GraphConfig,
    build_local_graph,
    build_world_graph,
    collate,
    drop_columns,
    trace_seed_features,
    transform_node_frame,
)
from wapsi.classify.features import FEATURE_COLUMNS
from wapsi.fixtures import showcase as sc

E = {c: j for j, c in enumerate(EDGE_COLUMNS)}
X = {c: j for j, c in enumerate(NODE_COLUMNS)}


@pytest.fixture(scope="module")
def built():
    return kit.showcase_graphs()


def _edge(g, src, dst):
    ids = g.node_ids
    ei = g.edge_index.numpy()
    hits = [k for k in range(ei.shape[1]) if ids[ei[0, k]] == src and ids[ei[1, k]] == dst]
    return [g.edge_attr[k].numpy() for k in hits]


def test_showcase_graph_shapes_and_counts(built):
    _, traces, graphs = built
    for trace, g in zip(traces, graphs, strict=True):
        n_seed = len(trace.accounts)
        assert int(g.is_seed.sum()) == n_seed
        assert g.node_ids[:n_seed] == list(trace_seed_features(kit.sc.showcase_ledger(), trace).index)
        assert g.x.shape == (g.num_nodes, len(NODE_COLUMNS))
        assert g.edge_attr.shape == (g.num_edges, len(EDGE_COLUMNS))
        assert g.edge_index.shape == (2, g.num_edges) and g.edge_index.dtype == torch.long
        assert g.edge_time.shape == (g.num_edges,)
        assert int(g.edge_index.max()) < g.num_nodes
        for t in (g.x, g.edge_attr, g.edge_time):
            assert torch.isfinite(t).all()
    # hand-built showcase: 9 trail/victim accounts + K's 5 past payers (L's tenant paid rent on 10 Aug, outside the
    # 30-day lookback); showcase 2: M and its 2 victims, no context
    assert (graphs[0].num_nodes, graphs[0].num_edges) == (14, 19)
    assert (graphs[1].num_nodes, graphs[1].num_edges) == (3, 2)


def test_seed_rows_are_the_classify_features(built):
    ledger, traces, graphs = built
    feats = trace_seed_features(ledger, traces[0])
    x = graphs[0].x.numpy()[: len(feats)]
    expected = transform_node_frame(feats.reindex(columns=list(NODE_COLUMNS)).fillna(0.0))
    np.testing.assert_allclose(x[:, : len(FEATURE_COLUMNS)], expected[:, : len(FEATURE_COLUMNS)], rtol=1e-6)


def test_edge_features_follow_the_showcase_numbers(built):
    _, _, graphs = built
    g = graphs[0]
    (v1_a,) = _edge(g, sc.V1, sc.A)  # T1 11:02 IMPS 4,80,000 from OB02
    assert v1_a[E["log_amount"]] == pytest.approx(math.log1p(480_000), rel=1e-6)
    assert v1_a[E["ch_IMPS"]] == 1 and v1_a[E["ch_UPI"]] == 0
    assert v1_a[E["is_cross_bank"]] == 1 and v1_a[E["is_round_amount"]] == 1
    assert v1_a[E["t_rel"]] == pytest.approx(0.0) and v1_a[E["after_anchor"]] == 1
    (a_b,) = [e for e in _edge(g, sc.A, sc.B) if e[E["is_device_link"]] == 0]  # T2 11:09, 7 min after T1
    assert a_b[E["dt_prev_credit"]] == pytest.approx(math.log1p(7 / 60), rel=1e-5)
    assert a_b[E["no_prev_credit"]] == 0 and a_b[E["is_cross_bank"]] == 0
    hour = (11 + 9 / 60) / 24 * 2 * math.pi  # IST hour of day
    assert a_b[E["hour_sin"]] == pytest.approx(math.sin(hour), abs=1e-5)
    assert a_b[E["hour_cos"]] == pytest.approx(math.cos(hour), abs=1e-5)
    # A and B share device DEV-7781: device-link edges both ways
    assert any(e[E["is_device_link"]] == 1 for e in _edge(g, sc.A, sc.B))
    assert any(e[E["is_device_link"]] == 1 for e in _edge(g, sc.B, sc.A))
    assert g.x[g.node_ids.index(sc.A), X["g_device_peers"]] > 0


def test_labels_and_masks(built):
    _, _, graphs = built
    g = graphs[0]
    y = dict(zip(g.node_ids, g.y.tolist(), strict=True))
    assert y[sc.A] == 1.0 and y[sc.B] == 1.0
    assert y[sc.L] == 0.0 and y[sc.K] == 0.0 and y[sc.P] == 0.0
    assert y[sc.V1] == -1.0  # victims are masked
    ctx = [a for a, s in zip(g.node_ids, g.is_seed.tolist(), strict=True) if not s]
    assert ctx and all(y[a] == -1.0 for a in ctx)  # not in the showcase role map -> masked


class SpyLedger:
    """Records consortium requests: the builder may only ask CFCFRMS about trail accounts."""

    def __init__(self, inner):
        self.inner = inner
        self.consortium_asked: set[str] = set()

    def consortium_transactions(self, account_ids, start, end):
        ids = list(account_ids)
        self.consortium_asked.update(ids)
        return self.inner.consortium_transactions(ids, start, end)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_visibility_no_consortium_queries_for_context_accounts():
    spy = SpyLedger(sc.showcase_ledger())
    ledger, traces, graphs = kit.showcase_graphs(ledger=spy)
    seeds = {a for t in traces for a in t.accounts}
    assert spy.consortium_asked
    assert spy.consortium_asked <= seeds
    g = graphs[0]
    other_ctx = [i for i, a in enumerate(g.node_ids) if not a.startswith("HB01") and not bool(g.is_seed[i])]
    assert other_ctx
    for i in other_ctx:
        assert float(g.x[i, X["g_features_observed"]]) == 0.0
        assert float(g.x[i, X["is_other_bank"]]) == 1.0
        assert float(g.x[i, X["age_days"]]) == 0.0


def test_neighbor_cap_and_drop_features(built):
    ledger, traces, graphs = built
    feats = trace_seed_features(ledger, traces[0])
    ws, we = traces[0].window_start, traces[0].window_end
    g0 = build_local_graph(ledger, feats, ws, end=we, cfg=GraphConfig(max_neighbors=0, max_device_peers=0))
    assert g0.num_nodes == len(feats)  # no context accounts
    gd = build_local_graph(ledger, feats, ws, end=we, cfg=GraphConfig(drop_features=("mulehunter_score",)))
    assert float(gd.x[:, X["mulehunter_score"]].abs().sum()) == 0.0
    assert float(graphs[0].x[:, X["mulehunter_score"]].abs().sum()) > 0.0
    dropped = drop_columns([graphs[0].__class__.from_dict(graphs[0].to_dict())], ["mulehunter_score"])[0]
    assert torch.equal(dropped.x[:, X["mulehunter_score"]], gd.x[:, X["mulehunter_score"]])


def test_collate_offsets(built):
    _, _, graphs = built
    b = collate(graphs)
    n0 = graphs[0].num_nodes
    assert b.x.shape[0] == sum(g.num_nodes for g in graphs)
    assert b.num_edges == sum(g.num_edges for g in graphs)
    assert torch.equal(b.edge_index[:, graphs[0].num_edges :], graphs[1].edge_index + n0)
    assert b.graph_index.tolist() == [0] * n0 + [1] * graphs[1].num_nodes
    assert b.node_ids == graphs[0].node_ids + graphs[1].node_ids


def test_world_graph_vectorised_on_showcase():
    ledger = sc.showcase_ledger()
    txns = ledger.all_transactions()
    start = min(t.ts for t in txns)
    end = max(t.ts for t in txns)
    wg = build_world_graph(ledger, start, end)
    acct_edges = [t for t in txns if t.from_account and t.to_account and t.from_account != t.to_account]
    assert wg.edge_index.shape == (2, len(acct_edges))
    assert sorted(wg.txn_ids) == sorted(t.txn_id for t in acct_edges)
    assert wg.x.shape == (len(wg.node_ids), len(GRAPH_COLUMNS))
    assert torch.isfinite(wg.x).all() and torch.isfinite(wg.edge_attr).all()
    stats = pd.DataFrame(wg.x.numpy(), index=wg.node_ids, columns=list(GRAPH_COLUMNS))
    assert stats.loc[sc.A, "g_exit_out"] == pytest.approx(math.log1p(2))  # two ATM withdrawals (T4a, T4b)
