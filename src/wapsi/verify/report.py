"""VerificationReport assembly and a plain-text summary for the UI.

Check messages start with a stable code (``slot_mismatch: ...``) so callers and tests can match on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from wapsi.models import CheckResult, VerificationReport
from wapsi.timeutil import ensure_utc, utcnow

CATEGORIES = ("numeric", "citations", "policy", "injection", "format")


def fail(
    name: str, code: str, message: str, spans: Iterable[str] = (), severity: str = "error"
) -> CheckResult:
    return CheckResult(
        name=name,
        passed=False,
        severity=severity,
        message=f"{code}: {message}",
        spans=[s for s in spans if s],
    )


def warn(name: str, code: str, message: str, spans: Iterable[str] = ()) -> CheckResult:
    return fail(name, code, message, spans, severity="warning")


def ok(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, passed=True, severity="error", message=f"ok: {message}")


def code_of(check: CheckResult) -> str:
    return check.message.split(":", 1)[0]


def is_blocking(check: CheckResult) -> bool:
    return not check.passed and check.severity == "error"


def build_report(
    draft_id: str, case_id: str, checks: Iterable[CheckResult], now: datetime | None = None
) -> VerificationReport:
    checks = list(checks)
    return VerificationReport(
        draft_id=draft_id,
        case_id=case_id,
        passed=not any(is_blocking(c) for c in checks),
        checks=checks,
        verified_at=ensure_utc(now) if now else utcnow(),
    )


def errors(report: VerificationReport) -> list[CheckResult]:
    return [c for c in report.checks if is_blocking(c)]


def warnings(report: VerificationReport) -> list[CheckResult]:
    return [c for c in report.checks if not c.passed and c.severity == "warning"]


def summarize(report: VerificationReport) -> str:
    """Multi-line plain-text summary: headline, then one line per error / warning / passed check."""
    errs, warns = errors(report), warnings(report)
    head = "PASSED" if report.passed else "FAILED"
    lines = [f"{head}: {report.draft_id} ({len(errs)} error(s), {len(warns)} warning(s))"]
    for label, group in (("ERROR", errs), ("WARN", warns)):
        for c in group:
            line = f"  {label} [{c.name}] {c.message}"
            if c.spans:
                line += " | " + "; ".join(f'"{s}"' for s in c.spans[:5])
                if len(c.spans) > 5:
                    line += f" (+{len(c.spans) - 5} more)"
            lines.append(line)
    for c in report.checks:
        if c.passed:
            lines.append(f"  OK [{c.name}] {c.message.removeprefix('ok: ')}")
    return "\n".join(lines)
