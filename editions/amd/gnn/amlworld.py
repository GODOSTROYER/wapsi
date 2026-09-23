"""EVENT-TIME ONLY: IBM AMLworld (HI-Small) loader and edge-level pre-training.

Dataset: "IBM Transactions for Anti Money Laundering (AML)" on Kaggle (ealtman2019), data licence CDLA-Sharing-1.0
(https://github.com/IBM/AML-Data). The user downloads ``HI-Small_Trans.csv`` at event time (12-18 Oct 2026); nothing
here downloads anything, and no AMLworld data is committed. Everything in it is synthetic (multi-agent simulation).

Documented CSV columns (the raw header repeats ``Account``; pandas names the second one ``Account.1``):

    Timestamp, From Bank, Account, To Bank, Account.1, Amount Received, Receiving Currency, Amount Paid,
    Payment Currency, Payment Format, Is Laundering

Timestamps look like ``2022/09/01 00:20``; banks are numeric ids, accounts hex-like strings; Payment Format values
include ACH, Bitcoin, Cash, Cheque, Credit Card, Reinvestment, Wire (unknown values map to ``OTHER``).

Pre-training task: edge classification (``Is Laundering``) with the same ``EDGE_COLUMNS`` edge features and the same
``EdgeSageLayer`` stack as the Wapsi node model; node inputs are the ``GRAPH_COLUMNS`` degree statistics (AMLworld has
no KYC / device / signal data). Split: temporal 60 / 20 / 20 by timestamp (train edges only are visible to message
passing during training; validation adds the validation-period edges). Transfer: ``model.transfer_weights`` copies
the message-passing layers and the edge encoder into the Wapsi node model; its node input encoder is re-initialised
and everything is fine-tuned on Wapsi node labels (``train.py --amlworld``).

Amounts: log1p of ``Amount Received`` in the receiving currency (no FX conversion; the currency pair is ignored).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from editions.amd.gnn.graph import EDGE_COLUMNS, GRAPH_COLUMNS, LOG_COLUMNS, degree_stats, edge_features
from editions.amd.gnn.model import EdgeHead, ModelConfig, WapsiGnn

AML_COLUMNS: tuple[str, ...] = (
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account.1",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
)
FORMAT_TO_CHANNEL = {
    "ACH": "NEFT",
    "Wire": "RTGS",
    "Credit Card": "POS",
    "Bitcoin": "CRYPTO",
    "Cash": "CASH_DEPOSIT",
    "Reinvestment": "INTERNAL",
    "Cheque": "OTHER",
}
TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M"


def load_amlworld_csv(path: str | Path, max_rows: int | None = None) -> pd.DataFrame:
    """Read an AMLworld ``*_Trans.csv`` into the Wapsi transaction frame layout (``graph.TX_COLUMNS`` + ``label``)."""
    raw = pd.read_csv(
        path,
        nrows=max_rows,
        dtype={"From Bank": str, "To Bank": str, "Account": str, "Account.1": str, "Payment Format": str},
    )
    missing = [c for c in AML_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: not an AMLworld transactions file, missing columns {missing}")
    ts = pd.to_datetime(raw["Timestamp"], format=TIMESTAMP_FORMAT, utc=True)
    src = raw["From Bank"].str.strip() + ":" + raw["Account"].str.strip()
    dst = raw["To Bank"].str.strip() + ":" + raw["Account.1"].str.strip()
    df = pd.DataFrame(
        {
            "txn_id": pd.Series([f"AML{i:09d}" for i in range(len(raw))], dtype=object),
            "ts": (ts - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy(dtype=float),
            "src": src.astype(object),
            "dst": dst.astype(object),
            "src_bank": raw["From Bank"].str.strip().astype(object),
            "dst_bank": raw["To Bank"].str.strip().astype(object),
            "amount": pd.to_numeric(raw["Amount Received"], errors="coerce")
            .fillna(0.0)
            .to_numpy(dtype=float),
            "channel": raw["Payment Format"].map(FORMAT_TO_CHANNEL).fillna("OTHER").astype(object),
            "label": pd.to_numeric(raw["Is Laundering"], errors="coerce").fillna(0).astype(int).to_numpy(),
        }
    )
    return df.sort_values(["ts", "txn_id"], kind="mergesort").reset_index(drop=True)


@dataclass
class AmlGraph:
    node_ids: list[str]
    x: torch.Tensor  # [N, len(GRAPH_COLUMNS)] log-transformed degree statistics
    edge_index: torch.Tensor
    edge_attr: torch.Tensor
    edge_time: torch.Tensor
    y: torch.Tensor  # [E] laundering label
    split: torch.Tensor  # [E] 0 train / 1 val / 2 test


def amlworld_graph(df: pd.DataFrame, splits: tuple[float, float] = (0.6, 0.8)) -> AmlGraph:
    edges = df[df["src"] != df["dst"]].reset_index(drop=True)
    t1, t2 = np.quantile(edges["ts"], splits)
    split = np.where(edges["ts"] <= t1, 0, np.where(edges["ts"] <= t2, 1, 2))
    nodes = sorted(set(edges["src"]) | set(edges["dst"]))
    code = pd.Series(np.arange(len(nodes)), index=nodes)
    train_tx = df[df["ts"] <= t1]  # degree statistics from the training period only (no look-ahead)
    stats = degree_stats(
        train_tx, nodes, pd.DataFrame(columns=["account", "peer", "seen"]), set(), set(nodes)
    )
    x = stats.to_numpy(dtype=float).copy()
    for j, c in enumerate(GRAPH_COLUMNS):
        if c in LOG_COLUMNS:
            x[:, j] = np.log1p(np.clip(x[:, j], 0.0, None))
    efeat, etime = edge_features(edges, df, float(t1), 24.0 * 30)
    ei = np.stack(
        [code.reindex(edges["src"]).to_numpy(np.int64), code.reindex(edges["dst"]).to_numpy(np.int64)]
    )
    return AmlGraph(
        node_ids=nodes,
        x=torch.from_numpy(x.astype(np.float32)),
        edge_index=torch.from_numpy(ei),
        edge_attr=torch.from_numpy(efeat),
        edge_time=torch.from_numpy(etime),
        y=torch.from_numpy(edges["label"].to_numpy(np.float32)),
        split=torch.from_numpy(split.astype(np.int64)),
    )


def _ap(y: np.ndarray, s: np.ndarray) -> float | None:
    if y.sum() == 0 or y.sum() == len(y):
        return None
    from sklearn.metrics import average_precision_score

    return round(float(average_precision_score(y, s)), 4)


def _best_f1(y: np.ndarray, s: np.ndarray) -> tuple[float | None, float]:
    """(best F1, threshold) - thresholds are chosen on validation edges only."""
    if y.sum() == 0:
        return None, 0.5
    from sklearn.metrics import precision_recall_curve

    p, r, thr = precision_recall_curve(y, s)
    f = 2 * p * r / np.clip(p + r, 1e-12, None)
    i = int(np.argmax(f[:-1])) if len(thr) else 0
    return round(float(f[i]), 4), float(thr[i]) if len(thr) else 0.5


def _f1_at(y: np.ndarray, s: np.ndarray, thr: float) -> float | None:
    if y.sum() == 0:
        return None
    pred = s >= thr
    tp = float((pred & (y > 0)).sum())
    p = tp / max(float(pred.sum()), 1.0)
    r = tp / float(y.sum())
    return round(2 * p * r / (p + r), 4) if p + r else 0.0


def pretrain_edges(
    g: AmlGraph,
    *,
    device: torch.device,
    epochs: int = 20,
    hidden: int = 64,
    edge_hidden: int = 32,
    layers: int = 2,
    dropout: float = 0.1,
    temporal: bool = False,
    lr: float = 3e-3,
    seed: int = 7,
    log: Callable[[str], None] = print,
) -> tuple[WapsiGnn, dict]:
    """Full-batch edge-classification pre-training (MI300X: the whole HI-Small graph fits in HBM)."""
    torch.manual_seed(seed)
    cfg = ModelConfig(
        node_dim=g.x.shape[1],
        edge_dim=len(EDGE_COLUMNS),
        hidden=hidden,
        edge_hidden=edge_hidden,
        layers=layers,
        dropout=dropout,
        temporal=temporal,
    )
    model = WapsiGnn(cfg)
    tr = g.split == 0
    model.set_normalization(g.x.mean(0), g.x.std(0), g.edge_attr[tr].mean(0), g.edge_attr[tr].std(0))
    head = EdgeHead(hidden, edge_hidden)
    model.to(device)
    head.to(device)
    x, ei, ea, et, y, split = (
        t.to(device) for t in (g.x, g.edge_index, g.edge_attr, g.edge_time, g.y, g.split)
    )
    n_pos = float(y[split == 0].sum())
    n_neg = float((split == 0).sum()) - n_pos
    pos_weight = torch.tensor(min(n_neg / max(n_pos, 1.0), 200.0), device=device)
    opt = torch.optim.AdamW([*model.parameters(), *head.parameters()], lr=lr, weight_decay=1e-4)
    history, best, best_state = [], -1.0, None

    def logits_for(visible: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        vis = visible.nonzero().squeeze(-1)
        xn = model.normalize_x(x)
        _, h = model.embed(xn, ei[:, vis], ea[vis], et[vis])
        tgt = target.nonzero().squeeze(-1)
        return head(h, ei[:, tgt], model.encode_edges(ea[tgt], et[tgt]))

    for epoch in range(1, epochs + 1):
        t0 = time.perf_counter()
        model.train()
        head.train()
        logit = logits_for(split == 0, split == 0)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(
            logit, y[split == 0], pos_weight=pos_weight
        )
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        model.eval()
        head.eval()
        with torch.no_grad():
            vl = torch.sigmoid(logits_for(split <= 1, split == 1)).cpu().numpy()
        yv = y[split == 1].cpu().numpy()
        rec = {
            "epoch": epoch,
            "loss": round(loss.detach().item(), 5),
            "val_ap": _ap(yv, vl),
            "val_best_f1": _best_f1(yv, vl)[0],
            "epoch_s": round(time.perf_counter() - t0, 3),
        }
        history.append(rec)
        log(
            f"[amlworld] epoch {epoch} loss {rec['loss']:.4f} val AP {rec['val_ap']} best-F1 {rec['val_best_f1']}"
        )
        score = rec["val_ap"] if rec["val_ap"] is not None else -float(loss)
        if score > best:
            best = score
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()
    with torch.no_grad():
        vl = torch.sigmoid(logits_for(split <= 1, split == 1)).cpu().numpy()
        tl = torch.sigmoid(logits_for(split <= 2, split == 2)).cpu().numpy()
    _, thr = _best_f1(y[split == 1].cpu().numpy(), vl)
    yt = y[split == 2].cpu().numpy()
    summary = {
        "nodes": len(g.node_ids),
        "edges": int(g.edge_index.shape[1]),
        "laundering_edges": int(g.y.sum()),
        "history": history,
        "test_ap": _ap(yt, tl),
        "test_minority_f1": _f1_at(yt, tl, thr),
        "threshold_from_val": round(thr, 6),
        "note": "EVENT-TIME AMLworld HI-Small pre-training (edge labels); temporal 60/20/20 split",
    }
    return model.cpu(), summary
