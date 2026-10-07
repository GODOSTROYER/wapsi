"""CLI for the tamper-evident audit log.

    uv run python -m wapsi.audit drill [--json]           tamper drill on the synthetic showcase case
    uv run python -m wapsi.audit verify FILE [--json]     verify the audit chain of a stored CaseBundle JSON
                                                          (or a JSON list of AuditEvents)

Exit status: 0 = chain intact / every tampering attempt detected, 1 = broken chain or a missed tamper.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from wapsi.audit.chain import verify_chain
from wapsi.models import AuditEvent


def _load_events(path: Path) -> tuple[list[AuditEvent], str | None]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [AuditEvent.model_validate(e) for e in data], None
    if isinstance(data, dict) and isinstance(data.get("audit"), list):
        case_id = (data.get("case") or {}).get("case_id")
        return [AuditEvent.model_validate(e) for e in data["audit"]], case_id
    raise ValueError(f"{path}: expected a CaseBundle JSON object or a JSON list of audit events")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wapsi.audit", description="Hash-chained audit log tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drill", help="tamper drill on the synthetic showcase case")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    v = sub.add_parser("verify", help="verify the audit chain in a CaseBundle / event-list JSON file")
    v.add_argument("path", type=Path)
    v.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.cmd == "drill":
        from wapsi.audit.drill import as_json, render, run_drill

        drill = run_drill()
        print(json.dumps(as_json(drill), indent=2) if args.json else render(drill))
        return 0 if drill.passed else 1

    events, case_id = _load_events(args.path)
    report = verify_chain(events, case_id=case_id) if case_id else verify_chain(events)
    print(json.dumps(asdict(report), indent=2) if args.json else report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
