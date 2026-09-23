from __future__ import annotations

from pydantic import BaseModel, Field

from wapsi.models import ApportionmentRule


class TraceConfig(BaseModel):
    window_hours: float = 72.0  # follow outflows up to this long after the first tainted receipt
    max_depth: int = 6
    min_edge_amount: float = 100.0  # ignore edges carrying less tainted money than this (rupees)
    lookback_days: int = 30  # sub-ledger history pulled before the first victim transfer
    rule: ApportionmentRule = ApportionmentRule.PRO_RATA
    follow_consortium: bool = True  # follow other-bank hops via simulated CFCFRMS responses
    max_accounts: int = 500


class ClassifyConfig(BaseModel):
    mule_threshold: float = 0.6
    innocent_threshold: float = 0.35
    feature_window_hours: float = 24.0


class WapsiConfig(BaseModel):
    trace: TraceConfig = Field(default_factory=TraceConfig)
    classify: ClassifyConfig = Field(default_factory=ClassifyConfig)
    escalation_min_prior_reports: int = 2  # s.106 escalation candidate needs FIR + >= this many reports
