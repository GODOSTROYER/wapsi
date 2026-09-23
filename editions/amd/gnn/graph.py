"""Transaction graphs for the Wapsi GNN (AMD edition).

Two builders share one feature definition (``NODE_COLUMNS`` / ``EDGE_COLUMNS``):

* ``build_local_graph`` - the graph the scorer and the trainer use. A **case subgraph** around the accounts of one
  money trail ("seeds": the victim accounts and every account that received tainted money), plus up to
  ``GraphConfig.max_neighbors`` context counterparties per seed (top by rupees exchanged inside the window) and up
  to ``max_device_peers`` device-sharing peers. Edges are every visible transaction between two nodes inside
  ``[as_of - lookback_days, end]`` (a directed multigraph: one edge per transaction) plus device-link edges.
  Built only through the ``wapsi.ports.Ledger`` protocol, so it works on ``ParquetLedger``, ``InMemoryLedger`` and
  the Snowflake / BigQuery adapters alike.
* ``build_world_graph`` - one graph over a whole time window (every account that transacted in it), vectorised from
  ``ParquetLedger.transactions_frame()``. Used for global statistics and edge-level pre-training; node features are
  the degree statistics (``account_features`` for 60k accounts is optional: it is correct but CPU-heavy).

Design choice (documented in README): **full-batch message passing on case subgraphs** instead of neighbour sampling
on the 2M-edge world graph. The scorer only ever has to label the accounts of one trail, the classify features are
anchored per case (``as_of`` = first reported victim transfer, burst window from each account's first tainted
receipt, see ``wapsi.classify``), and a case subgraph is small (tens to a few hundred nodes), so training batches are
disjoint unions of case subgraphs. Neighbour sampling is still applied where it matters: hub accounts (merchants,
pools) keep only their top-``max_neighbors`` counterparties. Cost is linear in the number of cases, not in the world
size; ``ParquetLedger`` answers each per-account query from its CSR index.

Visibility (AGENTS.md / docs/data-contract.md): home-bank transactions come from ``transactions_touching``; other-bank
activity only from ``consortium_transactions`` and only for **seed** accounts (accounts on the trail, which the bank
really asks CFCFRMS about). Other-bank *context* accounts are never queried through the consortium: their node
features stay zero (``g_features_observed = 0``) apart from ``is_other_bank`` and the degree statistics of the edges
the home bank can see.

Node features: ``wapsi.classify.features.account_features`` columns (reused from the core, anchored exactly like
``classify_trace``) + ``GRAPH_COLUMNS`` degree statistics; heavy-tailed columns are ``log1p``-transformed here and
standardised inside the model with the mean / std saved in the checkpoint.
Edge features: log amount, channel one-hot, time since the sender's previous credit, IST hour-of-day sin/cos,
cross-bank flag, round-amount flag, device-link flag, signed log time relative to the anchor, after-anchor flag.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from wapsi.classify.features import FEATURE_COLUMNS, account_features
from wapsi.config import ClassifyConfig, TraceConfig
from wapsi.models import Channel, Role, TraceResult, Transaction
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc

GRAPH_VERSION = "wapsi-gnn-graph/1"

GRAPH_COLUMNS: tuple[str, ...] = (
    "g_in_txns",  # credits from other accounts inside the window
    "g_out_txns",  # debits to other accounts inside the window
    "g_in_nbrs",  # distinct payers
    "g_out_nbrs",  # distinct payees
    "g_in_amount",  # rupees received (incl. cash deposits)
    "g_out_amount",  # rupees sent (incl. ATM / crypto exits)
    "g_exit_out",  # ATM / crypto debits (no receiving account)
    "g_cash_in",  # cash deposits (no paying account)
    "g_device_peers",  # distinct accounts sharing a device (links first seen by the window end)
    "g_is_seed",  # account is on the trail (or a victim account of the case)
    "g_features_observed",  # 0 for other-bank context accounts (no consortium data requested)
)
NODE_COLUMNS: tuple[str, ...] = (*FEATURE_COLUMNS, *GRAPH_COLUMNS)
LOG_COLUMNS = frozenset(
    {
        "age_days",
        "in_count_24h",
        "out_count_24h",
        "in_amount_24h",
        "out_amount_24h",
        "median_forward_minutes",
        "n_distinct_payers_7d",
        "n_distinct_payees_7d",
        "device_shared_count",
        "shared_device_with_seed_accounts",
        "ncrp_prior_reports",
        "history_days",
        "salary_credits_90d",
        "recurring_rent_like_90d",
        "hist_distinct_payers_90d",
        "hist_credit_turnover_ratio",
        *(c for c in GRAPH_COLUMNS if c not in ("g_is_seed", "g_features_observed")),
    }
)
CHANNELS: tuple[str, ...] = (*(c.value for c in Channel), "OTHER")
EDGE_COLUMNS: tuple[str, ...] = (
    "log_amount",
    *(f"ch_{c}" for c in CHANNELS),
    "dt_prev_credit",  # log1p(hours since the sender's previous credit), capped at the lookback
    "no_prev_credit",
    "hour_sin",
    "hour_cos",
    "is_cross_bank",
    "is_round_amount",
    "is_device_link",
    "t_rel",  # sign(h) * log1p(|h|), h = hours relative to the anchor (as_of)
    "after_anchor",
)
EXIT_CHANNELS = frozenset({Channel.ATM.value, Channel.CRYPTO.value})
IST_OFFSET_S = 5.5 * 3600
DAY_S = 86_400.0
ROUND_UNIT = 1000.0

# Label policy (labels_accounts.role_truth): MULE = 1; legitimate profiles = 0; everything else is masked.
POSITIVE_ROLES = frozenset({Role.MULE.value})
NEGATIVE_ROLES = frozenset(
    {Role.INNOCENT_HOLDER.value, Role.LEGIT.value, Role.MERCHANT.value, Role.INTERMEDIARY.value}
)


@dataclass(frozen=True)
class GraphConfig:
    lookback_days: float = 30.0  # history kept before the anchor
    horizon_hours: float = (
        TraceConfig().window_hours
    )  # window end = anchor + horizon (mirrors trace window_end)
    max_neighbors: int = 10  # context counterparties kept per frontier account (top by rupees exchanged)
    max_device_peers: int = 5  # device-sharing peers kept per frontier account
    hops: int = 1  # context expansion depth around the seeds
    include_device_edges: bool = True
    drop_features: tuple[
        str, ...
    ] = ()  # node feature columns zeroed for every node (signal-availability ablations)

    def fingerprint(self, classify_cfg: ClassifyConfig | None = None) -> str:
        g = {
            k: v for k, v in asdict(self).items() if k != "drop_features"
        }  # applied after caching, see drop_columns
        payload = {"v": GRAPH_VERSION, "g": g, "c": (classify_cfg or ClassifyConfig()).model_dump()}
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def label_of(role: str | None) -> float:
    """1.0 mule, 0.0 legitimate profile, -1.0 masked (victim / unknown / missing)."""
    if role in POSITIVE_ROLES:
        return 1.0
    if role in NEGATIVE_ROLES:
        return 0.0
    return -1.0


# --------------------------------------------------------------------------- containers


@dataclass
class CaseGraph:
    """One case subgraph. ``x`` holds log-transformed but *unstandardised* node features (model normalises)."""

    node_ids: list[str]
    x: torch.Tensor  # [N, len(NODE_COLUMNS)] float32
    edge_index: torch.Tensor  # [2, E] int64 (row 0 = sender, row 1 = receiver)
    edge_attr: torch.Tensor  # [E, len(EDGE_COLUMNS)] float32
    edge_time: torch.Tensor  # [E] float32, hours relative to the anchor
    is_seed: torch.Tensor  # [N] bool
    y: torch.Tensor  # [N] float32, 1 / 0 / -1 (masked)
    meta: dict = field(default_factory=dict)  # case_id, world, as_of, end (ISO strings)

    @property
    def num_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def num_edges(self) -> int:
        return int(self.edge_index.shape[1])

    def to_dict(self) -> dict:
        return {
            "node_ids": list(self.node_ids),
            "x": self.x,
            "edge_index": self.edge_index,
            "edge_attr": self.edge_attr,
            "edge_time": self.edge_time,
            "is_seed": self.is_seed,
            "y": self.y,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> CaseGraph:
        return cls(
            **{k: d[k] for k in ("node_ids", "x", "edge_index", "edge_attr", "edge_time", "is_seed", "y")},
            meta=dict(d.get("meta", {})),
        )


@dataclass
class GraphBatch:
    """Disjoint union of case graphs (what the model consumes)."""

    x: torch.Tensor
    edge_index: torch.Tensor
    edge_attr: torch.Tensor
    edge_time: torch.Tensor
    is_seed: torch.Tensor
    y: torch.Tensor
    graph_index: torch.Tensor  # [N] graph id of every node
    node_ids: list[str]
    num_graphs: int

    def to(self, device: torch.device | str) -> GraphBatch:
        nb = dict(non_blocking=True)
        return GraphBatch(
            x=self.x.to(device, **nb),
            edge_index=self.edge_index.to(device, **nb),
            edge_attr=self.edge_attr.to(device, **nb),
            edge_time=self.edge_time.to(device, **nb),
            is_seed=self.is_seed.to(device, **nb),
            y=self.y.to(device, **nb),
            graph_index=self.graph_index.to(device, **nb),
            node_ids=self.node_ids,
            num_graphs=self.num_graphs,
        )

    @property
    def num_edges(self) -> int:
        return int(self.edge_index.shape[1])


def collate(graphs: Sequence[CaseGraph]) -> GraphBatch:
    offsets, n = [], 0
    for g in graphs:
        offsets.append(n)
        n += g.num_nodes
    return GraphBatch(
        x=torch.cat([g.x for g in graphs]) if graphs else torch.zeros(0, len(NODE_COLUMNS)),
        edge_index=(
            torch.cat([g.edge_index + o for g, o in zip(graphs, offsets, strict=True)], dim=1)
            if graphs
            else torch.zeros(2, 0, dtype=torch.long)
        ),
        edge_attr=torch.cat([g.edge_attr for g in graphs]) if graphs else torch.zeros(0, len(EDGE_COLUMNS)),
        edge_time=torch.cat([g.edge_time for g in graphs]) if graphs else torch.zeros(0),
        is_seed=torch.cat([g.is_seed for g in graphs]) if graphs else torch.zeros(0, dtype=torch.bool),
        y=torch.cat([g.y for g in graphs]) if graphs else torch.zeros(0),
        graph_index=(
            torch.cat([torch.full((g.num_nodes,), i, dtype=torch.long) for i, g in enumerate(graphs)])
            if graphs
            else torch.zeros(0, dtype=torch.long)
        ),
        node_ids=[a for g in graphs for a in g.node_ids],
        num_graphs=len(graphs),
    )


# --------------------------------------------------------------------------- frames


def _epoch(ts: datetime) -> float:
    return ensure_utc(ts).timestamp()


TX_COLUMNS = ("txn_id", "ts", "src", "dst", "src_bank", "dst_bank", "amount", "channel")


def txn_frame(txns: Iterable[Transaction]) -> pd.DataFrame:
    """Transactions -> frame (ts = UTC epoch seconds), de-duplicated by ``txn_id`` and sorted by (ts, txn_id)."""
    uniq: dict[str, Transaction] = {t.txn_id: t for t in txns}
    vals = list(uniq.values())
    df = pd.DataFrame(
        {
            "txn_id": pd.Series([t.txn_id for t in vals], dtype=object),
            "ts": np.array([_epoch(t.ts) for t in vals], dtype=float),
            "src": pd.Series([t.from_account for t in vals], dtype=object),
            "dst": pd.Series([t.to_account for t in vals], dtype=object),
            "src_bank": pd.Series([t.from_bank_id for t in vals], dtype=object),
            "dst_bank": pd.Series([t.to_bank_id for t in vals], dtype=object),
            "amount": np.array([t.amount for t in vals], dtype=float),
            "channel": pd.Series([str(t.channel) for t in vals], dtype=object),
        }
    )
    return df.sort_values(["ts", "txn_id"], kind="mergesort").reset_index(drop=True)


def _bank_of(ledger: Ledger, ids: Iterable[str]) -> dict[str, str]:
    ids = list(dict.fromkeys(ids))
    accts = ledger.accounts(ids)
    return {a: (accts[a].bank_id if a in accts else a.split("-", 1)[0]) for a in ids}


def pull_transactions(
    ledger: Ledger,
    home_ids: Iterable[str],
    consortium_ids: Iterable[str],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Visible transactions of ``home_ids`` plus consortium responses for ``consortium_ids`` (trail accounts only)."""
    home_ids, consortium_ids = list(home_ids), list(consortium_ids)
    txns: list[Transaction] = []
    if home_ids:
        txns.extend(ledger.transactions_touching(home_ids, start, end))
    if consortium_ids:
        txns.extend(ledger.consortium_transactions(consortium_ids, start, end))
    return txn_frame(txns)


