"""Graph-model extension points: the ``graph_neighbourhood`` reason and the ``bind_trace`` hook of classify_trace."""

import copy

import pandas as pd

from wapsi.classify import HeuristicScorer, Scorer, TraceAwareScorer, classify_trace
from wapsi.classify.explain import explain, top_indicators
from wapsi.classify.features import FEATURE_COLUMNS, NO_FORWARD_MINUTES
from wapsi.classify.scoring import BIAS, GRAPH_NEIGHBOURHOOD
from wapsi.config import ClassifyConfig
from wapsi.fixtures import showcase as sc
from wapsi.fixtures.bundles import showcase_bundle
from wapsi.models import Role

CFG = ClassifyConfig()


def _row(**kw) -> pd.Series:
    r = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    r["median_forward_minutes"] = NO_FORWARD_MINUTES
    r.update(kw)
    return pd.Series(r, dtype=float)


def _contrib(**kw) -> pd.Series:
    c = dict.fromkeys([*FEATURE_COLUMNS, GRAPH_NEIGHBOURHOOD, "ig_residual", BIAS], 0.0)
    c.update(kw)
    return pd.Series(c, dtype=float)


# --------------------------------------------------------------------------- explain


def test_graph_neighbourhood_gets_a_sentence_in_both_directions():
    mule = explain(Role.MULE, _row(), _contrib(**{GRAPH_NEIGHBOURHOOD: 2.0, BIAS: -1.0}), score=0.8)
    assert mule == [
        "The accounts it received money from and sent money to, and the devices it shares, raise the graph "
        "model's mule score"
    ]
    legit = top_indicators(_row(), _contrib(**{GRAPH_NEIGHBOURHOOD: -1.5}), sign=-1)
    assert legit == [
        "The accounts it received money from and sent money to, and the devices it shares, lower the graph "
        "model's mule score"
    ]
    for reason in mule + legit:  # a model summary: no amounts, account numbers or guilt words
        assert not any(ch.isdigit() for ch in reason) and "mule account" not in reason.lower()


def test_graph_neighbourhood_is_ranked_below_what_the_bank_observed():
    row = _row(age_days=5, history_days=0)
    contrib = _contrib(age_days=0.7, **{GRAPH_NEIGHBOURHOOD: 0.8})  # 0.7 * 1.5 (tenure) > 0.8 * 0.8 (graph)
    reasons = top_indicators(row, contrib, sign=+1, k=2)
    assert reasons[0] == "Account opened 5 days before the fraud"
    assert "graph model" in reasons[1]


def test_small_graph_effects_and_residuals_stay_silent():
    contrib = _contrib(**{GRAPH_NEIGHBOURHOOD: 0.1, "ig_residual": 3.0})
    assert top_indicators(_row(), contrib, sign=+1) == []
    assert explain(Role.MULE, _row(), contrib, score=0.7) == [
        "Model mule score of 0.70 is above the review threshold"
    ]


# --------------------------------------------------------------------------- classify_trace hook


class _Recording(HeuristicScorer):
    """Heuristic scores plus the optional trace hook; ``bind_trace`` returns a named bound copy."""

    def __init__(self, returns: str = "copy") -> None:
        super().__init__()
        self.name = "recording"
        self.returns = returns
        self.bound_to: list = []
        self.trace = None

    def bind_trace(self, ledger, trace):
        self.bound_to.append((ledger, trace))
        if self.returns == "none":
            self.trace = trace
            return None
        if self.returns == "self":
            self.trace = trace
            return self
        bound = copy.copy(self)
        bound.trace, bound.name = trace, f"recording@{trace.case_id}"
        return bound


def test_scorers_with_the_hook_receive_the_trace_once():
    ledger, trace = sc.showcase_ledger(), showcase_bundle(1).trace
    plain = classify_trace(ledger, trace, CFG)
    scorer = _Recording()
    assert isinstance(scorer, TraceAwareScorer) and isinstance(scorer, Scorer)
    res = classify_trace(ledger, trace, CFG, model=scorer)
    assert len(scorer.bound_to) == 1 and scorer.bound_to[0][0] is ledger and scorer.bound_to[0][1] is trace
    assert scorer.trace is None  # the copy was bound, the caller's scorer was not
    assert (
        res[sc.A].model == f"recording@{trace.case_id}"
        and res[sc.K].model == f"rules+recording@{trace.case_id}"
    )
    assert {a: (r.role, r.score, r.reasons) for a, r in res.items()} == {
        a: (r.role, r.score, r.reasons) for a, r in plain.items()
    }


def test_hook_returning_self_or_none_keeps_the_scorer():
    ledger, trace = sc.showcase_ledger(), showcase_bundle(1).trace
    for returns in ("self", "none"):
        scorer = _Recording(returns)
        res = classify_trace(ledger, trace, CFG, model=scorer)
        assert scorer.trace is trace and res[sc.A].model == "recording"


def test_scorers_without_the_hook_are_unchanged():
    assert not isinstance(HeuristicScorer(), TraceAwareScorer)
    ledger, trace = sc.showcase_ledger(), showcase_bundle(1).trace
    res = classify_trace(ledger, trace, CFG, model=HeuristicScorer())
    assert res[sc.A].role == Role.MULE and res[sc.A].model == HeuristicScorer.name
