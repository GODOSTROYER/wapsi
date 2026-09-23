"""Shared helpers for tests/amd/test_amd_gnn_*.py (no tests here).

Importing this module puts the repository root on ``sys.path`` (``editions`` is a namespace package next to ``src``)
and caps torch CPU threads: the case graphs are tiny and many threads only add contention.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
torch.set_num_threads(2)

from editions.amd.gnn.graph import EDGE_COLUMNS, NODE_COLUMNS, CaseGraph, graph_for_trace  # noqa: E402
from editions.amd.gnn.model import ModelConfig, WapsiGnn  # noqa: E402
from editions.amd.gnn.train import norm_stats  # noqa: E402
from wapsi.fixtures import showcase as sc  # noqa: E402
from wapsi.fixtures.bundles import showcase_bundle  # noqa: E402

# showcase roles (wapsi.fixtures.showcase docstring): the ground truth the fixture was hand-built with
SHOWCASE_ROLES = {
    sc.A: "MULE",
    sc.B: "MULE",
    sc.D: "MULE",
    sc.M: "MULE",
    sc.C: "MULE",
    sc.E: "MULE",
    sc.L: "INNOCENT_HOLDER",
    sc.K: "MERCHANT",
    sc.P: "INTERMEDIARY",
    sc.V1: "VICTIM",
    sc.V2: "VICTIM",
    sc.V3: "VICTIM",
}


def showcase_graphs(ledger=None, labels=SHOWCASE_ROLES) -> tuple[object, list, list[CaseGraph]]:
    ledger = ledger or sc.showcase_ledger()
    traces = [showcase_bundle(w).trace for w in (1, 2)]
    return ledger, traces, [graph_for_trace(ledger, t, labels=labels) for t in traces]


def tiny_model(
    graphs: list[CaseGraph], *, seed: int = 0, temporal: bool = False, hidden: int = 16
) -> WapsiGnn:
    torch.manual_seed(seed)
    m = WapsiGnn(
        ModelConfig(
            node_dim=len(NODE_COLUMNS),
            edge_dim=len(EDGE_COLUMNS),
            hidden=hidden,
            edge_hidden=8,
            layers=2,
            dropout=0.0,
            temporal=temporal,
        )
    )
    s = norm_stats(graphs)
    m.set_normalization(s["x_mean"], s["x_std"], s["e_mean"], s["e_std"])
    return m.eval()


def synthetic_graphs(n: int = 48, seed: int = 0) -> list[CaseGraph]:
    """Tiny synthetic 'world': mules forward fast to other mules; the label is visible in features *and* edges."""
    rng = np.random.default_rng(seed)
    col = {c: j for j, c in enumerate(NODE_COLUMNS)}
    ecol = {c: j for j, c in enumerate(EDGE_COLUMNS)}
    out = []
    for i in range(n):
        k = int(rng.integers(5, 10))
        y = (rng.random(k) < 0.5).astype(np.float32)
        x = rng.normal(0, 1, (k, len(NODE_COLUMNS))).astype(np.float32)
        x[:, col["pass_through_ratio_24h"]] = y * 0.9 + rng.random(k) * 0.1
        x[:, col["g_is_seed"]] = 1.0
        src, dst, feats = [], [], []
        for a in range(k):
            for b in range(k):
                if a != b and rng.random() < 0.3:
                    src.append(a)
                    dst.append(b)
                    e = np.zeros(len(EDGE_COLUMNS), dtype=np.float32)
                    e[ecol["log_amount"]] = 8 + rng.random() * 4
                    e[ecol["dt_prev_credit"]] = 0.2 if y[a] else 4.0
                    e[ecol["ch_UPI"]] = 1.0
                    feats.append(e)
        out.append(
            CaseGraph(
                node_ids=[f"S{i}-{a}" for a in range(k)],
                x=torch.from_numpy(x),
                edge_index=torch.tensor([src, dst], dtype=torch.long)
                if src
                else torch.zeros(2, 0, dtype=torch.long),
                edge_attr=torch.from_numpy(np.stack(feats)) if feats else torch.zeros(0, len(EDGE_COLUMNS)),
                edge_time=torch.zeros(len(src)),
                is_seed=torch.ones(k, dtype=torch.bool),
                y=torch.from_numpy(y),
                meta={"case_id": f"S{i}", "world": "synthetic"},
            )
        )
    return out