def _concat_tx(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if not f.empty]
    if not frames:
        return txn_frame([])
    df = pd.concat(frames, ignore_index=True).drop_duplicates("txn_id")
    return df.sort_values(["ts", "txn_id"], kind="mergesort").reset_index(drop=True)


def select_context(tx: pd.DataFrame, frontier: Iterable[str], exclude: set[str], k: int) -> list[str]:
    """Top-``k`` counterparties (by rupees exchanged, then count, then id) of every frontier account."""
    frontier = set(frontier)
    if tx.empty or k <= 0 or not frontier:
        return []
    src, dst = tx["src"], tx["dst"]
    not_self = (src != dst).to_numpy()
    out_rows = tx[src.isin(frontier).to_numpy() & dst.notna().to_numpy() & not_self]
    in_rows = tx[dst.isin(frontier).to_numpy() & src.notna().to_numpy() & not_self]
    cand = pd.concat(
        [
            pd.DataFrame({"anchor": out_rows["src"], "cp": out_rows["dst"], "amount": out_rows["amount"]}),
            pd.DataFrame({"anchor": in_rows["dst"], "cp": in_rows["src"], "amount": in_rows["amount"]}),
        ],
        ignore_index=True,
    )
    cand = cand[~cand["cp"].isin(exclude)]
    if cand.empty:
        return []
    agg = cand.groupby(["anchor", "cp"], sort=False)["amount"].agg(["sum", "count"]).reset_index()
    agg = agg.sort_values(
        ["anchor", "sum", "count", "cp"], ascending=[True, False, False, True], kind="mergesort"
    )
    return sorted(set(agg.groupby("anchor", sort=False).head(k)["cp"]))


