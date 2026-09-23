"""AMLworld loader against the documented CSV layout, on a tiny synthetic file (no download, no real data)."""

import numpy as np
import pytest
import test_amd_gnn_kit  # noqa: F401  (sys.path + threads)
import torch

from editions.amd.gnn.amlworld import AML_COLUMNS, amlworld_graph, load_amlworld_csv, pretrain_edges
from editions.amd.gnn.graph import EDGE_COLUMNS, GRAPH_COLUMNS, NODE_COLUMNS
from editions.amd.gnn.model import ModelConfig, WapsiGnn, transfer_weights

# the raw file repeats "Account"; pandas renames the second one to "Account.1"
RAW_HEADER = (
    "Timestamp,From Bank,Account,To Bank,Account,Amount Received,Receiving Currency,Amount Paid,"
    "Payment Currency,Payment Format,Is Laundering"
)
FORMATS = ["ACH", "Wire", "Cheque", "Credit Card", "Cash", "Bitcoin", "Reinvestment", "Mystery"]


def _write_csv(path, n=240, seed=0):
    rng = np.random.default_rng(seed)
    accts = [f"80{i:05X}0" for i in range(40)]
    lines = [RAW_HEADER]
    for i in range(n):
        a, b = rng.choice(len(accts), 2, replace=False)
        amt = round(float(rng.lognormal(7, 1.5)), 2)
        launder = int(i % 17 == 0)
        fmt = FORMATS[i % len(FORMATS)]
        day, minute = 1 + i // 60, i % 60
        lines.append(
            f"2022/09/{day:02d} 10:{minute:02d},{10 + a % 3},{accts[a]},{10 + b % 3},{accts[b]},"
            f"{amt},US Dollar,{amt},US Dollar,{fmt},{launder}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_documented_columns(tmp_path):
    df = load_amlworld_csv(_write_csv(tmp_path / "HI-Small_Trans.csv"))
    assert len(df) == 240 and int(df["label"].sum()) == 15
    assert df["ts"].is_monotonic_increasing
    assert df["src"].str.contains(":").all() and df["dst"].str.contains(":").all()
    assert set(df["channel"]) <= {"NEFT", "RTGS", "OTHER", "POS", "CASH_DEPOSIT", "CRYPTO", "INTERNAL"}
    assert (df.loc[df.index[:5], "amount"] > 0).all()
    assert load_amlworld_csv(tmp_path / "HI-Small_Trans.csv", max_rows=10).shape[0] == 10
    assert "Account.1" in AML_COLUMNS


def test_rejects_other_files(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_amlworld_csv(p)


def test_pretrain_and_transfer(tmp_path):
    g = amlworld_graph(load_amlworld_csv(_write_csv(tmp_path / "t.csv")))
    assert g.x.shape == (len(g.node_ids), len(GRAPH_COLUMNS))
    assert g.edge_attr.shape == (g.edge_index.shape[1], len(EDGE_COLUMNS))
    assert set(g.split.unique().tolist()) == {0, 1, 2}
    pre, summary = pretrain_edges(
        g, device=torch.device("cpu"), epochs=2, hidden=16, edge_hidden=8, log=lambda _m: None
    )
    assert len(summary["history"]) == 2 and summary["edges"] == g.edge_index.shape[1]
    node_model = WapsiGnn(
        ModelConfig(node_dim=len(NODE_COLUMNS), edge_dim=len(EDGE_COLUMNS), hidden=16, edge_hidden=8)
    )
    moved = transfer_weights(pre, node_model)
    assert any(k.startswith("layers.") for k in moved) and any(k.startswith("edge_in.") for k in moved)
