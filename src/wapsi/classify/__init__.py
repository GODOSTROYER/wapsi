"""Account role classification: MULE / INNOCENT_HOLDER / MERCHANT / INTERMEDIARY / VICTIM / UNKNOWN.

``classify_trace`` pipeline:

1. Features (``wapsi.classify.features.account_features``) for every account of the trace, with
   ``as_of = trace.window_start`` (history is what the account looked like *before* the first victim transfer)
   and a per-account burst window ``[first_tainted_at, first_tainted_at + cfg.feature_window_hours]``
   (fallback ``window_start`` for accounts without a tainted receipt, e.g. victims). Anchoring the burst on each
   account's own first tainted receipt keeps deep-layer accounts, reached hours after ``window_start``, comparable
   with first-layer ones. Trail accounts (layer >= 1) are the seeds for ``shared_device_with_seed_accounts``.
2. Hard rules (``wapsi.classify.rules``): VICTIM, then INTERMEDIARY, then MERCHANT.
3. Otherwise the scorer (default ``HeuristicScorer``): score >= ``cfg.mule_threshold`` -> MULE,
   <= ``cfg.innocent_threshold`` -> INNOCENT_HOLDER, else UNKNOWN.
4. Safety guard (``wapsi.classify.rules.innocent_blockers``): an INNOCENT_HOLDER score becomes UNKNOWN when the
   account forwarded most of the money quickly to new counterparties, cashed it out, shares a device with the
   trail, woke from dormancy or was reported repeatedly. Uncertain cases go to the officer, not to a label.
5. Reasons: rule reasons, or the top indicators from the scorer's contributions (``wapsi.classify.explain``).

Heavy dependencies (xgboost, sklearn) and ``wapsi.graph`` are never imported at module import time.
"""

from __future__ import annotations

import math

from wapsi.classify.explain import explain, explain_blocked
from wapsi.classify.features import FEATURE_COLUMNS, account_features
from wapsi.classify.rules import RuleHit, hard_rules, innocent_blockers
from wapsi.classify.scoring import HeuristicScorer, Scorer, XgbScorer
from wapsi.config import ClassifyConfig
from wapsi.models import ClassificationResult, Role, TraceResult
from wapsi.ports import Ledger
from wapsi.timeutil import ensure_utc

__all__ = [
    "FEATURE_COLUMNS",
    "HeuristicScorer",
    "RuleHit",
    "Scorer",
    "XgbScorer",
    "account_features",
    "classify_trace",
    "hard_rules",
    "innocent_blockers",
    "role_from_score",
]


def role_from_score(score: float, cfg: ClassifyConfig) -> Role:
    if score >= cfg.mule_threshold:
        return Role.MULE
    if score <= cfg.innocent_threshold:
        return Role.INNOCENT_HOLDER
    return Role.UNKNOWN


def _clean(x: float) -> float:
    x = float(x)
    return round(x, 6) if math.isfinite(x) else 0.0


def classify_trace(
    ledger: Ledger,
    trace: TraceResult,
    cfg: ClassifyConfig,
    model: Scorer | None = None,
) -> dict[str, ClassificationResult]:
    """Classify every account in ``trace.accounts`` (victims included)."""
    if not trace.accounts:
        return {}
    cfg = cfg or ClassifyConfig()
    scorer: Scorer = model if model is not None else HeuristicScorer()
    ids = sorted(trace.accounts)
    window_start = ensure_utc(trace.window_start)
    burst = {
        a: ensure_utc(st.first_tainted_at) if st.first_tainted_at else window_start
        for a, st in trace.accounts.items()
    }
    seeds = {a for a, st in trace.accounts.items() if st.layer >= 1}
    feats = account_features(ledger, ids, window_start, cfg, seed_accounts=seeds, burst_start=burst)
    scores = scorer.score(feats)
    contribs = scorer.contributions(feats)
    rules = hard_rules(ledger, trace)

    out: dict[str, ClassificationResult] = {}
    for acct in ids:
        row = feats.loc[acct]
        score = min(max(_clean(scores.loc[acct]), 0.0), 1.0)
        hit = rules.get(acct)
        if hit is not None:
            role = hit.role
            reasons = list(hit.reasons)
            model_name = f"rules+{scorer.name}"
            if role == Role.MERCHANT and score >= cfg.mule_threshold:
                reasons.append(
                    f"Mule indicators are also present (score {score:.2f}); verify the business before relying on "
                    "the merchant treatment"
                )
        else:
            role = role_from_score(score, cfg)
            blockers = innocent_blockers(row) if role == Role.INNOCENT_HOLDER else []
            if blockers:
                role = Role.UNKNOWN
                reasons = explain_blocked(
                    row, contribs.loc[acct], blockers, score=score, window_hours=cfg.feature_window_hours
                )
            else:
                reasons = explain(
                    role, row, contribs.loc[acct], score=score, window_hours=cfg.feature_window_hours
                )
            model_name = scorer.name
        out[acct] = ClassificationResult(
            account_id=acct,
            role=role,
            score=round(score, 4),
            reasons=reasons,
            features={k: _clean(v) for k, v in row.items()},
            model=model_name,
        )
    return out
