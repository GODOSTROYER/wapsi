"""``GnnScorer``: the GNN behind the ``wapsi.classify.Scorer`` protocol.

    from editions.amd.gnn.scorer import GnnScorer
    scorer = GnnScorer.load("editions/amd/gnn/checkpoints/<name>", ledger=ledger)
    classify_trace(ledger, trace, cfg, model=scorer)                   # drop-in, like HeuristicScorer / XgbScorer
    process_case(ledger, ncrp_ids, llm=llm, scorer=scorer, ...)        # whole pipeline

How graph context enters. The protocol hands a scorer only the per-account feature frame (index ``account_id``), so
the scorer holds a ledger reference and builds the case subgraph itself (``graph.build_local_graph``): the rows of the
frame become the seed nodes (their features are used as given, so they match the heuristic / XGBoost inputs
exactly), context counterparties and device peers are added from the ledger, and the GNN scores the seeds with
messages from their in- and out-neighbourhoods. The time anchor is resolved, in order, from
1. an explicit ``as_of`` (service calls),
2. a bound trace (``GnnScorer.for_trace`` / ``bind``): ``window_start`` / ``window_end`` / layers of the trace,
3. the complaints whose victim accounts are among the rows (a trace always contains its victim accounts): the first
   reported transfer to an account in the frame = ``trace.window_start``; the window end mirrors
   ``wapsi.graph.expand``. This is what makes ``classify_trace(..., model=GnnScorer.load(path, ledger=...))`` work,
4. otherwise the world end (``meta.world_end``) with a warning.
Without a ledger every row is scored as an isolated node (degraded, documented, ``warnings`` says so).

Contributions (``wapsi.classify.explain`` reasons keep working). For each account, Integrated Gradients over its own
standardised node features, path from the training mean (baseline) to the actual values, with the rest of the
graph held fixed; attributions are in log-odds and map 1:1 to ``FEATURE_COLUMNS`` (and the ``g_*`` degree
statistics). Two extra columns make the decomposition additive:
``bias`` = logit of an isolated account with mean features, ``graph_neighbourhood`` = what the account's position in
the transaction graph adds (logit with baseline own-features in the real neighbourhood minus ``bias``), and
``ig_residual`` = numerical integration error. ``bias + graph_neighbourhood + sum(features) + ig_residual = logit``.
Columns unknown to ``explain`` are ignored by it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from editions.amd.gnn.graph import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    Anchor,
    CaseGraph,
    GraphConfig,
    anchor_from_trace,
    build_local_graph,
    isolated_graph,
    pull_transactions,
    trace_seed_features,
    trace_window_end,
    world_bounds,
)
from editions.amd.gnn.model import WapsiGnn, load_checkpoint, resolve_device
from wapsi.classify.features import FEATURE_COLUMNS, account_features
from wapsi.classify.scoring import BIAS
from wapsi.config import ClassifyConfig, TraceConfig
from wapsi.models import TraceResult
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc

GRAPH_CONTEXT = "graph_neighbourhood"
IG_RESIDUAL = "ig_residual"
CONTRIBUTION_COLUMNS: tuple[str, ...] = (*NODE_COLUMNS, GRAPH_CONTEXT, IG_RESIDUAL, BIAS)


@dataclass
class _Result:
    graph: CaseGraph
    positions: np.ndarray  # node position of every requested row
    logits: np.ndarray  # per requested row
    anchor: Anchor | None
    contributions: pd.DataFrame | None = None


class GnnScorer:
    """Implements ``wapsi.classify.scoring.Scorer`` (``name``, ``score``, ``contributions``)."""

    def __init__(
        self,
        model: WapsiGnn,
        config: dict,
        ledger: Ledger | None = None,
        *,
        device: torch.device | str = "cpu",
        name: str | None = None,
        ig_steps: int = 32,
    ) -> None:
        cols = config.get("node_columns")
        if cols is not None and tuple(cols) != NODE_COLUMNS:
            raise ValueError(
                "checkpoint node features differ from this code's NODE_COLUMNS; retrain the model"
            )
        if config.get("edge_columns") is not None and tuple(config["edge_columns"]) != EDGE_COLUMNS:
            raise ValueError(
                "checkpoint edge features differ from this code's EDGE_COLUMNS; retrain the model"
            )
        self.device = torch.device(device)
        self.model = model.to(self.device).eval()
        self.config = config
        self.ledger = ledger
        gk = {f.name for f in fields(GraphConfig)}
        gc = {k: v for k, v in config.get("graph_config", {}).items() if k in gk}
        gc["drop_features"] = tuple(gc.get("drop_features", ()))
        self.graph_cfg = GraphConfig(**gc)
        self.classify_cfg = ClassifyConfig(**config.get("classify_config", {}))
        self.name = name or f"gnn:{config.get('name', 'edgesage')}"
        self.ig_steps = int(ig_steps)
        self.warnings: list[str] = []
        self._bound: Anchor | None = None
        self._victims: dict[str, list[str]] | None = None
        self._cache: tuple[tuple, _Result] | None = None

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, path: str | Path, ledger: Ledger | None = None, device: str = "cpu", **kw) -> GnnScorer:
        dev = resolve_device(device)
        model, cfg = load_checkpoint(path, dev)
        return cls(model, cfg, ledger, device=dev, **kw)

    @classmethod
    def for_trace(cls, source: str | Path | GnnScorer, ledger: Ledger, trace: TraceResult, **kw) -> GnnScorer:
        scorer = source if isinstance(source, GnnScorer) else cls.load(source, ledger=ledger, **kw)
        scorer.ledger = ledger
        return scorer.bind(trace)

    def bind(self, trace: TraceResult) -> GnnScorer:
        """Use this trace's anchors (exact ``window_start`` / ``window_end`` / layers) for its accounts."""
        self._bound = anchor_from_trace(trace)
        self._cache = None
        return self

    def unbind(self) -> GnnScorer:
        self._bound = None
        self._cache = None
        return self

    # ------------------------------------------------------------------ protocol
    def score(self, features: pd.DataFrame) -> pd.Series:
        if features.empty:
            return pd.Series(dtype=float, index=features.index, name="score")
        res = self._run(features)
        return pd.Series(1.0 / (1.0 + np.exp(-res.logits)), index=features.index, name="score").clip(0.0, 1.0)

    def logits(self, features: pd.DataFrame) -> pd.Series:
        """Raw log-odds per row (``score`` is their sigmoid)."""
        return pd.Series(self._run(features).logits, index=features.index, name="logit")

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        if features.empty:
            return pd.DataFrame(columns=list(CONTRIBUTION_COLUMNS), index=features.index, dtype=float)
        res = self._run(features)
        if res.contributions is None:
            res.contributions = self._integrated_gradients(res, features.index)
        return res.contributions.copy()

    # ------------------------------------------------------------------ service entry point
    def score_accounts(
        self,
        account_ids: Sequence[str] | None = None,
        *,
        as_of: datetime | None = None,
        ncrp_ids: Sequence[str] | None = None,
        with_contributions: bool = True,
    ) -> tuple[pd.Series, pd.DataFrame | None, pd.DataFrame, Anchor | None]:
        """Score accounts without a precomputed feature frame: (scores, contributions, features, anchor).

        With ``ncrp_ids`` the case is traced (``trace_case``) and scored exactly like ``classify_trace`` would;
        otherwise features are computed with ``as_of`` (given or inferred from complaints) and a burst window from
        each account's first credit at or after ``as_of``."""
        if self.ledger is None:
            raise ValueError("score_accounts needs a ledger: GnnScorer.load(path, ledger=...)")
        if ncrp_ids:
            from wapsi.graph.trace import trace_case

            trace = trace_case(
                self.ledger, list(ncrp_ids), TraceConfig(window_hours=self.graph_cfg.horizon_hours)
            )
            anchor = anchor_from_trace(trace)
            feats = trace_seed_features(self.ledger, trace, self.classify_cfg)
            extra = [a for a in (account_ids or []) if a not in feats.index]
            if extra:
                more = account_features(
                    self.ledger,
                    extra,
                    anchor.as_of,
                    self.classify_cfg,
                    seed_accounts=set(anchor.trail),
                    burst_start=anchor.as_of,
                )
                feats = pd.concat([feats, more])
        else:
            ids = list(dict.fromkeys(account_ids or []))
            if not ids:
                raise ValueError("account_ids or ncrp_ids required")
            anchor = self._resolve_anchor(ids, as_of)
            burst = self._first_credit(ids, anchor) if anchor is not None else None
            at = anchor.as_of if anchor is not None else datetime.now(UTC)
            feats = account_features(
                self.ledger,
                ids,
                at,
                self.classify_cfg,
                seed_accounts=set(anchor.trail) if anchor else None,
                burst_start=burst or at,
            )
        res = self._run(feats, anchor=anchor)
        scores = pd.Series(1.0 / (1.0 + np.exp(-res.logits)), index=feats.index, name="score")
        contribs = None
        if with_contributions:
            if res.contributions is None:
                res.contributions = self._integrated_gradients(res, feats.index)
            contribs = res.contributions.copy()
        if account_ids:
            keep = [a for a in account_ids if a in scores.index]
            scores = scores.loc[keep]
            contribs = contribs.loc[keep] if contribs is not None else None
        return scores, contribs, feats, res.anchor

    # ------------------------------------------------------------------ internals
    def _key(self, features: pd.DataFrame, anchor: Anchor | None) -> tuple:
        vals = np.ascontiguousarray(features.reindex(columns=list(FEATURE_COLUMNS)).to_numpy(dtype=float))
        digest = hashlib.sha1(vals.tobytes()).hexdigest()
        a = (anchor.as_of.isoformat(), anchor.end.isoformat()) if anchor else None
        b = (self._bound.as_of.isoformat(), self._bound.end.isoformat()) if self._bound else None
        return tuple(map(str, features.index)), digest, a, b, id(self.ledger)

    def _run(self, features: pd.DataFrame, anchor: Anchor | None = None) -> _Result:
        key = self._key(features, anchor)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        ids = [str(a) for a in features.index]
        self.warnings = []
        if self.ledger is None:
            self.warnings.append("no ledger: accounts scored without graph context (isolated nodes)")
            g, anchor = isolated_graph(features, self.graph_cfg.drop_features), None
        else:
            anchor = anchor or self._resolve_anchor(ids, None)
            if anchor is None:
                _, end = world_bounds(self.ledger)
                self.warnings.append("no complaint anchor found: using the world end as as_of")
                anchor = Anchor(as_of=ensure_utc(end), end=ensure_utc(end), trail=frozenset(ids))
            g = build_local_graph(
                self.ledger,
                features,
                anchor.as_of,
                end=anchor.end,
                trail_accounts=anchor.trail,
                cfg=self.graph_cfg,
                classify_cfg=self.classify_cfg,
            )
        pos = {a: i for i, a in enumerate(g.node_ids)}
        positions = np.array([pos[a] for a in ids], dtype=np.int64)
        with torch.no_grad():
            logits = (
                self.model(
                    g.x.to(self.device),
                    g.edge_index.to(self.device),
                    g.edge_attr.to(self.device),
                    g.edge_time.to(self.device),
                )
                .float()
                .cpu()
                .numpy()
            )
        res = _Result(graph=g, positions=positions, logits=logits[positions].astype(float), anchor=anchor)
        self._cache = (key, res)
        return res

    def _resolve_anchor(self, ids: Sequence[str], as_of: datetime | None) -> Anchor | None:
        idset = set(ids)
        if as_of is not None:
            as_of = ensure_utc(as_of)
            return Anchor(
                as_of=as_of, end=as_of + timedelta(hours=self.graph_cfg.horizon_hours), trail=frozenset(idset)
            )
        if self._bound is not None and idset <= set(self._bound.burst or {}):
            return self._bound
        return self._anchor_from_complaints(idset)

    def _anchor_from_complaints(self, idset: set[str]) -> Anchor | None:
        ledger = self.ledger
        if ledger is None:
            return None
        if self._victims is None:
            self._victims = {}
            for c in ledger.complaints():
                self._victims.setdefault(c.victim_account, []).append(c.ncrp_id)
        times, victims = [], set()
        for acct in sorted(idset):
            for n in self._victims.get(acct, []):
                hits = [t for t in ledger.complaint_transactions(n) if t.to_account in idset]
                if hits:
                    victims.add(acct)
                    times.extend(ensure_utc(t.ts) for t in hits)
        if not times:
            return None
        times.sort()
        end = trace_window_end(times[0], times[-1], self.graph_cfg.horizon_hours)
        return Anchor(as_of=times[0], end=end, trail=frozenset(idset - victims))

    def _first_credit(self, ids: Sequence[str], anchor: Anchor) -> dict[str, datetime]:
        ledger = self.ledger
        home = ledger.home_bank_id()
        accts = ledger.accounts(ids)
        bank = {a: (accts[a].bank_id if a in accts else a.split("-", 1)[0]) for a in ids}
        tx = pull_transactions(
            ledger,
            [a for a in ids if bank[a] == home],
            [a for a in ids if bank[a] != home],
            anchor.as_of,
            anchor.end,
        )
        out = {a: anchor.as_of for a in ids}
        if not tx.empty:
            cred = tx[tx["dst"].isin(set(ids)) & (tx["src"] != tx["dst"])]
            for a, ts in cred.groupby("dst")["ts"].min().items():
                out[str(a)] = datetime.fromtimestamp(float(ts), tz=UTC)
        return out

    @torch.no_grad()
    def _bias_logit(self) -> float:
        m, dev = self.model, self.device
        z = torch.zeros(1, m.cfg.node_dim, device=dev)
        ei = torch.zeros(2, 0, dtype=torch.long, device=dev)
        return float(
            m.forward_normalized(
                z, ei, torch.zeros(0, m.cfg.edge_dim, device=dev), torch.zeros(0, device=dev)
            )
        )

    def _integrated_gradients(self, res: _Result, index: Iterable) -> pd.DataFrame:
        m, dev, g = self.model, self.device, res.graph
        x = g.x.to(dev)
        ei, ea, et = g.edge_index.to(dev), g.edge_attr.to(dev), g.edge_time.to(dev)
        n, steps = x.shape[0], max(self.ig_steps, 1)
        k = (
            steps + 1
        )  # midpoint Riemann steps + the alpha = 0 copy (baseline own-features, real neighbourhood)
        alphas = torch.cat([(torch.arange(steps, dtype=torch.float32) + 0.5) / steps, torch.zeros(1)]).to(dev)
        big_ei = torch.cat([ei + j * n for j in range(k)], dim=1)
        big_ea, big_et = ea.repeat(k, 1), et.repeat(k)
        with torch.no_grad():
            xn = m.normalize_x(x)
        bias = self._bias_logit()
        rows = []
        for p, logit in zip(res.positions.tolist(), res.logits.tolist(), strict=True):
            inp = (xn[p].unsqueeze(0) * alphas.unsqueeze(1)).detach().requires_grad_(True)
            idx = p + torch.arange(k, device=dev) * n
            big_x = xn.repeat(k, 1).index_put((idx,), inp)
            out = m.forward_normalized(big_x, big_ei, big_ea, big_et)[idx]
            (grad,) = torch.autograd.grad(out[:steps].sum(), inp)
            attr = (xn[p] * grad[:steps].mean(0)).detach().cpu().numpy().astype(float)
            base_ctx = float(out[steps].detach())
            rows.append([*attr, base_ctx - bias, logit - base_ctx - float(attr.sum()), bias])
        return pd.DataFrame(
            rows, index=pd.Index(list(index), name="account_id"), columns=list(CONTRIBUTION_COLUMNS)
        )

    # ------------------------------------------------------------------ introspection
    def last_graph(self) -> CaseGraph | None:
        return self._cache[1].graph if self._cache else None

    def last_anchor(self) -> Anchor | None:
        return self._cache[1].anchor if self._cache else None
