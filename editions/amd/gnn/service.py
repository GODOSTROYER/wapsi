"""GNN scoring service (docker-compose service ``gnn``, port 8081).

    uv run python -m editions.amd.gnn.service            # or: uvicorn editions.amd.gnn.service:app --port 8081

Configuration (environment; the first name wins, the second is the name editions/amd/docker-compose.yml sets):

| Variable | Default | Meaning |
|---|---|---|
| ``WAPSI_GNN_CHECKPOINT`` / ``WAPSI_GNN_ARTIFACTS`` | ``editions/amd/gnn/checkpoints`` | checkpoint dir (with ``config.json``) or a dir of checkpoints (newest wins, or ``WAPSI_GNN_MODEL`` names one) |
| ``WAPSI_GNN_WORLD`` / ``WAPSI_WORLD`` | ``data/generated/demo`` | world directory for ``ParquetLedger`` (graph context) |
| ``WAPSI_GNN_DEVICE`` | ``auto`` | ``auto`` / ``cpu`` / ``cuda`` (ROCm = ``cuda``) |
| ``WAPSI_GNN_THREADS`` | 4 on CPU | torch CPU threads |
| ``PORT`` | 8081 | listen port (``__main__`` only) |

``POST /score`` accepts three request shapes (JSON), all answered with the same response:

1. ``{"account_ids": [...], "features": [{"account_id": "...", "<feature>": 1.0, ...}], "feature_columns": [...]}``
   - the feature frame ``classify_trace`` hands a scorer (what ``editions/amd/app/gnn_client.py`` sends). Scored
   exactly like the in-process ``GnnScorer``: the case anchor is inferred from the complaints of the victim accounts
   among the rows.
2. ``{"ncrp_ids": [...], "account_ids": [...]?}`` - trace the case and score its accounts (optionally a subset).
3. ``{"account_ids": [...], "as_of": "2026-09-01T13:26:39Z"?}`` - compute features for the accounts at ``as_of``
   (inferred from complaints when omitted).

Response::

    {"model": "gnn:<name>", "scores": {"<id>": 0.97, ...},
     "contributions": {"<id>": {"<feature>": 0.41, ..., "bias": -1.3}},   # FEATURE_COLUMNS + bias, additive log-odds
     "graph": {"<id>": {"graph_neighbourhood": 0.8, "degree_stats": 0.2, "ig_residual": 0.0, "isolated_bias": -2.1}},
     "anchor": {"as_of": "...", "end": "..."}, "warnings": [...]}

``contributions[id]`` sums to the account's logit: ``bias`` there already includes the graph terms listed under
``graph`` (neighbourhood effect, degree-statistic attributions, integration residual), so clients that only know the
core feature names (``wapsi.classify.explain``) stay consistent. ``GET /healthz`` reports model, device and world.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from editions.amd.gnn.graph import GRAPH_COLUMNS
from editions.amd.gnn.scorer import GRAPH_CONTEXT, IG_RESIDUAL, GnnScorer
from wapsi.classify.features import FEATURE_COLUMNS
from wapsi.classify.scoring import BIAS

MAX_ACCOUNTS = 2000
MAX_ID_LEN = 64


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_ids: list[str] = Field(default_factory=list, max_length=MAX_ACCOUNTS)
    features: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_ACCOUNTS)
    feature_columns: list[str] | None = None
    ncrp_ids: list[str] = Field(default_factory=list, max_length=100)
    as_of: datetime | None = None
    contributions: bool = True
    top_k: int = Field(default=0, ge=0, le=len(FEATURE_COLUMNS))  # 0 = every feature


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def resolve_checkpoint(path: str | Path, model_name: str | None = None) -> Path:
    """A checkpoint dir, or a dir of checkpoints (``model_name`` or the newest ``config.json`` wins)."""
    p = Path(path)
    if (p / "config.json").exists():
        return p
    if model_name and (p / model_name / "config.json").exists():
        return p / model_name
    found = (
        sorted(p.glob("*/config.json"), key=lambda f: f.stat().st_mtime, reverse=True) if p.is_dir() else []
    )
    if not found:
        raise FileNotFoundError(
            f"no GNN checkpoint under {p} (train one: python -m editions.amd.gnn.train ...)"
        )
    return found[0].parent


def _finite(x: float) -> float:
    x = float(x)
    return round(x, 6) if math.isfinite(x) else 0.0


class _State:
    def __init__(
        self, scorer: GnnScorer | None, checkpoint: str | None, world: str | None, device: str
    ) -> None:
        self.scorer = scorer
        self.checkpoint = checkpoint
        self.world = world
        self.device = device
        self.error: str | None = None
        self.lock = threading.RLock()

    def get(self) -> GnnScorer:
        with self.lock:
            if self.scorer is None:
                try:
                    from editions.amd.gnn.model import configure_cpu_threads, resolve_device
                    from wapsi.adapters.parquet_ledger import ParquetLedger

                    dev = resolve_device(self.device)
                    configure_cpu_threads(dev, int(_env("WAPSI_GNN_THREADS", default="0") or 0) or None)
                    ckpt = resolve_checkpoint(
                        self.checkpoint or "editions/amd/gnn/checkpoints", _env("WAPSI_GNN_MODEL")
                    )
                    ledger = ParquetLedger(self.world or "data/generated/demo")
                    self.scorer = GnnScorer.load(ckpt, ledger=ledger, device=str(dev))
                    self.checkpoint = str(ckpt)
                    self.error = None
                except Exception as exc:  # reported by /healthz and /score
                    self.error = f"{type(exc).__name__}: {exc}"
                    raise
            return self.scorer


def _response(
    scorer: GnnScorer, scores: pd.Series, contribs: pd.DataFrame | None, anchor, top_k: int
) -> dict:
    out: dict[str, Any] = {
        "model": scorer.name,
        "scores": {str(a): _finite(v) for a, v in scores.items()},
        "anchor": {"as_of": anchor.as_of.isoformat(), "end": anchor.end.isoformat()} if anchor else None,
        "warnings": list(scorer.warnings),
    }
    if contribs is not None:
        feats, graph = {}, {}
        for acct, row in contribs.iterrows():
            degree = float(sum(row.get(c, 0.0) for c in GRAPH_COLUMNS))
            extra = float(row.get(GRAPH_CONTEXT, 0.0)) + float(row.get(IG_RESIDUAL, 0.0)) + degree
            vals = {c: _finite(row.get(c, 0.0)) for c in FEATURE_COLUMNS}
            if top_k:
                keep = sorted(vals, key=lambda c: -abs(vals[c]))[:top_k]
                dropped = sum(v for c, v in vals.items() if c not in keep)
                vals = {c: vals[c] for c in keep}
                extra += dropped
            vals[BIAS] = _finite(float(row.get(BIAS, 0.0)) + extra)
            feats[str(acct)] = vals
            graph[str(acct)] = {
                "graph_neighbourhood": _finite(row.get(GRAPH_CONTEXT, 0.0)),
                "degree_stats": _finite(degree),
                "ig_residual": _finite(row.get(IG_RESIDUAL, 0.0)),
                "isolated_bias": _finite(row.get(BIAS, 0.0)),
            }
        out["contributions"], out["graph"] = feats, graph
    return out


def create_app(
    scorer: GnnScorer | None = None,
    *,
    checkpoint: str | None = None,
    world: str | None = None,
    device: str | None = None,
) -> FastAPI:
    """App factory. Tests inject a ready ``GnnScorer``; the container reads the environment and loads lazily."""
    state = _State(
        scorer,
        checkpoint or _env("WAPSI_GNN_CHECKPOINT", "WAPSI_GNN_ARTIFACTS"),
        world or _env("WAPSI_GNN_WORLD", "WAPSI_WORLD"),
        device or _env("WAPSI_GNN_DEVICE", default="auto"),
    )
    app = FastAPI(title="Wapsi GNN scorer", version="1")
    app.state.gnn = state

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            s = state.get()
        except Exception:
            return JSONResponse({"status": "error", "error": state.error}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "model": s.name,
                "checkpoint": state.checkpoint,
                "device": str(s.device),
                "world": state.world,
                "parameters": sum(p.numel() for p in s.model.parameters()),
            }
        )

    @app.post("/score")
    def score(req: ScoreRequest) -> dict:
        try:
            s = state.get()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"model not loaded: {state.error}") from exc
        ids = [a for a in req.account_ids if a]
        if any(len(a) > MAX_ID_LEN for a in ids):
            raise HTTPException(status_code=422, detail=f"account ids longer than {MAX_ID_LEN} characters")
        with state.lock:  # the scorer caches per-request graphs; serialise access
            try:
                if req.features:
                    rows = {str(r.get("account_id")): r for r in req.features if r.get("account_id")}
                    order = ids or list(rows)
                    missing = [a for a in order if a not in rows]
                    if missing:
                        raise HTTPException(status_code=422, detail=f"features missing for {missing[:5]}")
                    frame = (
                        pd.DataFrame(
                            [[rows[a].get(c) for c in FEATURE_COLUMNS] for a in order],
                            index=pd.Index(order, name="account_id"),
                            columns=list(FEATURE_COLUMNS),
                        )
                        .apply(pd.to_numeric, errors="coerce")
                        .fillna(0.0)
                        .astype(float)
                    )
                    s.unbind()
                    scores = s.score(frame)
                    contribs = s.contributions(frame) if req.contributions else None
                    return _response(s, scores, contribs, s.last_anchor(), req.top_k)
                if not ids and not req.ncrp_ids:
                    raise HTTPException(status_code=422, detail="account_ids, features or ncrp_ids required")
                scores, contribs, _, anchor = s.score_accounts(
                    ids or None,
                    as_of=req.as_of,
                    ncrp_ids=req.ncrp_ids or None,
                    with_contributions=req.contributions,
                )
                return _response(s, scores, contribs, anchor, req.top_k)
            except HTTPException:
                raise
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)[:300]) from exc

    return app


app = create_app()


def main() -> None:  # pragma: no cover - container entry point
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8081")))


if __name__ == "__main__":  # pragma: no cover
    main()
