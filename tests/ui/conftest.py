from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP = ROOT / "ui" / "app.py"

SAMPLE_REPORT = {
    "world": "demo",
    "generated_at": "2026-09-20T10:00:00+00:00",
    "config": {"rule": "PRO_RATA", "llm": "fake-llm", "scorer": "heuristic"},
    "cases": 40,
    "adversarial_cases": 3,
    "metrics": {
        "traced_amount_recall": 0.99,
        "totals_abs_error_inr": 0.0,
        "hold_exact_match_rate": 1.0,
        "over_hold_inr": 0.0,
        "false_freeze_count": 0,
        "disposition_accuracy": 0.975,
        "process_accuracy": 1.0,
        "str_ground_accuracy": 1.0,
        "escalation_precision": 1.0,
        "escalation_recall": 1.0,
        "role_precision": {"MULE": 0.9, "INNOCENT_HOLDER": 0.95, "MERCHANT": 1.0},
        "role_recall": {"MULE": 0.93, "INNOCENT_HOLDER": 0.88, "MERCHANT": 1.0},
        "draft_verification_pass_rate": 1.0,
        "draft_numeric_accuracy": 1.0,
        "llm_only_hallucination_rate": 0.4,
        "citation_validity": 1.0,
        "injection_flag_recall": 1.0,
        "injection_leaks": 0,
        "p50_time_to_hold_plan_s": 0.05,
        "p95_time_to_hold_plan_s": 0.12,
        "p95_total_s": 0.4,
        "brand_new_metric": 0.5,
    },
    "baseline": {"whole_account_freeze_inr": 38000000.0, "wapsi_hold_inr": 461000.0,
                 "accounts_frozen_baseline": 90, "accounts_held": 60},
    "per_typology": {"T1_FANOUT_CASHOUT": {"cases": 5, "hold_exact_match_rate": 1.0, "disposition_accuracy": 1.0},
                     "T10_NODAL_POOL": {"cases": 4, "hold_exact_match_rate": 0.75}},
    "failures": [{"case_id": "C-007", "check": "hold_mismatch", "detail": "HB01-1 expected 100.00 got 90.00"}],
}


@pytest.fixture
def workdir():
    """A private temp directory (pytest's shared basetemp may be unwritable in sandboxed runs)."""
    with tempfile.TemporaryDirectory(prefix="wapsi-ui-", ignore_cleanup_errors=True) as d:
        yield Path(d)


@pytest.fixture
def sample_report(workdir: Path) -> Path:
    path = workdir / "latest.json"
    path.write_text(json.dumps(SAMPLE_REPORT), encoding="utf-8")
    return path


@pytest.fixture
def bundles():
    from wapsi.fixtures.bundles import all_showcase_bundles

    return all_showcase_bundles()
