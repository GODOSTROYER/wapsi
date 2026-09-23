"""Train the Wapsi GNN mule scorer.

    uv run python -m editions.amd.gnn.train \
        --worlds data/generated/demo data/generated/gnn-1001 ... \
        --val-worlds data/generated/gnn-2001 data/generated/gnn-2002 \
        --test-worlds data/generated/heldout-404 data/generated/heldout-505 \
        --device auto --epochs 50 --out editions/amd/gnn/checkpoints/<name>

Protocol (anti-overfitting): weights are fitted on ``--worlds`` only; early stopping and every choice made while
developing use ``--val-worlds`` only; ``--test-worlds`` are scored once, after training, and must be seeds that were
never used for training or tuning. Worlds are generated with different seeds (``python -m wapsi.synth --seed``).

Training data: one case subgraph per case of each world (``graph.world_case_graphs``: trail from ``trace_case`` like
production, features anchored like ``classify_trace``). Labels from ``labels_accounts.role_truth`` (MULE = 1,
INNOCENT_HOLDER / LEGIT / MERCHANT / INTERMEDIARY = 0, VICTIM / UNKNOWN masked). Loss: BCE on trail accounts
(class-balanced with ``--balance``) plus context accounts at ``--context-weight``.

Outputs in ``--out``: ``model.safetensors`` + ``config.json`` (model config, feature columns, normalisation stats in the
weights, graph / classify config, worlds) + ``metrics.json`` (per-epoch loss, val AUC / AP, epoch time, edges/s, peak
memory; final test metrics).

``--amlworld <HI-Small_Trans.csv>`` (EVENT-TIME ONLY): edge-level pre-training on IBM AMLworld laundering labels,
then the message-passing layers are transferred and fine-tuned on Wapsi node labels. See ``amlworld.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from editions.amd.gnn.graph import (
    EDGE_COLUMNS,
    GRAPH_VERSION,
    NODE_COLUMNS,
    CaseGraph,
    GraphBatch,
    GraphConfig,
    cached_world_case_graphs,
    collate,
)
from editions.amd.gnn.model import (
    ModelConfig,
    WapsiGnn,
    configure_cpu_threads,
    count_parameters,
    device_info,
    load_checkpoint,
    resolve_device,
    save_checkpoint,
    transfer_weights,
)
from wapsi.config import ClassifyConfig

DEFAULT_CACHE = Path("data/generated/gnn-cache")


@dataclass
class TrainConfig:
    epochs: int = 50
    batch_size: int = 32  # case subgraphs per step
    lr: float = 3e-3
    weight_decay: float = 1e-4
    patience: int = 10
    context_weight: float = 0.3
    balance: bool = True
    grad_clip: float = 1.0
    seed: int = 7


# --------------------------------------------------------------------------- data


def load_world_graphs(
    worlds: Sequence[str | Path],
    gcfg: GraphConfig,
    ccfg: ClassifyConfig,
    *,
    cache_dir: str | Path | None = DEFAULT_CACHE,
    workers: int = 1,
    refresh: bool = False,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, list[CaseGraph]]:
    worlds = [Path(w) for w in worlds]
    for w in worlds:
        if not (w / "transactions.parquet").exists():
            raise SystemExit(
                f"world {w} not found. Generate it: uv run python -m wapsi.synth --size demo --seed <s> "
                f"--name {w.name} --out {w} --golden-dir ''"
            )
    t0 = time.perf_counter()
    if workers > 1 and len(worlds) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(worlds))) as ex:
            futs = [
                ex.submit(cached_world_case_graphs, w, cache_dir, gcfg, ccfg, refresh=refresh, limit=limit)
                for w in worlds
            ]
            out = {w.name: f.result() for w, f in zip(worlds, futs, strict=True)}
    else:
        out = {
            w.name: cached_world_case_graphs(w, cache_dir, gcfg, ccfg, refresh=refresh, limit=limit)
            for w in worlds
        }
    n_nodes = sum(g.num_nodes for gs in out.values() for g in gs)
    n_edges = sum(g.num_edges for gs in out.values() for g in gs)
    log(
        f"graphs: {', '.join(f'{k}={len(v)}' for k, v in out.items())} | {n_nodes} nodes, {n_edges} edges "
        f"| {time.perf_counter() - t0:.1f}s"
    )
    return out


def norm_stats(graphs: Sequence[CaseGraph]) -> dict[str, torch.Tensor]:
    x = torch.cat([g.x for g in graphs])
    e = (
        torch.cat([g.edge_attr for g in graphs])
        if any(g.num_edges for g in graphs)
        else torch.zeros(1, len(EDGE_COLUMNS))
    )
    return {
        "x_mean": x.mean(0),
        "x_std": x.std(0, unbiased=False),
        "e_mean": e.mean(0),
        "e_std": e.std(0, unbiased=False),
    }


def class_weights(graphs: Sequence[CaseGraph], balance: bool) -> tuple[float, float]:
    y = torch.cat([g.y[g.is_seed] for g in graphs]) if graphs else torch.zeros(0)
    n_pos, n_neg = float((y == 1).sum()), float((y == 0).sum())
    if not balance or n_pos == 0 or n_neg == 0:
        return 1.0, 1.0
    n = n_pos + n_neg
    return n / (2 * n_pos), n / (2 * n_neg)


def node_weights(b: GraphBatch, w_pos: float, w_neg: float, context_weight: float) -> torch.Tensor:
    seed_w = torch.where(b.y > 0.5, torch.full_like(b.y, w_pos), torch.full_like(b.y, w_neg))
    w = torch.where(b.is_seed, seed_w, torch.full_like(b.y, context_weight))
    return torch.where(b.y >= 0, w, torch.zeros_like(w))


def _loss(logits: torch.Tensor, b: GraphBatch, w: torch.Tensor) -> torch.Tensor:
    per = torch.nn.functional.binary_cross_entropy_with_logits(logits, b.y.clamp(min=0.0), reduction="none")
    return (per * w).sum() / w.sum().clamp(min=1e-9)


# --------------------------------------------------------------------------- metrics


def _peak_memory_mb(device: torch.device) -> float | None:
    if device.type == "cuda":
        return round(torch.cuda.max_memory_allocated(device) / 2**20, 1)
    try:
        import psutil

        mi = psutil.Process(os.getpid()).memory_info()
        return round(float(getattr(mi, "peak_wset", 0) or mi.rss) / 2**20, 1)
    except Exception:  # pragma: no cover - psutil optional
        return None


@torch.no_grad()
def predict(
    model: WapsiGnn, graphs: Sequence[CaseGraph], device: torch.device, batch_size: int = 256
) -> pd.DataFrame:
    """One row per node: world, case_id, account_id, is_seed, y, logit, score."""
    model.eval()
    rows = []
    for i in range(0, len(graphs), batch_size):
        chunk = graphs[i : i + batch_size]
        b = collate(chunk).to(device)
        logit = model(b.x, b.edge_index, b.edge_attr, b.edge_time).float().cpu().numpy()
        gi = b.graph_index.cpu().numpy()
        rows.append(
            pd.DataFrame(
                {
                    "world": [chunk[j].meta.get("world", "") for j in gi],
                    "case_id": [chunk[j].meta.get("case_id", "") for j in gi],
                    "account_id": b.node_ids,
                    "is_seed": b.is_seed.cpu().numpy(),
                    "y": b.y.cpu().numpy(),
                    "logit": logit,
                    "score": 1.0 / (1.0 + np.exp(-logit)),
                }
            )
        )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def node_metrics(pred: pd.DataFrame, mule_threshold: float = 0.6) -> dict:
    """Mule metrics on labelled trail accounts (seeds) and on context accounts."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    out: dict = {}
    for name, sub in (
        ("seed", pred[pred["is_seed"] & (pred["y"] >= 0)]),
        ("context", pred[~pred["is_seed"] & (pred["y"] >= 0)]),
    ):
        y = sub["y"].to_numpy() > 0.5
        s = sub["score"].to_numpy()
        m: dict = {"n": len(sub), "n_mule": int(y.sum())}
        if 0 < y.sum() < len(y):
            m["auc"] = round(float(roc_auc_score(y, s)), 4)
            m["ap"] = round(float(average_precision_score(y, s)), 4)
        pred_pos = s >= mule_threshold
        tp = int((pred_pos & y).sum())
        p = tp / max(int(pred_pos.sum()), 1)
        r = tp / max(int(y.sum()), 1)
        m.update(
            {
                "mule_precision": round(p, 4),
                "mule_recall": round(r, 4),
                "mule_f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            }
        )
        out[name] = m
    return out


# --------------------------------------------------------------------------- training


def build_model(train_graphs: Sequence[CaseGraph], mcfg: ModelConfig) -> WapsiGnn:
    model = WapsiGnn(mcfg)
    s = norm_stats(train_graphs)
    model.set_normalization(s["x_mean"], s["x_std"], s["e_mean"], s["e_std"])
    return model


def train_model(
    model: WapsiGnn,
    train_graphs: Sequence[CaseGraph],
    val_graphs: Sequence[CaseGraph],
    tcfg: TrainConfig,
    device: torch.device,
    log: Callable[[str], None] = print,
) -> tuple[WapsiGnn, list[dict], int]:
    """Mini-batch training over case subgraphs; early stopping on the validation seed loss. Returns the best model."""
    torch.manual_seed(tcfg.seed)
    rng = np.random.default_rng(tcfg.seed)
    model.to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=tcfg.lr, weight_decay=tcfg.weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(tcfg.epochs, 1), eta_min=tcfg.lr * 0.05)
    w_pos, w_neg = class_weights(train_graphs, tcfg.balance)
    val_batch = collate(list(val_graphs)).to(device) if val_graphs else None
    val_w = node_weights(val_batch, w_pos, w_neg, 0.0) if val_batch is not None else None
    history: list[dict] = []
    best_loss, best_epoch, best_state, bad = float("inf"), 0, None, 0
    for epoch in range(1, tcfg.epochs + 1):
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(device)
            torch.cuda.synchronize(device)
        model.train()
        t0 = time.perf_counter()
        perm = rng.permutation(len(train_graphs))
        total, count, edges, nodes = 0.0, 0.0, 0, 0
        for i in range(0, len(perm), tcfg.batch_size):
            b = collate([train_graphs[j] for j in perm[i : i + tcfg.batch_size]]).to(device)
            w = node_weights(b, w_pos, w_neg, tcfg.context_weight)
            if float(w.sum()) <= 0:
                continue
            logits = model(b.x, b.edge_index, b.edge_attr, b.edge_time)
            loss = _loss(logits, b, w)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            if tcfg.grad_clip:
                torch.nn.utils.clip_grad_norm_(model.parameters(), tcfg.grad_clip)
            opt.step()
            total += float(loss.detach()) * float(w.sum())
            count += float(w.sum())
            edges += b.num_edges
            nodes += b.x.shape[0]
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        train_s = time.perf_counter() - t0
        sched.step()
        rec = {
            "epoch": epoch,
            "train_loss": round(total / max(count, 1e-9), 5),
            "epoch_s": round(train_s, 3),
            "edges_per_s": round(edges / max(train_s, 1e-9), 1),
            "nodes_per_s": round(nodes / max(train_s, 1e-9), 1),
            "peak_mem_mb": _peak_memory_mb(device),
            "lr": round(sched.get_last_lr()[0], 6),
        }
        if val_batch is not None:
            model.eval()
            with torch.no_grad():
                vlogit = model(val_batch.x, val_batch.edge_index, val_batch.edge_attr, val_batch.edge_time)
                vloss = float(_loss(vlogit, val_batch, val_w))
            rec["val_loss"] = round(vloss, 5)
            vm = node_metrics(predict(model, val_graphs, device))["seed"]
            rec["val_auc"], rec["val_ap"] = vm.get("auc"), vm.get("ap")
            if vloss < best_loss - 1e-5:
                best_loss, best_epoch, bad = vloss, epoch, 0
                best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            else:
                bad += 1
        history.append(rec)
        log(
            f"epoch {epoch:3d} train {rec['train_loss']:.4f} val {rec.get('val_loss', float('nan')):.4f} "
            f"AUC {rec.get('val_auc')} AP {rec.get('val_ap')} | {rec['epoch_s']:.2f}s {rec['edges_per_s']:.0f} edges/s "
            f"peak {rec['peak_mem_mb']} MB"
        )
        if val_batch is not None and bad >= tcfg.patience:
            log(f"early stop at epoch {epoch} (best {best_epoch})")
            break
    if best_state is not None:
        model.load_state_dict(best_state)
    else:
        best_epoch = len(history)
    model.eval()
    return model, history, best_epoch


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m editions.amd.gnn.train",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--worlds", nargs="+", required=True, help="training world directories")
    ap.add_argument("--val-worlds", nargs="+", required=True, help="validation worlds (early stopping only)")
    ap.add_argument(
        "--test-worlds", nargs="*", default=[], help="held-out worlds, scored once after training"
    )
    ap.add_argument("--out", required=True, help="checkpoint directory")
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda (ROCm uses the cuda API)")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--patience", type=int, default=10)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--edge-hidden", type=int, default=32)
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--temporal", action="store_true", help="Time2Vec edge-time encoding variant")
    ap.add_argument("--context-weight", type=float, default=0.3)
    ap.add_argument("--no-balance", action="store_true", help="do not class-balance trail-account losses")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--lookback-days", type=float, default=GraphConfig.lookback_days)
    ap.add_argument("--max-neighbors", type=int, default=GraphConfig.max_neighbors)
    ap.add_argument("--hops", type=int, default=GraphConfig.hops)
    ap.add_argument(
        "--drop-features", nargs="*", default=[], help="node features zeroed for every node (ablation)"
    )
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="graph cache ('' disables)")
    ap.add_argument("--refresh-cache", action="store_true")
    ap.add_argument("--workers", type=int, default=1, help="processes for graph building (one per world)")
    ap.add_argument("--limit-cases", type=int, default=None, help="cases per world (smoke runs)")
    ap.add_argument("--threads", type=int, default=None, help="torch CPU threads")
    ap.add_argument(
        "--init-from", default=None, help="checkpoint to fine-tune from (transferable layers only)"
    )
    ap.add_argument(
        "--amlworld", default=None, help="EVENT-TIME: AMLworld HI-Small_Trans.csv for edge pre-training"
    )
    ap.add_argument("--amlworld-max-rows", type=int, default=None)
    ap.add_argument("--amlworld-epochs", type=int, default=20)
    args = ap.parse_args(argv)

    overlap = {Path(w).resolve() for w in args.test_worlds} & {
        Path(w).resolve() for w in [*args.worlds, *args.val_worlds]
    }
    if overlap:
        print(
            f"error: test worlds also used for training/validation: {sorted(map(str, overlap))}",
            file=sys.stderr,
        )
        return 2
    device = resolve_device(args.device)
    configure_cpu_threads(device, args.threads)
    dinfo = device_info(device)
    print(f"device: {dinfo}")
    unknown = sorted(set(args.drop_features) - set(NODE_COLUMNS))
    if unknown:
        print(f"error: unknown --drop-features {unknown}", file=sys.stderr)
        return 2
    gcfg = GraphConfig(
        lookback_days=args.lookback_days,
        max_neighbors=args.max_neighbors,
        hops=args.hops,
        drop_features=tuple(args.drop_features),
    )
    ccfg = ClassifyConfig()
    tcfg = TrainConfig(
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        weight_decay=args.weight_decay,
        patience=args.patience,
        context_weight=args.context_weight,
        balance=not args.no_balance,
        seed=args.seed,
    )
    cache = args.cache_dir or None
    kw = dict(cache_dir=cache, workers=args.workers, refresh=args.refresh_cache, limit=args.limit_cases)
    t_data = time.perf_counter()
    train_by = load_world_graphs(args.worlds, gcfg, ccfg, **kw)
    val_by = load_world_graphs(args.val_worlds, gcfg, ccfg, **kw)
    test_by = load_world_graphs(args.test_worlds, gcfg, ccfg, **kw) if args.test_worlds else {}
    data_s = time.perf_counter() - t_data
    train = [g for gs in train_by.values() for g in gs]
    val = [g for gs in val_by.values() for g in gs]

    mcfg = ModelConfig(
        node_dim=len(NODE_COLUMNS),
        edge_dim=len(EDGE_COLUMNS),
        hidden=args.hidden,
        edge_hidden=args.edge_hidden,
        layers=args.layers,
        dropout=args.dropout,
        temporal=args.temporal,
    )
    model = build_model(train, mcfg)
    pretrain_summary = None
    if args.amlworld:
        from editions.amd.gnn.amlworld import amlworld_graph, load_amlworld_csv, pretrain_edges

        print(f"[amlworld] EVENT-TIME pre-training on {args.amlworld}")
        g = amlworld_graph(load_amlworld_csv(args.amlworld, max_rows=args.amlworld_max_rows))
        pre, pretrain_summary = pretrain_edges(
            g,
            device=device,
            epochs=args.amlworld_epochs,
            hidden=args.hidden,
            edge_hidden=args.edge_hidden,
            layers=args.layers,
            dropout=args.dropout,
            temporal=args.temporal,
            seed=args.seed,
        )
        pretrain_summary["transferred"] = transfer_weights(pre, model)
    if args.init_from:
        src, _ = load_checkpoint(args.init_from)
        print(f"init from {args.init_from}: {len(transfer_weights(src, model))} tensors transferred")

    print(f"model: {count_parameters(model)} parameters, {mcfg}")
    t_train = time.perf_counter()
    model, history, best_epoch = train_model(model, train, val, tcfg, device)
    train_s = time.perf_counter() - t_train

    test_metrics = {}
    for name, gs in test_by.items():
        test_metrics[name] = node_metrics(predict(model, gs, device), ccfg.mule_threshold)
    if test_by:
        test_metrics["pooled"] = node_metrics(
            predict(model, [g for gs in test_by.values() for g in gs], device), ccfg.mule_threshold
        )
    val_metrics = node_metrics(predict(model, val, device), ccfg.mule_threshold) if val else {}

    out = Path(args.out)
    created = datetime.now(UTC).isoformat()
    extra = {
        "name": out.name,
        "created_at": created,
        "graph_version": GRAPH_VERSION,
        "node_columns": list(NODE_COLUMNS),
        "edge_columns": list(EDGE_COLUMNS),
        "graph_config": asdict(gcfg),
        "classify_config": ccfg.model_dump(),
        "train_config": asdict(tcfg),
        "label_policy": {
            "MULE": 1,
            "INNOCENT_HOLDER": 0,
            "LEGIT": 0,
            "MERCHANT": 0,
            "INTERMEDIARY": 0,
            "VICTIM": "masked",
            "UNKNOWN": "masked",
        },
        "worlds": {
            "train": [str(w) for w in args.worlds],
            "val": [str(w) for w in args.val_worlds],
            "test": [str(w) for w in args.test_worlds],
        },
        "device": dinfo,
        "amlworld": bool(args.amlworld),
        "drop_features": list(gcfg.drop_features),
    }
    save_checkpoint(model, out, extra)
    metrics = {
        "name": out.name,
        "created_at": created,
        "label": "CPU dev run"
        if device.type == "cpu"
        else f"{dinfo.get('backend')} run on {dinfo.get('name')}",
        "device": dinfo,
        "parameters": count_parameters(model),
        "data": {
            "graph_build_s": round(data_s, 1),
            "train_cases": len(train),
            "val_cases": len(val),
            "train_nodes": sum(g.num_nodes for g in train),
            "train_edges": sum(g.num_edges for g in train),
            "per_world_cases": {k: len(v) for k, v in {**train_by, **val_by, **test_by}.items()},
        },
        "train_s": round(train_s, 1),
        "best_epoch": best_epoch,
        "history": history,
        "val": val_metrics,
        "test": test_metrics,
        "amlworld_pretrain": pretrain_summary,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"best_epoch": best_epoch, "val": val_metrics, "test": test_metrics}, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