def _device_pairs(ledger: Ledger, ids: Sequence[str], end_s: float) -> pd.DataFrame:
    """(account, peer, seen) for every pair sharing a device, both links first seen by ``end_s``."""
    empty = pd.DataFrame(
        {"account": pd.Series(dtype=object), "peer": pd.Series(dtype=object), "seen": pd.Series(dtype=float)}
    )
    own = [(lk.account_id, lk.device_id, _epoch(lk.first_seen)) for lk in ledger.device_links(ids)]
    own = [o for o in own if o[2] <= end_s]
    if not own:
        return empty
    own_df = pd.DataFrame(own, columns=["account", "device", "seen_a"])
    peers = [
        (lk.device_id, lk.account_id, _epoch(lk.first_seen))
        for lk in ledger.device_links_for_devices(sorted(own_df["device"].unique()))
    ]
    peer_df = pd.DataFrame(peers, columns=["device", "peer", "seen_b"])
    pairs = own_df.merge(peer_df, on="device")
    pairs = pairs[(pairs["peer"] != pairs["account"]) & (pairs["seen_b"] <= end_s)]
    if pairs.empty:
        return empty
    pairs = pairs.assign(seen=np.maximum(pairs["seen_a"], pairs["seen_b"]))
    pairs = pairs.groupby(["account", "peer"], as_index=False)["seen"].min()
    return pairs.sort_values(["account", "peer"]).reset_index(drop=True)


