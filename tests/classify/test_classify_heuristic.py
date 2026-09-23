"""Calibration intent of the hand-set HeuristicScorer on archetypal feature rows."""

import pandas as pd
import pytest

from wapsi.classify import role_from_score
from wapsi.classify.explain import explain, fmt_minutes, pct
from wapsi.classify.features import FEATURE_COLUMNS, NO_FORWARD_MINUTES
from wapsi.classify.scoring import HeuristicScorer
from wapsi.config import ClassifyConfig
from wapsi.models import Role

CFG = ClassifyConfig()


def row(**kw) -> dict:
    r = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    r["median_forward_minutes"] = NO_FORWARD_MINUTES
    r.update(kw)
    return r


FAST = dict(pass_through_ratio_24h=0.95, median_forward_minutes=10.0, in_amount_24h=200_000.0)
SALARIED = dict(age_days=1800.0, history_days=180.0, salary_credits_90d=3.0)
ARCHETYPES = {
    # name: (features, expected role)
    "new_account_only_received": (row(age_days=10, in_amount_24h=50_000), Role.UNKNOWN),
    "new_account_fast_forward": (row(age_days=10, **FAST), Role.MULE),
    "new_account_min_kyc_cash_out": (row(age_days=10, kyc_min=1, atm_share_24h=1.0, in_amount_24h=9e4), Role.MULE),
    "new_account_external_risk": (row(age_days=10, mulehunter_score=0.8, fri_risk=2), Role.MULE),
    "salaried_kept_money": (row(**SALARIED, in_amount_24h=20_000, n_distinct_payers_7d=2), Role.INNOCENT_HOLDER),
    "salaried_fast_forward": (row(**SALARIED, **FAST), Role.UNKNOWN),
    "old_no_salary_fast_forward": (row(age_days=1800, history_days=180, **FAST), Role.MULE),
    "landlord_with_rent": (row(age_days=2600, history_days=31, recurring_rent_like_90d=2, in_amount_24h=5e3),
                           Role.INNOCENT_HOLDER),
    "dormant_old_burst": (row(age_days=1500, dormant_then_burst=1, pass_through_ratio_24h=0.8,
                              median_forward_minutes=30, atm_share_24h=0.5, in_amount_24h=6e4), Role.MULE),
    "shared_device_ring": (row(age_days=40, shared_device_with_seed_accounts=2, device_shared_count=3), Role.MULE),
    "night_crypto_exit": (row(age_days=200, night_share=0.8, crypto_share_24h=1.0, in_amount_24h=1e5), Role.MULE),
    "no_evidence_at_all": (row(), Role.UNKNOWN),
    "delivered_merchant_like": (row(age_days=900, merchant_delivered_share=1, gst_active=1, history_days=180),
                                Role.INNOCENT_HOLDER),
}  # fmt: skip


@pytest.fixture(scope="module")
def scored():
    frame = pd.DataFrame({k: v[0] for k, v in ARCHETYPES.items()}).T[list(FEATURE_COLUMNS)].astype(float)
    hs = HeuristicScorer()
    return frame, hs.score(frame), hs.contributions(frame)


@pytest.mark.parametrize("name", list(ARCHETYPES))
def test_archetype_roles(scored, name):
    _, scores, _ = scored
    assert role_from_score(scores[name], CFG) == ARCHETYPES[name][1], (name, round(scores[name], 3))


def test_scores_bounded_and_deterministic(scored):
    frame, scores, _ = scored
    assert scores.between(0.01, 0.99).all()
    pd.testing.assert_series_equal(HeuristicScorer().score(frame), scores)


@pytest.mark.parametrize("name", list(ARCHETYPES))
def test_archetype_explanations(scored, name):
    frame, scores, contrib = scored
    role = role_from_score(scores[name], CFG)
    reasons = explain(role, frame.loc[name], contrib.loc[name], score=scores[name])
    assert 1 <= len(reasons) <= 3
    assert all(r and "nan" not in r.lower().split() for r in reasons)
    if role == Role.INNOCENT_HOLDER:
        assert "SOP Para 5(xii)" in reasons[-1]
    if role == Role.UNKNOWN:
        assert "officer review" in reasons[-1]


def test_explanation_wording(scored):
    frame, scores, contrib = scored
    fast = explain(Role.MULE, frame.loc["old_no_salary_fast_forward"], contrib.loc["old_no_salary_fast_forward"],
                   score=scores["old_no_salary_fast_forward"])  # fmt: skip
    assert (
        "95% of the ₹2,00,000.00 received was sent on within 24 hours, typically 10 minutes after arriving"
        in fast
    )
    ring = explain(Role.MULE, frame.loc["shared_device_ring"], contrib.loc["shared_device_ring"], score=0.9)
    assert "Device shared with 2 other accounts that received the same money" in ring
    assert not any(r == "Device shared with 3 other accounts" for r in ring)  # suppressed duplicate
    night = " | ".join(
        explain(Role.MULE, frame.loc["night_crypto_exit"], contrib.loc["night_crypto_exit"], score=0.9)
    )
    assert "crypto exchanges" in night and "all of the money sent out" in night
    dormant = explain(Role.MULE, frame.loc["dormant_old_burst"], contrib.loc["dormant_old_burst"], score=0.9)
    assert dormant[0].startswith("Dormant account")
    rent = explain(
        Role.INNOCENT_HOLDER, frame.loc["landlord_with_rent"], contrib.loc["landlord_with_rent"], score=0.1
    )
    assert rent[-1].startswith("Long-standing account (open about 7 years) with regular rent credits")
    salaried = explain(Role.INNOCENT_HOLDER, frame.loc["salaried_kept_money"], contrib.loc["salaried_kept_money"],
                       score=0.05)  # fmt: skip
    assert "regular salary credits" in salaried[-1]
    assert "None of the money received was sent on within 24 hours" in salaried


def test_formatting_helpers():
    assert pct(0.996) == "99%" and pct(1.0) == "100%" and pct(0.001) == "1%" and pct(0.0) == "0%"
    assert fmt_minutes(0.2) == "less than a minute"
    assert fmt_minutes(1) == "1 minute" and fmt_minutes(31.4) == "31 minutes"
    assert fmt_minutes(300) == "about 5 hours" and fmt_minutes(4 * 1440) == "about 4 days"
