import json

import numpy as np
import pytest
import test_amd_gnn_kit as kit
import torch

from editions.amd.gnn.graph import collate
from editions.amd.gnn.model import (
    CHECKPOINT_FORMAT,
    ModelConfig,
    WapsiGnn,
    load_checkpoint,
    resolve_device,
    save_checkpoint,
    scatter_max,
    scatter_mean,
    transfer_weights,
)
from editions.amd.gnn.train import TrainConfig, build_model, class_weights, node_weights, train_model


def test_scatter_helpers_match_brute_force():
    src = torch.tensor([[1.0, 5.0], [3.0, 2.0], [4.0, 0.5]])
    idx = torch.tensor([0, 0, 2])
    assert torch.equal(scatter_max(src, idx, 4), torch.tensor([[3.0, 5.0], [0, 0], [4.0, 0.5], [0, 0]]))
    assert torch.allclose(scatter_mean(src, idx, 4), torch.tensor([[2.0, 3.5], [0, 0], [4.0, 0.5], [0, 0]]))
    assert torch.equal(scatter_max(src[:0], idx[:0], 3), torch.zeros(3, 2))


def test_forward_backward_on_showcase_graph():
    _, _, graphs = kit.showcase_graphs()
    for temporal in (False, True):
        m = kit.tiny_model(graphs, temporal=temporal).train()
        b = collate(graphs)
        out = m(b.x, b.edge_index, b.edge_attr, b.edge_time)
        assert out.shape == (b.x.shape[0],) and torch.isfinite(out).all()
        out.sum().backward()
        grads = [p.grad for p in m.parameters() if p.requires_grad]
        assert all(g is not None and torch.isfinite(g).all() for g in grads)
        assert sum(float(g.abs().sum()) for g in grads) > 0


def test_direction_matters_and_isolated_nodes_work():
    _, _, graphs = kit.showcase_graphs()
    g = graphs[0]
    m = kit.tiny_model(graphs)
    with torch.no_grad():
        fwd = m(g.x, g.edge_index, g.edge_attr, g.edge_time)
        rev = m(g.x, g.edge_index.flip(0), g.edge_attr, g.edge_time)
        iso = m(g.x, torch.zeros(2, 0, dtype=torch.long), g.edge_attr[:0], g.edge_time[:0])
    assert not torch.allclose(fwd, rev)  # in- and out-neighbourhoods are aggregated separately
    assert torch.isfinite(iso).all()


def test_one_epoch_on_tiny_synthetic_world_improves_loss():
    graphs = kit.synthetic_graphs(48, seed=1)
    train, val = graphs[:40], graphs[40:]
    model = build_model(
        train,
        ModelConfig(
            node_dim=graphs[0].x.shape[1],
            edge_dim=graphs[0].edge_attr.shape[1],
            hidden=16,
            edge_hidden=8,
            dropout=0.0,
        ),
    )
    b = collate(train)
    w_pos, w_neg = class_weights(train, True)
    w = node_weights(b, w_pos, w_neg, 0.3)

    def loss() -> float:
        model.eval()
        with torch.no_grad():
            lg = model(b.x, b.edge_index, b.edge_attr, b.edge_time)
            per = torch.nn.functional.binary_cross_entropy_with_logits(lg, b.y.clamp(min=0), reduction="none")
            return float((per * w).sum() / w.sum())

    before = loss()
    model, history, _ = train_model(
        model,
        train,
        val,
        TrainConfig(epochs=1, batch_size=8, lr=1e-2, seed=0),
        torch.device("cpu"),
        log=lambda _m: None,
    )
    after = loss()
    assert len(history) == 1 and history[0]["edges_per_s"] > 0 and "val_loss" in history[0]
    assert after < before * 0.9, (before, after)


def test_checkpoint_round_trip_identical_scores(tmp_path):
    _, _, graphs = kit.showcase_graphs()
    m = kit.tiny_model(graphs, seed=3, temporal=True)
    out = save_checkpoint(m, tmp_path / "ck", {"name": "t", "node_columns": None})
    assert (out / "model.safetensors").exists()
    cfg = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert cfg["format"] == CHECKPOINT_FORMAT and cfg["model"]["temporal"] is True
    m2, cfg2 = load_checkpoint(out)
    b = collate(graphs)
    with torch.no_grad():
        a = m(b.x, b.edge_index, b.edge_attr, b.edge_time)
        c = m2(b.x, b.edge_index, b.edge_attr, b.edge_time)
    assert torch.equal(a, c)
    assert torch.equal(m.x_std, m2.x_std) and torch.equal(m.e_mean, m2.e_mean)  # normalisation travels along
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "config.json").write_text('{"format": "other"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_checkpoint(tmp_path / "bad")


def test_transfer_weights_only_moves_matching_message_passing_layers():
    src = WapsiGnn(ModelConfig(node_dim=11, edge_dim=20, hidden=16, edge_hidden=8))
    dst = WapsiGnn(ModelConfig(node_dim=44, edge_dim=20, hidden=16, edge_hidden=8))
    moved = transfer_weights(src, dst)
    assert moved and all(k.startswith(("layers.", "edge_in.")) for k in moved)
    assert torch.equal(src.layers[0].msg_in.weight, dst.layers[0].msg_in.weight)
    assert dst.node_in.weight.shape[1] == 44  # node encoder kept (re-initialised), not copied


def test_resolve_device():
    assert resolve_device("cpu").type == "cpu"
    assert resolve_device("auto").type in ("cpu", "cuda")
    if not torch.cuda.is_available():
        with pytest.raises(RuntimeError):
            resolve_device("cuda")


def test_synthetic_kit_is_deterministic():
    a, b = kit.synthetic_graphs(3, seed=5), kit.synthetic_graphs(3, seed=5)
    assert all(np.array_equal(x.x.numpy(), y.x.numpy()) for x, y in zip(a, b, strict=True))