def _device_context(pairs: pd.DataFrame, frontier: Iterable[str], exclude: set[str], k: int) -> list[str]:
    if pairs.empty or k <= 0:
        return []
    sub = pairs[pairs["account"].isin(set(frontier)) & ~pairs["peer"].isin(exclude)]
    return sorted(set(sub.groupby("account", sort=False).head(k)["peer"]))


# --------------------------------------------------------------------------- features


def transform_node_frame(df: pd.DataFrame) -> np.ndarray:
    """``NODE_COLUMNS`` frame -> float32 matrix with ``log1p`` on heavy-tailed columns (no standardisation)."""
    x = df.reindex(columns=list(NODE_COLUMNS)).astype(float).fillna(0.0).to_numpy(copy=True)
    for j, c in enumerate(NODE_COLUMNS):
        if c in LOG_COLUMNS:
            x[:, j] = np.log1p(np.clip(x[:, j], 0.0, None))
    x[~np.isfinite(x)] = 0.0
    return x.astype(np.float32)


def degree_stats(
    tx: pd.DataFrame, node_ids: Sequence[str], device_pairs: pd.DataFrame, seeds: set[str], observed: set[str]
) -> pd.DataFrame:
    """``GRAPH_COLUMNS`` per node from the visible transactions in the window (all counterparties, not only nodes)."""
    index = pd.Index(list(node_ids), name="account_id")
    out = pd.DataFrame(0.0, index=index, columns=list(GRAPH_COLUMNS))
    if not tx.empty:
        not_self = (tx["src"] != tx["dst"]).to_numpy()
        cred = tx[tx["dst"].isin(index).to_numpy() & not_self]
        deb = tx[tx["src"].isin(index).to_numpy() & not_self]
        acct_cred = cred[cred["src"].notna()]
        acct_deb = deb[deb["dst"].notna()]

        def agg(frame: pd.DataFrame, by: str, col: str, how: str) -> pd.Series:
            if frame.empty:
                return pd.Series(0.0, index=index)
            return frame.groupby(by)[col].agg(how).reindex(index).fillna(0.0).astype(float)

        out["g_in_txns"] = agg(acct_cred, "dst", "txn_id", "count")
        out["g_out_txns"] = agg(acct_deb, "src", "txn_id", "count")
        out["g_in_nbrs"] = agg(acct_cred, "dst", "src", "nunique")
        out["g_out_nbrs"] = agg(acct_deb, "src", "dst", "nunique")
        out["g_in_amount"] = agg(cred, "dst", "amount", "sum")
        out["g_out_amount"] = agg(deb, "src", "amount", "sum")
        out["g_exit_out"] = agg(
            deb[deb["dst"].isna() & deb["channel"].isin(EXIT_CHANNELS)], "src", "txn_id", "count"
        )
        out["g_cash_in"] = agg(cred[cred["src"].isna()], "dst", "txn_id", "count")
    if not device_pairs.empty:
        out["g_device_peers"] = (
            device_pairs.groupby("account")["peer"].nunique().reindex(index).fillna(0.0).astype(float)
        )
    out["g_is_seed"] = [float(a in seeds) for a in index]
    out["g_features_observed"] = [float(a in observed) for a in index]
    return out


def _dt_prev_credit_hours(edges: pd.DataFrame, tx: pd.DataFrame) -> np.ndarray:
    """Hours from each edge to the sender's latest visible credit at or before it (NaN if none).

    Same-second credits count as previous (AGENTS.md: a credit is applied before the debits it funds)."""
    if edges.empty:
        return np.zeros(0)
    credits = tx.loc[tx["dst"].notna() & (tx["src"] != tx["dst"]), ["dst", "ts"]]
    if credits.empty:
        return np.full(len(edges), np.nan)
    credits = pd.DataFrame(
        {
            "account": credits["dst"].astype(str).to_numpy(),
            "ts": credits["ts"].to_numpy(),
            "credit_ts": credits["ts"].to_numpy(),
        }
    ).sort_values("ts", kind="mergesort")
    q = pd.DataFrame(
        {
            "account": edges["src"].astype(str).to_numpy(),
            "ts": edges["ts"].to_numpy(),
            "_i": np.arange(len(edges)),
        }
    ).sort_values("ts", kind="mergesort")
    m = pd.merge_asof(q, credits, on="ts", by="account", direction="backward", allow_exact_matches=True)
    m = m.sort_values("_i")
    return ((m["ts"] - m["credit_ts"]) / 3600.0).to_numpy(dtype=float)


