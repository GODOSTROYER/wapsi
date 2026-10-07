"""Mule scorers.

``Scorer`` is the extension point: anything with ``name``, ``score(features_df)`` and
``contributions(features_df)`` can be passed to ``wapsi.classify.classify_trace(model=...)``.

* ``HeuristicScorer`` (default): transparent hand-set logistic model, deterministic, no dependencies.
* ``XgbScorer``: gradient-boosted trees trained by ``wapsi.classify.train`` (optional ``ml`` extra).
* GraphSAGE / GNN scorer: reserved for the AMD build window (12-18 Oct 2026, ``editions/amd/``). It should
  implement this same protocol. ``score`` only receives the per-account feature frame (index ``account_id``), so a
  graph model should take its graph context in its constructor (e.g. ``GnnScorer.for_trace(ledger, trace)``) and
  look rows up by index. ``contributions`` must return log-odds-like per-feature (or per-neighbour, mapped to a
  feature name) contributions so ``wapsi.classify.explain`` can render reasons.

Optional trace hook (``TraceAwareScorer``): a scorer that needs the case itself (window, layers, burst anchors), such
as a graph model, can define ``bind_trace(ledger, trace) -> Scorer``. ``classify_trace`` calls it once per case,
before scoring, and scores with the scorer it returns (``self`` after binding, or a bound copy so a shared scorer is
not mutated). Scorers without the method are used as given, so the hook changes nothing for them.

Contribution convention: DataFrame with the same index as ``features_df``; one column per feature name plus a
``bias`` column; values are additive log-odds; positive pushes towards MULE, negative towards a legitimate profile.
A graph model may add a ``graph_neighbourhood`` column (``GRAPH_NEIGHBOURHOOD``): what the account's position in the
transaction graph adds on top of its own features. ``wapsi.classify.explain`` renders it as one sentence; any other
extra column (e.g. an integration residual) is ignored by the reasons but still counts towards the row's sum.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from wapsi.classify.features import FEATURE_COLUMNS
from wapsi.models import Role

if TYPE_CHECKING:  # pragma: no cover
    import xgboost as xgb

    from wapsi.models import TraceResult
    from wapsi.ports import Ledger

BIAS = "bias"
GRAPH_NEIGHBOURHOOD = "graph_neighbourhood"  # optional contribution column of graph models
TRACE_HOOK = "bind_trace"  # optional scorer method, see ``TraceAwareScorer``


@runtime_checkable
class Scorer(Protocol):
    name: str

    def score(self, features: pd.DataFrame) -> pd.Series:
        """Mule score in [0, 1] per row (index preserved)."""
        ...

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        """Additive log-odds contributions per feature (+ ``bias``); positive = towards MULE."""
        ...


@runtime_checkable
class TraceAwareScorer(Scorer, Protocol):
    """A ``Scorer`` that wants the case's ``TraceResult`` before scoring (optional extension)."""

    def bind_trace(self, ledger: Ledger, trace: TraceResult) -> Scorer:
        """Return the scorer to use for this trace's accounts (``self`` after binding, or a bound copy)."""
        ...


def scorer_for_trace(scorer: Scorer, ledger: Ledger, trace: TraceResult) -> Scorer:
    """``scorer.bind_trace(ledger, trace)`` if the scorer has the hook (a ``None`` result keeps it), else it."""
    hook = getattr(scorer, TRACE_HOOK, None)
    if not callable(hook):
        return scorer
    bound = hook(ledger, trace)
    return scorer if bound is None else bound


# --------------------------------------------------------------------------- heuristic


def _clip01(x: pd.Series) -> pd.Series:
    return x.clip(lower=0.0, upper=1.0)


@dataclass(frozen=True)
class Term:
    feature: str
    weight: float  # log-odds added when the transformed value is 1.0
    transform: Callable[[pd.Series], pd.Series]  # maps the raw feature to [0, 1]
    note: str


HEURISTIC_SCORE_RANGE = (0.01, 0.99)  # a hand-set heuristic never claims certainty

# Hand-set log-odds weights. Mule indicators follow RBI FRM early-warning patterns and I4C mule typologies (new
# accounts, rapid pass-through, cash/crypto exits, shared devices, external risk signals). Legitimate-profile
# indicators (tenure, salary/rent history, delivered invoices, GSTIN) pull towards the SOP Para 5(xii) band.
# Calibration intent (default thresholds 0.6 / 0.35), checked in tests/classify/test_classify_heuristic.py:
#   * a 10-day-old account that only received money scores ~0.58 (UNKNOWN: officer review);
#   * add fast pass-through, cash-out, shared devices or external risk signals and it becomes MULE;
#   * a long-standing salaried account that kept the money scores < 0.1 (INNOCENT_HOLDER);
#   * the same account forwarding 95% within minutes stays UNKNOWN; without salary history it becomes MULE.
#   * a genuine seller paid with victim money (years old, a base of past customers, turnover well above the
#     payment, rent paid to its usual landlord) scores < 0.35 (INNOCENT_HOLDER), even with a middling external
#     score. The payer-base and turnover terms together (max -1.5) cannot move an old account without salary
#     history that forwards 95% within minutes below 0.35 (it stays UNKNOWN), and
#     ``wapsi.classify.rules.innocent_blockers`` caps any such account at UNKNOWN whatever its score
#     (tests/classify/test_classify_counterparty.py).
HEURISTIC_BIAS = -0.4


