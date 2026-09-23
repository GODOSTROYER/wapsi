"""Compact, JSON-safe case summaries (API responses, smoke test output, benchmark rows)."""

from __future__ import annotations

from typing import Any

from wapsi.models import CaseBundle


def case_timings(bundle: CaseBundle) -> dict[str, float]:
    """Stage timings recorded by ``process_case`` in its ``case_processed`` audit event (seconds)."""
    ev = next((e for e in reversed(bundle.audit) if e.action == "case_processed"), None)
    raw = (ev.details.get("timings") if ev else None) or {}
    return {k: float(v) for k, v in raw.items() if isinstance(v, int | float)}


def failed_checks(bundle: CaseBundle) -> list[dict[str, str]]:
    out = []
    for draft_id, rep in bundle.verifications.items():
        for c in rep.checks:
            if not c.passed and c.severity == "error":
                out.append({"draft_id": draft_id, "check": c.name, "message": c.message[:300]})
    return out


def case_summary(bundle: CaseBundle) -> dict[str, Any]:
    drafts = bundle.drafts
    llm_drafts = [d for d in drafts if "llm_fallback" in d.structured]
    fallbacks = sum(1 for d in llm_drafts if d.structured.get("llm_fallback"))
    passed = sum(1 for d in drafts if (r := bundle.verifications.get(d.draft_id)) is not None and r.passed)
    plan = bundle.plan
    injection_flags = sum(1 for e in bundle.audit if e.action == "untrusted_instruction_detected")
    ev = next((e for e in reversed(bundle.audit) if e.action == "case_processed"), None)
    return {
        "case_id": bundle.case.case_id,
        "ncrp_ids": list(bundle.case.ncrp_ids),
        "status": bundle.case.status.value,
        "total_hold": plan.total_hold if plan else None,
        "total_recoverable": plan.total_recoverable if plan else None,
        "baseline_freeze_amount": plan.baseline_freeze_amount if plan else None,
        "restoration_process": plan.restoration_process.value if plan else None,
        "hold_accounts": sum(1 for d in plan.decisions if d.hold_amount > 0) if plan else 0,
        "drafts": len(drafts),
        "drafts_passed": passed,
        "all_verified": bool(bundle.verifications) and all(r.passed for r in bundle.verifications.values()),
        "llm_drafts": len(llm_drafts),
        "prose_fallbacks": fallbacks,
        "injection_flags": injection_flags,
        "failed_checks": failed_checks(bundle),
        "timings_s": case_timings(bundle),
        "llm": (ev.details.get("llm") if ev else None),
        "scorer": sorted({c.model for c in bundle.classifications.values()}),
    }