def edge_features(
    edges: pd.DataFrame, tx_for_credits: pd.DataFrame, as_of_s: float, lookback_hours: float
) -> tuple[np.ndarray, np.ndarray]:
    """Transaction edges (columns of ``TX_COLUMNS``) -> (``EDGE_COLUMNS`` matrix, hours relative to the anchor)."""
    n = len(edges)
    feat = np.zeros((n, len(EDGE_COLUMNS)), dtype=np.float32)
    if n == 0:
        return feat, np.zeros(0, dtype=np.float32)
    col = {c: j for j, c in enumerate(EDGE_COLUMNS)}
    amount = edges["amount"].to_numpy(dtype=float)
    ts = edges["ts"].to_numpy(dtype=float)
    feat[:, col["log_amount"]] = np.log1p(np.clip(amount, 0.0, None))
    ch = edges["channel"].astype(str).to_numpy()
    known = set(CHANNELS)
    for c in CHANNELS[:-1]:
        feat[:, col[f"ch_{c}"]] = ch == c
    feat[:, col["ch_OTHER"]] = np.array([c not in known or c == "OTHER" for c in ch], dtype=bool)
    cap = max(float(lookback_hours), 1.0)
    dt = _dt_prev_credit_hours(edges, tx_for_credits)
    none = ~np.isfinite(dt)
    dt = np.where(none, cap, np.clip(dt, 0.0, cap))
    feat[:, col["dt_prev_credit"]] = np.log1p(dt)
    feat[:, col["no_prev_credit"]] = none
    hour = ((ts + IST_OFFSET_S) % DAY_S) / 3600.0
    feat[:, col["hour_sin"]] = np.sin(2 * math.pi * hour / 24.0)
    feat[:, col["hour_cos"]] = np.cos(2 * math.pi * hour / 24.0)
    sb, db = edges["src_bank"].astype(object).to_numpy(), edges["dst_bank"].astype(object).to_numpy()
    feat[:, col["is_cross_bank"]] = np.array(
        [a is not None and b is not None and a != b for a, b in zip(sb, db, strict=True)]
    )
    feat[:, col["is_round_amount"]] = (amount >= ROUND_UNIT) & (
        np.abs(amount - np.round(amount / ROUND_UNIT) * ROUND_UNIT) < 0.005
    )
    rel_h = (ts - as_of_s) / 3600.0
    feat[:, col["t_rel"]] = np.sign(rel_h) * np.log1p(np.abs(rel_h))
    feat[:, col["after_anchor"]] = rel_h >= 0
    return feat, rel_h.astype(np.float32)


def device_edge_features(pairs: pd.DataFrame, as_of_s: float) -> tuple[np.ndarray, np.ndarray]:
    feat = np.zeros((len(pairs), len(EDGE_COLUMNS)), dtype=np.float32)
    if pairs.empty:
        return feat, np.zeros(0, dtype=np.float32)
    col = {c: j for j, c in enumerate(EDGE_COLUMNS)}
    rel_h = (pairs["seen"].to_numpy(dtype=float) - as_of_s) / 3600.0
    feat[:, col["is_device_link"]] = 1.0
    feat[:, col["t_rel"]] = np.sign(rel_h) * np.log1p(np.abs(rel_h))
    feat[:, col["after_anchor"]] = rel_h >= 0
    return feat, rel_h.astype(np.float32)


# --------------------------------------------------------------------------- local (case) graph