def _new(x: pd.Series) -> pd.Series:
    return _clip01(1.0 - x / 90.0)  # 1 at 0 days, 0 from 90 days


def _established(x: pd.Series) -> pd.Series:
    return _clip01((x - 180.0) / (3 * 365.0))  # 0 up to 6 months, 1 from ~3.5 years


def _fast(x: pd.Series) -> pd.Series:
    return np.exp(-x.clip(lower=0.0) / 120.0)  # 1 at 0 minutes, 0.61 at 1 h, 0.14 at 4 h


def _per(n: float, offset: float = 0.0) -> Callable[[pd.Series], pd.Series]:
    return lambda x: _clip01((x - offset) / n)


# fmt: off
HEURISTIC_TERMS: tuple[Term, ...] = (
    # feature                             weight  transform       note
    Term("age_days",                          0.8, _new,          "opened recently (0-90 days)"),
    Term("age_days",                         -1.2, _established,  "long-standing (6 months to ~3.5 years)"),
    Term("kyc_min",                           0.5, _clip01,       "minimum-KYC account"),
    Term("kyc_vkyc",                          0.2, _clip01,       "remotely opened via video KYC"),
    Term("pass_through_ratio_24h",            2.0, _clip01,       "share of burst inflow sent on in the window"),
    Term("median_forward_minutes",            1.4, _fast,         "fast forwarding (2 h decay)"),
    Term("n_distinct_payers_7d",              0.8, _per(4, 1),    "fan-in: 2+ payers, full at 5"),
    Term("n_distinct_payees_7d",              0.5, _per(5, 2),    "fan-out: 3+ payees, full at 7"),
    Term("night_share",                       0.8, _clip01,       "share of activity 23:00-05:00 IST"),
    Term("atm_share_24h",                     1.0, _clip01,       "cash-out share of burst outflow"),
    Term("crypto_share_24h",                  1.2, _clip01,       "crypto-exit share of burst outflow"),
    Term("device_shared_count",               0.8, _per(3),       "device shared with other accounts, full at 3"),
    Term("shared_device_with_seed_accounts",  1.5, _per(2),       "device shared with trail accounts, full at 2"),
    Term("dormant_then_burst",                1.2, _clip01,       "dormant account suddenly active"),
    Term("round_amount_share",                0.3, _clip01,       "round-amount transfers"),
    Term("fri_risk",                          1.5, _per(3),       "DoT FRI of the linked mobile (very high = 1)"),
    Term("mulehunter_score",                  2.0, _clip01,       "MuleHunter-style score"),
    Term("ncrp_prior_reports",                1.2, _per(2),       "earlier NCRP reports, full at 2"),
    Term("history_days",                     -0.6, _per(90),      "observed history before the fraud, full at 90 d"),
    Term("salary_credits_90d",               -0.8, _per(2),       "salary credits, full at 2"),
    Term("recurring_rent_like_90d",          -1.5, _per(2),       "monthly similar-amount credits, full at 2"),
    Term("merchant_delivered_share",         -1.5, _clip01,       "inflow backed by delivered invoices"),
    Term("gst_active",                       -0.5, _clip01,       "active GSTIN"),
    Term("is_intermediary",                  -3.0, _clip01,       "nodal / pool / escrow account"),
    # Counterparty history (what a genuine seller / landlord paid with victim money looks like). Rationale:
    # - a base of many distinct past payers is a shop's or landlord's customer book; 1-2 payers (family,
    #   employer) is normal for anyone and earns nothing; mule accounts are fresh or dormant with no such base
    Term("hist_distinct_payers_90d",         -1.0, _per(8, 2),    "established payer base: 3+ past payers, full at 10"),
    # - a payment that is small next to the account's usual 90-day turnover is ordinary business; mules are
    #   kept near empty and the victim money dwarfs everything before it (no credit up to 1x, full at 3x)
    Term("hist_credit_turnover_ratio",       -0.5, _per(2, 1),    "90-day turnover above the payment, full at 3x"),
    # - paying the usual landlord / supplier is spending, not layering; offsets most (not all) of the
    #   pass-through weight for that part of the outflow
    Term("known_payee_out_ratio_24h",        -1.5, _clip01,       "outflow to counterparties paid before the fraud"),
)
# fmt: on


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


