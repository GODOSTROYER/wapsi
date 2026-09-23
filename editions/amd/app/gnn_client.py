"""``GnnHttpScorer``: the ``wapsi.classify.Scorer`` protocol over the GNN scoring service, with a safe fallback.

The GraphSAGE service itself lives in ``editions/amd/gnn/service.py`` (owned by the GNN work stream; port 8081,
``POST /score``). This client only needs the contract below and degrades to the transparent
``HeuristicScorer`` whenever the service is unreachable, slow, or answers with something unusable, so a case is
never blocked by the GNN.

Request (JSON)::

    {"account_ids": ["HB01-...", ...],
     "feature_columns": ["account_age_days", ...],
     "features": [{"account_id": "HB01-...", "account_age_days": 11.0, ...}, ...]}

Accepted responses (any of)::

    {"scores": {"HB01-...": 0.91, ...}, "contributions": {"HB01-...": {"feature": 0.4, "bias": -1.2}}, "model": "graphsage-v1"}
    {"scores": [{"account_id": "HB01-...", "score": 0.91, "contributions": {...}}, ...], "model": "..."}
    [{"account_id": "HB01-...", "score": 0.91}, ...]

Scores must be finite and are clipped to [0, 1]. Accounts the service does not score (e.g. outside its graph)
get the heuristic score for that row. When the service returns no contributions, the heuristic's per-feature
contributions are used for the reasons and the ``bias`` column is shifted so each row still sums to the GNN's
log-odds (the convention ``wapsi.classify.explain`` expects). After a failure the service is skipped for
``retry_after_failure_s`` seconds (circuit breaker) so a dead service costs one timeout, not one per case.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import pandas as pd

from wapsi.classify.scoring import BIAS, HeuristicScorer, Scorer

log = logging.getLogger("wapsi.amd.gnn_client")

SOURCE_GNN, SOURCE_MIXED, SOURCE_FALLBACK = "gnn", "gnn+heuristic", "heuristic-fallback"


class GnnUnavailable(RuntimeError):
    pass


@dataclass
class GnnStats:
    calls: int = 0
    gnn_ok: int = 0
    failures: int = 0
    skipped_open_circuit: int = 0
    fallback_rows: int = 0
    last_source: str = ""
    last_error: str = ""
    last_latency_s: float = 0.0
    latencies_s: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies_s)
        return {
            "calls": self.calls,
            "gnn_ok": self.gnn_ok,
            "failures": self.failures,
            "skipped_open_circuit": self.skipped_open_circuit,
            "fallback_rows": self.fallback_rows,
            "last_source": self.last_source,
            "last_error": self.last_error,
            "p50_latency_s": round(lat[len(lat) // 2], 4) if lat else None,
        }


@dataclass
class _Result:
    scores: pd.Series
    contributions: pd.DataFrame
    source: str
    model: str


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _features_payload(features: pd.DataFrame) -> dict[str, Any]:
    by_id = json.loads(features.to_json(orient="index"))  # NaN -> null, numpy -> JSON types
    ids = [str(i) for i in features.index]
    return {
        "account_ids": ids,
        "feature_columns": [str(c) for c in features.columns],
        "features": [{"account_id": i, **(by_id.get(i) or {})} for i in ids],
    }


def _parse(data: Any) -> tuple[dict[str, float], dict[str, dict[str, float]], str]:
    """(scores, contributions, model) from any accepted response shape; raises GnnUnavailable otherwise."""
    model = ""
    contribs: dict[str, dict[str, float]] = {}
    rows: Any = data
    if isinstance(data, dict):
        model = str(data.get("model") or data.get("model_name") or "")
        rows = data.get("scores")
        raw_contribs = data.get("contributions")
        if isinstance(raw_contribs, dict):
            contribs = {str(k): dict(v) for k, v in raw_contribs.items() if isinstance(v, dict)}
    scores: dict[str, float] = {}
    if isinstance(rows, dict):
        items = [(k, v) for k, v in rows.items()]
    elif isinstance(rows, list):
        items = []
        for r in rows:
            if not isinstance(r, dict) or "account_id" not in r:
                raise GnnUnavailable("score rows must be objects with account_id")
            items.append((r["account_id"], r.get("score", r.get("mule_score"))))
            if isinstance(r.get("contributions"), dict):
                contribs[str(r["account_id"])] = dict(r["contributions"])
    else:
        raise GnnUnavailable("response has no scores")
    for k, v in items:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise GnnUnavailable(f"non-numeric score for {k}") from exc
        if not math.isfinite(f):
            raise GnnUnavailable(f"non-finite score for {k}")
        scores[str(k)] = min(max(f, 0.0), 1.0)
    return scores, contribs, model


class GnnHttpScorer:
    """Implements ``wapsi.classify.Scorer``."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        fallback: Scorer | None = None,
        retry_after_failure_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.url = f"{self.base_url}/score"
        self.fallback: Scorer = fallback if fallback is not None else HeuristicScorer()
        self.retry_after_failure_s = float(retry_after_failure_s)
        self._client = httpx.Client(timeout=timeout_s, transport=transport, headers=headers)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: tuple[str, _Result] | None = None
        self._open_until = 0.0
        self.stats = GnnStats()
        self.model_name = "graphsage"

    def __repr__(self) -> str:
        return f"GnnHttpScorer({self.base_url!r})"

    def close(self) -> None:
        self._client.close()

    @property
    def name(self) -> str:
        src = self.stats.last_source
        if src == SOURCE_FALLBACK:
            return f"{self.fallback.name} (gnn unavailable)"
        if src == SOURCE_MIXED:
            return f"gnn:{self.model_name}+{self.fallback.name}"
        return f"gnn:{self.model_name}"

    # -- Scorer protocol
    def score(self, features: pd.DataFrame) -> pd.Series:
        return self._result(features).scores.copy()

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        return self._result(features).contributions.copy()

    # -- internals
    @staticmethod
    def _key(features: pd.DataFrame) -> str:
        h = hashlib.sha256()
        h.update("|".join(map(str, features.index)).encode("utf-8"))
        h.update("|".join(map(str, features.columns)).encode("utf-8"))
        h.update(pd.util.hash_pandas_object(features.fillna(0), index=True).to_numpy().tobytes())
        return h.hexdigest()

    def _result(self, features: pd.DataFrame) -> _Result:
        key = self._key(features)
        with self._lock:
            if self._cache is not None and self._cache[0] == key:
                return self._cache[1]
            res = self._compute(features)
            self._cache = (key, res)
            return res

    def _call(self, features: pd.DataFrame) -> tuple[dict[str, float], dict[str, dict[str, float]], str]:
        t = time.perf_counter()
        try:
            resp = self._client.post(self.url, json=_features_payload(features))
        except httpx.HTTPError as exc:
            raise GnnUnavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            self.stats.last_latency_s = time.perf_counter() - t
        if resp.status_code >= 400:
            raise GnnUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GnnUnavailable("non-JSON response") from exc
        return _parse(data)

    def _compute(self, features: pd.DataFrame) -> _Result:
        self.stats.calls += 1
        heur_scores = self.fallback.score(features)
        heur_contribs = self.fallback.contributions(features)
        if features.empty:
            self.stats.last_source = SOURCE_GNN
            return _Result(heur_scores, heur_contribs, SOURCE_GNN, self.model_name)
        now = self._clock()
        if now < self._open_until:
            self.stats.skipped_open_circuit += 1
            return self._fallback_result(heur_scores, heur_contribs, "circuit open after a recent failure")
        try:
            scores, contribs, model = self._call(features)
        except GnnUnavailable as exc:
            self._open_until = now + self.retry_after_failure_s
            self.stats.failures += 1
            log.warning("GNN service %s unavailable (%s); using %s", self.url, exc, self.fallback.name)
            return self._fallback_result(heur_scores, heur_contribs, str(exc))
        self._open_until = 0.0
        self.stats.gnn_ok += 1
        self.stats.latencies_s.append(self.stats.last_latency_s)
        if model:
            self.model_name = model.removeprefix("gnn:")
        ids = [str(i) for i in features.index]
        missing = [i for i in ids if i not in scores]
        if len(missing) == len(ids):
            self.stats.failures += 1
            return self._fallback_result(heur_scores, heur_contribs, "service scored none of the accounts")
        self.stats.fallback_rows += len(missing)
        out_scores = pd.Series(
            [scores.get(str(i), float(heur_scores.loc[i])) for i in features.index],
            index=features.index,
            name="score",
            dtype=float,
        )
        out_contribs = heur_contribs.copy()
        # the service may add columns (e.g. graph_neighbourhood, ig_residual, g_* degree stats) that keep its
        # decomposition additive; keep them (0 for other rows) so each row still sums to the GNN's logit
        extra = sorted({str(k) for row in contribs.values() for k in row} - set(map(str, out_contribs.columns)))
        for col in extra:
            out_contribs[col] = 0.0
        for i in features.index:
            sid = str(i)
            if sid not in scores:
                continue
            row = contribs.get(sid)
            if row:
                vals = {c: float(row.get(c, 0.0) or 0.0) for c in out_contribs.columns}
                out_contribs.loc[i] = pd.Series(vals)
            else:
                shift = _logit(scores[sid]) - float(out_contribs.loc[i].sum())
                out_contribs.loc[i, BIAS] = float(out_contribs.loc[i, BIAS]) + shift
        out_contribs = out_contribs.replace([np.inf, -np.inf], 0.0).fillna(0.0)
        source = SOURCE_MIXED if missing else SOURCE_GNN
        self.stats.last_source = source
        self.stats.last_error = ""
        return _Result(out_scores, out_contribs, source, self.model_name)

    def _fallback_result(self, scores: pd.Series, contribs: pd.DataFrame, reason: str) -> _Result:
        self.stats.last_source = SOURCE_FALLBACK
        self.stats.last_error = reason[:300]
        self.stats.fallback_rows += len(scores)
        return _Result(scores, contribs, SOURCE_FALLBACK, self.fallback.name)