def build_local_graph(
    ledger: Ledger,
    seed_features: pd.DataFrame,
    as_of: datetime,
    *,
    end: datetime | None = None,
    trail_accounts: Iterable[str] | None = None,
    cfg: GraphConfig | None = None,
    classify_cfg: ClassifyConfig | None = None,
    labels: Mapping[str, str] | None = None,
    meta: Mapping[str, str] | None = None,
) -> CaseGraph:
    """Case subgraph around the rows of ``seed_features`` (index ``account_id``, columns ``FEATURE_COLUMNS``).

    ``seed_features`` must be anchored like ``classify_trace`` does (it *is* the frame ``classify_trace`` passes to a
    scorer). ``trail_accounts`` (layer >= 1) drive ``shared_device_with_seed_accounts`` of the context accounts;
    default: every seed. ``labels``: ``account_id -> role_truth`` (training only; the product never reads labels).
    """
    cfg = cfg or GraphConfig()
    ccfg = classify_cfg or ClassifyConfig()
    as_of = ensure_utc(as_of)
    end = ensure_utc(end) if end is not None else as_of + timedelta(hours=cfg.horizon_hours)
    start = as_of - timedelta(days=cfg.lookback_days)
    as_of_s, end_s = as_of.timestamp(), end.timestamp()
    seeds = [str(a) for a in dict.fromkeys(seed_features.index)]
    seed_set = set(seeds)
    trail = set(trail_accounts) if trail_accounts is not None else set(seeds)
    home = ledger.home_bank_id()
    bank = _bank_of(ledger, seeds)

    # 1. seeds: home-visible transactions + consortium responses for other-bank trail accounts
    tx = pull_transactions(
        ledger, [a for a in seeds if bank[a] == home], [a for a in seeds if bank[a] != home], start, end
    )
    nodes: list[str] = list(seeds)
    known: set[str] = set(seeds)
    frontier = list(seeds)
    frames = [tx]
    for _hop in range(max(cfg.hops, 0)):
        pairs = _device_pairs(ledger, frontier, end_s)
        ctx = select_context(tx, frontier, known, cfg.max_neighbors)
        ctx += [a for a in _device_context(pairs, frontier, known | set(ctx), cfg.max_device_peers)]
        ctx = sorted(set(ctx) - known)
        if not ctx:
            break
        bank.update(_bank_of(ledger, ctx))
        ctx_home = [a for a in ctx if bank[a] == home]
        # context accounts: home-visible activity only (never a consortium request for a non-trail account)
        frames.append(pull_transactions(ledger, ctx_home, [], start, end))
        tx = _concat_tx(frames)
        nodes.extend(ctx)
        known.update(ctx)
        frontier = ctx

    ctx_all = nodes[len(seeds) :]
    ctx_home = [a for a in ctx_all if bank.get(a) == home]
    ctx_other = [a for a in ctx_all if bank.get(a) != home]
    observed = seed_set | set(ctx_home)

    # 2. node features: seeds as given; home context anchored like non-tainted trace accounts in classify_trace
    frames_x = [seed_features.reindex(index=seeds, columns=list(FEATURE_COLUMNS)).astype(float).fillna(0.0)]
    if ctx_home:
        frames_x.append(
            account_features(ledger, ctx_home, as_of, ccfg, seed_accounts=trail, burst_start=as_of)
        )
    if ctx_other:
        other = pd.DataFrame(0.0, index=pd.Index(ctx_other, name="account_id"), columns=list(FEATURE_COLUMNS))
        other["is_other_bank"] = 1.0
        frames_x.append(other)
    feats = pd.concat(frames_x)
    feats = feats.reindex(nodes)
    for c in cfg.drop_features:
        if c in feats:
            feats[c] = 0.0
    all_pairs = _device_pairs(ledger, nodes, end_s)
    stats = degree_stats(tx, nodes, all_pairs, seed_set, observed)
    x = transform_node_frame(pd.concat([feats, stats], axis=1))

    # 3. edges: every visible transaction between two nodes inside the window (+ device links)
    code = {a: i for i, a in enumerate(nodes)}
    in_window = (tx["ts"] >= start.timestamp()) & (tx["ts"] <= end_s)
    et = tx[
        in_window
        & tx["src"].isin(code).to_numpy()
        & tx["dst"].isin(code).to_numpy()
        & (tx["src"] != tx["dst"])
    ]
    efeat, etime = edge_features(et, tx, as_of_s, cfg.lookback_days * 24.0)
    src = et["src"].map(code).to_numpy(dtype=np.int64)
    dst = et["dst"].map(code).to_numpy(dtype=np.int64)
    if cfg.include_device_edges and not all_pairs.empty:
        dp = all_pairs[all_pairs["account"].isin(code) & all_pairs["peer"].isin(code)]
        dfeat, dtime = device_edge_features(dp, as_of_s)
        src = np.concatenate([src, dp["account"].map(code).to_numpy(dtype=np.int64)])
        dst = np.concatenate([dst, dp["peer"].map(code).to_numpy(dtype=np.int64)])
        efeat, etime = np.concatenate([efeat, dfeat]), np.concatenate([etime, dtime])

    y = np.array([label_of(labels.get(a)) if labels is not None else -1.0 for a in nodes], dtype=np.float32)
    m = {"as_of": as_of.isoformat(), "end": end.isoformat(), "graph_version": GRAPH_VERSION}
    m.update({k: str(v) for k, v in (meta or {}).items()})
    return CaseGraph(
        node_ids=nodes,
        x=torch.from_numpy(x),
        edge_index=torch.from_numpy(np.stack([src, dst]))
        if len(src)
        else torch.zeros(2, 0, dtype=torch.long),
        edge_attr=torch.from_numpy(np.ascontiguousarray(efeat, dtype=np.float32)),
        edge_time=torch.from_numpy(np.ascontiguousarray(etime, dtype=np.float32)),
        is_seed=torch.tensor([a in seed_set for a in nodes], dtype=torch.bool),
        y=torch.from_numpy(y),
        meta=m,
    )


def isolated_graph(seed_features: pd.DataFrame, drop_features: Sequence[str] = ()) -> CaseGraph:
    """No ledger available: every row is an isolated node (no graph context). Degraded mode, documented."""
    ids = [str(a) for a in seed_features.index]
    seed_features = seed_features.copy()
    for c in drop_features:
        if c in seed_features:
            seed_features[c] = 0.0
    stats = pd.DataFrame(0.0, index=seed_features.index, columns=list(GRAPH_COLUMNS))
    stats["g_is_seed"] = 1.0
    stats["g_features_observed"] = 1.0
    x = transform_node_frame(pd.concat([seed_features.reindex(columns=list(FEATURE_COLUMNS)), stats], axis=1))
    return CaseGraph(
        node_ids=ids,
        x=torch.from_numpy(x),
        edge_index=torch.zeros(2, 0, dtype=torch.long),
        edge_attr=torch.zeros(0, len(EDGE_COLUMNS)),
        edge_time=torch.zeros(0),
        is_seed=torch.ones(len(ids), dtype=torch.bool),
        y=torch.full((len(ids),), -1.0),
        meta={"isolated": "1", "graph_version": GRAPH_VERSION},
    )


# --------------------------------------------------------------------------- trace -> graph (training / scorer)


@dataclass(frozen=True)
class Anchor:
    """Time context of one case: history before ``as_of``, edges up to ``end``; ``trail`` = layer >= 1 accounts."""

    as_of: datetime
    end: datetime
    trail: frozenset[str]
    burst: Mapping[str, datetime] | None = None  # per-account burst start (first tainted receipt)