class HeuristicScorer:
    """Transparent logistic score: ``sigmoid(bias + sum(weight * transform(feature)))``, clipped to
    ``HEURISTIC_SCORE_RANGE``. Deterministic; contributions are the unclipped log-odds terms."""

    name = "heuristic-v2"

    def __init__(self, terms: Sequence[Term] = HEURISTIC_TERMS, bias: float = HEURISTIC_BIAS) -> None:
        self.terms = tuple(terms)
        self.bias = float(bias)

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        cols = list(FEATURE_COLUMNS) + [c for c in features.columns if c not in FEATURE_COLUMNS]
        out = pd.DataFrame(0.0, index=features.index, columns=cols + [BIAS])
        for term in self.terms:
            raw = features[term.feature] if term.feature in features else pd.Series(0.0, index=features.index)
            val = term.transform(raw.astype(float).fillna(0.0))
            out[term.feature] = out[term.feature] + term.weight * np.asarray(val, dtype=float)
        out[BIAS] = self.bias
        return out

    def score(self, features: pd.DataFrame) -> pd.Series:
        z = self.contributions(features).sum(axis=1).to_numpy(dtype=float)
        lo, hi = HEURISTIC_SCORE_RANGE
        return pd.Series(np.clip(_sigmoid(z), lo, hi), index=features.index, name="score")

    def describe(self) -> list[dict]:
        """Weights table for docs / UI."""
        return [{"feature": t.feature, "weight": t.weight, "note": t.note} for t in self.terms] + [
            {"feature": BIAS, "weight": self.bias, "note": "intercept"}
        ]


# --------------------------------------------------------------------------- xgboost

CLASSES_ATTR = "wapsi_classes"
NAME_ATTR = "wapsi_name"


def _xgb():
    try:
        import xgboost
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("XgbScorer needs the optional 'ml' extra: uv sync --extra ml") from exc
    return xgboost


class XgbScorer:
    """Multi-class XGBoost role model. ``score`` = P(MULE); ``contributions`` = TreeSHAP values of the MULE margin.

    The model is saved as a single XGBoost JSON file; the class order and name are stored as booster attributes.
    """

    def __init__(self, booster: xgb.Booster, classes: Sequence[str], name: str = "xgb") -> None:
        self.booster = booster
        self.classes = [str(c) for c in classes]
        self.name = name
        self.feature_names = list(booster.feature_names or FEATURE_COLUMNS)

    # -- persistence
    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.booster.set_attr(**{CLASSES_ATTR: json.dumps(self.classes), NAME_ATTR: self.name})
        self.booster.save_model(str(path))
        return path

    @classmethod
    def load(cls, path: str | Path) -> XgbScorer:
        xgb = _xgb()
        path = Path(path)
        booster = xgb.Booster()
        booster.load_model(str(path))
        classes_raw = booster.attr(CLASSES_ATTR)
        if not classes_raw:
            raise ValueError(f"{path} is not a Wapsi role model (missing '{CLASSES_ATTR}' attribute)")
        return cls(booster, json.loads(classes_raw), name=booster.attr(NAME_ATTR) or f"xgb:{path.stem}")

    # -- inference
    def _dmatrix(self, features: pd.DataFrame):
        xgb = _xgb()
        x = features.reindex(columns=self.feature_names).astype(float).fillna(0.0)
        return xgb.DMatrix(x.to_numpy(), feature_names=self.feature_names)

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        if features.empty:
            return pd.DataFrame(columns=self.classes, index=features.index, dtype=float)
        p = np.asarray(self.booster.predict(self._dmatrix(features)), dtype=float)
        p = p.reshape(len(features), -1)
        return pd.DataFrame(p, index=features.index, columns=self.classes)

    def predict_role(self, features: pd.DataFrame) -> pd.Series:
        proba = self.predict_proba(features)
        return proba.idxmax(axis=1) if not proba.empty else pd.Series(dtype=object, index=features.index)

    def score(self, features: pd.DataFrame) -> pd.Series:
        proba = self.predict_proba(features)
        if Role.MULE.value not in proba.columns:
            return pd.Series(0.0, index=features.index, name="score")
        return proba[Role.MULE.value].clip(0.0, 1.0).rename("score")

    def contributions(self, features: pd.DataFrame) -> pd.DataFrame:
        cols = self.feature_names + [BIAS]
        if features.empty or Role.MULE.value not in self.classes:
            return pd.DataFrame(0.0, index=features.index, columns=cols)
        raw = np.asarray(self.booster.predict(self._dmatrix(features), pred_contribs=True), dtype=float)
        if raw.ndim == 3:  # (rows, classes, features + 1)
            raw = raw[:, self.classes.index(Role.MULE.value), :]
        return pd.DataFrame(raw, index=features.index, columns=cols)
