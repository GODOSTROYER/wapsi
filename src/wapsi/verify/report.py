"""VerificationReport assembly and a plain-text summary for the UI.

Every check carries a stable machine code in ``CheckResult.code`` (``slot_mismatch``, ``ok``, ...) so callers and
tests can match on it; ``CheckResult.message`` is plain text. Reports stored before the ``code`` field existed carry
the code as a ``"<code>: "`` message prefix: ``code_of`` and ``text_of`` read both shapes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from wapsi.models import CheckResult, VerificationReport
from wapsi.timeutil import ensure_utc, utcnow

CATEGORIES = ("numeric", "citations", "policy", "injection", "format")
OK_CODE = "ok"
_LEGACY_CODE = re.compile(r"^([a-z][a-z0-9_]*):\s*")  # "slot_mismatch: ..." (reports stored before ``code``)


def fail(
    name: str, code: str, message: str, spans: Iterable[str] = (), severity: str = "error"
) -> CheckResult:
    return CheckResult(
        name=name,
        passed=False,
        severity=severity,
        code=code,
        message=message,
        spans=[s for s in spans if s],
    )


def warn(name: str, code: str, message: str, spans: Iterable[str] = ()) -> CheckResult:
    return fail(name, code, message, spans, severity="warning")


def ok(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, passed=True, severity="error", code=OK_CODE, message=message)


def code_of(check: CheckResult) -> str:
    """The check's machine code (``check.code``; for a legacy record, the ``"<code>: "`` message prefix)."""
    if check.code:
        return check.code
    m = _LEGACY_CODE.match(check.message or "")
    return m.group(1) if m else ""


def text_of(check: CheckResult) -> str:
    """The check's human-readable message, without a legacy ``"<code>: "`` prefix."""
    if check.code:
        return check.message
    return _LEGACY_CODE.sub("", check.message or "", count=1)


def label_of(check: CheckResult) -> str:
    """``"<code>: <message>"`` (the one-line form logs, summaries and tool payloads show)."""
    code, text = code_of(check), text_of(check)
    return f"{code}: {text}" if code and text else code or text


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
            line = f"  {label} [{c.name}] {label_of(c)}"
            if c.spans:
                line += " | " + "; ".join(f'"{s}"' for s in c.spans[:5])
                if len(c.spans) > 5:
                    line += f" (+{len(c.spans) - 5} more)"
            lines.append(line)
    for c in report.checks:
        if c.passed:
            lines.append(f"  OK [{c.name}] {text_of(c)}")
    return "\n".join(lines)