def anchor_from_trace(trace: TraceResult) -> Anchor:
    ws = ensure_utc(trace.window_start)
    return Anchor(
        as_of=ws,
        end=ensure_utc(trace.window_end),
        trail=frozenset(a for a, st in trace.accounts.items() if st.layer >= 1),
        burst={
            a: ensure_utc(st.first_tainted_at) if st.first_tainted_at else ws
            for a, st in trace.accounts.items()
        },
    )


def trace_seed_features(
    ledger: Ledger, trace: TraceResult, cfg: ClassifyConfig | None = None
) -> pd.DataFrame:
    """Exactly the feature frame ``wapsi.classify.classify_trace`` hands to its scorer."""
    cfg = cfg or ClassifyConfig()
    a = anchor_from_trace(trace)
    ids = sorted(trace.accounts)
    return account_features(
        ledger, ids, a.as_of, cfg, seed_accounts=set(a.trail), burst_start=dict(a.burst or {})
    )


def graph_for_trace(
    ledger: Ledger,
    trace: TraceResult,
    cfg: GraphConfig | None = None,
    classify_cfg: ClassifyConfig | None = None,
    labels: Mapping[str, str] | None = None,
    meta: Mapping[str, str] | None = None,
) -> CaseGraph:
    a = anchor_from_trace(trace)
    feats = trace_seed_features(ledger, trace, classify_cfg)
    return build_local_graph(
        ledger,
        feats,
        a.as_of,
        end=a.end,
        trail_accounts=a.trail,
        cfg=cfg,
        classify_cfg=classify_cfg,
        labels=labels,
        meta={"case_id": trace.case_id, **(meta or {})},
    )


def trace_window_end(first: datetime, last: datetime, horizon_hours: float) -> datetime:
    """Mirror of ``wapsi.graph.expand``: first + horizon, extended to last + horizon when the last seed is later."""
    end = first + timedelta(hours=horizon_hours)
    return last + timedelta(hours=horizon_hours) if last > end else end


# --------------------------------------------------------------------------- world helpers (training data)


def load_cases(world: str | Path) -> list[list[str]]:
    """ncrp_id groups of a generated world: golden cases if present, else the product's own grouping."""
    world = Path(world)
    for p in (world / "golden" / "cases.jsonl", Path("data/golden") / world.name / "cases.jsonl"):
        if p.exists():
            groups = []
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    d = json.loads(line)
                    groups.append((d["case_id"], sorted(d["ncrp_ids"])))
            return [g for _, g in groups]
    from wapsi.adapters.parquet_ledger import ParquetLedger
    from wapsi.pipeline.run import group_complaints

    ledger = ParquetLedger(world)
    return group_complaints(ledger, ledger.complaints())


def world_labels(world: str | Path) -> dict[str, str]:
    df = pd.read_parquet(Path(world) / "labels_accounts.parquet", columns=["account_id", "role_truth"])
    return dict(zip(df["account_id"].astype(str), df["role_truth"].astype(str), strict=True))


def world_case_graphs(
    world: str | Path,
    cfg: GraphConfig | None = None,
    classify_cfg: ClassifyConfig | None = None,
    trace_cfg: TraceConfig | None = None,
    *,
    with_labels: bool = True,
    limit: int | None = None,
    ledger: Ledger | None = None,
) -> list[CaseGraph]:
    """One labelled case subgraph per case of a generated world (trail from ``trace_case``, like production)."""
    from wapsi.graph.trace import trace_case

    world = Path(world)
    if ledger is None:
        from wapsi.adapters.parquet_ledger import ParquetLedger

        ledger = ParquetLedger(world)
    labels = world_labels(world) if with_labels else None
    out: list[CaseGraph] = []
    for i, ncrp_ids in enumerate(load_cases(world)[:limit]):
        trace = trace_case(ledger, ncrp_ids, trace_cfg or TraceConfig(), case_id=f"{world.name}-{i:04d}")
        if not trace.accounts:
            continue
        out.append(graph_for_trace(ledger, trace, cfg, classify_cfg, labels, meta={"world": world.name}))
    return out


def drop_columns(graphs: Sequence[CaseGraph], columns: Sequence[str]) -> list[CaseGraph]:
    """Zero node feature columns in place (``transform_node_frame`` maps 0 to 0, so this equals dropping them at
    build time; cached graphs can be reused for ablations)."""
    idx = [NODE_COLUMNS.index(c) for c in columns if c in NODE_COLUMNS]
    if idx:
        for g in graphs:
            g.x[:, idx] = 0.0
    return list(graphs)


def cached_world_case_graphs(
    world: str | Path,
    cache_dir: str | Path | None,
    cfg: GraphConfig | None = None,
    classify_cfg: ClassifyConfig | None = None,
    *,
    refresh: bool = False,
    limit: int | None = None,
) -> list[CaseGraph]:
    """``world_case_graphs`` with an on-disk cache keyed by world name, config fingerprint and file mtimes."""
    world = Path(world)
    cfg = cfg or GraphConfig()
    if cache_dir is None:
        return world_case_graphs(world, cfg, classify_cfg, limit=limit)
    stamp = int(max((p.stat().st_mtime for p in world.glob("*.parquet")), default=0))
    key = f"{world.name}-{cfg.fingerprint(classify_cfg)}-{stamp}{'' if limit is None else f'-n{limit}'}"
    path = Path(cache_dir) / f"{key}.pt"
    if path.exists() and not refresh:
        return drop_columns(
            [CaseGraph.from_dict(d) for d in torch.load(path, weights_only=True)], cfg.drop_features
        )
    graphs = world_case_graphs(world, cfg, classify_cfg, limit=limit)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save([g.to_dict() for g in graphs], path)
    return graphs


