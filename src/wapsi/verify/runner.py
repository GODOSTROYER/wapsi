"""Entry points: ``verify_draft`` (one draft) and ``verify_plan`` (the case's hold plan)."""

from __future__ import annotations

from datetime import datetime

from wapsi.config import WapsiConfig
from wapsi.models import CaseBundle, Draft, VerificationReport
from wapsi.ports import DocSearch, Guard, Ledger
from wapsi.verify.citations import check_citations
from wapsi.verify.injection import check_injection
from wapsi.verify.numeric import check_numeric
from wapsi.verify.policy import check_draft_policy, check_plan
from wapsi.verify.report import build_report, fail, ok

PLAN_DRAFT_ID = "PLAN"


def verify_draft(
    draft: Draft,
    bundle: CaseBundle,
    ledger: Ledger,
    search: DocSearch | None = None,
    guard: Guard | None = None,
    *,
    cfg: WapsiConfig | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Recompute every slot, amount, identifier and citation of ``draft`` and re-check the policy rules.

    ``passed`` is False when any error-severity check fails; warnings never block.
    """
    checks = []
    if draft.case_id != bundle.case.case_id:
        checks.append(
            fail(
                "format",
                "case_mismatch",
                f"draft belongs to {draft.case_id}, bundle is {bundle.case.case_id}",
                [draft.case_id],
            )
        )
    checks += check_numeric(draft, bundle, ledger)
    checks += check_citations(draft, search)
    checks += check_draft_policy(draft, bundle, ledger, cfg)
    checks += check_injection(draft, bundle, guard, ledger)
    if not any(c.name == "format" for c in checks):
        checks.append(ok("format", "no template leftovers"))
    return build_report(draft.draft_id, bundle.case.case_id, checks, now)


def verify_plan(
    bundle: CaseBundle,
    ledger: Ledger | None = None,
    *,
    cfg: WapsiConfig | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Plan-level policy re-check (draft_id ``PLAN``). Pass ``ledger`` to also verify merchant delivery and prior
    NCRP reports behind escalation flags."""
    checks = check_plan(bundle, ledger, cfg)
    if not any(not c.passed and c.severity == "error" for c in checks):
        checks.append(ok("policy", "hold plan re-checked against the trace"))
    return build_report(PLAN_DRAFT_ID, bundle.case.case_id, checks, now)