# --------------------------------------------------------------------------- whole-window graph (vectorised)


def _world_tx(ledger: Ledger, start: datetime, end: datetime) -> pd.DataFrame:
    s, e = pd.Timestamp(ensure_utc(start)), pd.Timestamp(ensure_utc(end))
    if hasattr(ledger, "transactions_frame"):  # ParquetLedger: Arrow -> pandas, no pydantic per row
        df = ledger.transactions_frame()
        ts = pd.to_datetime(df["ts"], utc=True)
        keep = ((ts >= s) & (ts <= e)).to_numpy()
        df, ts = df[keep], ts[keep]
        return (
            pd.DataFrame(
                {
                    "txn_id": df["txn_id"].astype(object).to_numpy(),
                    "ts": (ts - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy(dtype=float),
                    "src": df["from_account"].astype(object).to_numpy(),
                    "dst": df["to_account"].astype(object).to_numpy(),
                    "src_bank": df["from_bank_id"].astype(object).to_numpy(),
                    "dst_bank": df["to_bank_id"].astype(object).to_numpy(),
                    "amount": df["amount"].to_numpy(dtype=float),
                    "channel": df["channel"].astype(str).to_numpy(),
                }
            )
            .sort_values(["ts", "txn_id"], kind="mergesort")
            .reset_index(drop=True)
        )
    if hasattr(ledger, "all_transactions"):  # InMemoryLedger
        s_e, e_e = s.timestamp(), e.timestamp()
        return txn_frame(t for t in ledger.all_transactions() if s_e <= _epoch(t.ts) <= e_e)
    raise TypeError("build_world_graph needs a ledger with transactions_frame() or all_transactions()")


@dataclass
class WorldGraph:
    node_ids: list[str]
    x: torch.Tensor  # degree statistics (GRAPH_COLUMNS, log-transformed) or full NODE_COLUMNS
    node_columns: tuple[str, ...]
    edge_index: torch.Tensor
    edge_attr: torch.Tensor
    edge_time: torch.Tensor
    txn_ids: list[str]


def build_world_graph(
    ledger: Ledger,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime | None = None,
    with_account_features: bool = False,
    classify_cfg: ClassifyConfig | None = None,
) -> WorldGraph:
    """One directed multigraph over every account-to-account transaction in ``[start, end]`` (ground-truth view:
    no visibility filter; for global statistics and pre-training, not for case scoring).

    The 2M-transaction ``full`` world builds in seconds (Arrow -> pandas -> numpy -> torch, no per-row Python)."""
    start, end = ensure_utc(start), ensure_utc(end)
    as_of = ensure_utc(as_of) if as_of is not None else end
    tx = _world_tx(ledger, start, end)
    acct_edges = tx[tx["src"].notna() & tx["dst"].notna() & (tx["src"] != tx["dst"])]
    ids = pd.unique(pd.concat([tx["src"].dropna(), tx["dst"].dropna()], ignore_index=True))
    nodes = sorted(str(a) for a in ids)
    code = pd.Series(np.arange(len(nodes)), index=nodes)
    stats = degree_stats(tx, nodes, pd.DataFrame(columns=["account", "peer", "seen"]), set(), set(nodes))
    stats["g_is_seed"] = 0.0
    if with_account_features:
        feats = account_features(ledger, nodes, as_of, classify_cfg or ClassifyConfig())
        x, cols = transform_node_frame(pd.concat([feats, stats], axis=1)), NODE_COLUMNS
    else:
        x = stats.to_numpy(dtype=float).copy()
        for j, c in enumerate(GRAPH_COLUMNS):
            if c in LOG_COLUMNS:
                x[:, j] = np.log1p(np.clip(x[:, j], 0.0, None))
        x, cols = x.astype(np.float32), GRAPH_COLUMNS
    efeat, etime = edge_features(acct_edges, tx, as_of.timestamp(), (as_of - start).total_seconds() / 3600.0)
    ei = np.stack(
        [
            code.reindex(acct_edges["src"]).to_numpy(np.int64),
            code.reindex(acct_edges["dst"]).to_numpy(np.int64),
        ]
    )
    return WorldGraph(
        node_ids=nodes,
        x=torch.from_numpy(x),
        node_columns=tuple(cols),
        edge_index=torch.from_numpy(ei),
        edge_attr=torch.from_numpy(efeat),
        edge_time=torch.from_numpy(etime),
        txn_ids=acct_edges["txn_id"].astype(str).tolist(),
    )


def world_bounds(ledger: Ledger) -> tuple[datetime, datetime]:
    meta = ledger.meta() if hasattr(ledger, "meta") else {}
    if "world_start" in meta and "world_end" in meta:
        return datetime.fromisoformat(meta["world_start"]), datetime.fromisoformat(meta["world_end"])
    return datetime(1970, 1, 1, tzinfo=UTC), datetime.now(UTC)
