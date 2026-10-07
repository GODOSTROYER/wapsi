"""Draft the STR (and other case drafts) with slot discipline, run the verifier, and print only drafts that pass.

Part of the `str-drafter` skill. Run from the repo root.

    uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory
    uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py <ncrp_id> --world demo --group --kind ALL
    # demo of the verifier catching a hand edit (finale step 6)
    uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory \
        --tamper "₹43,200.00" "₹45,000.00"
    # baseline: an LLM that writes wrong numbers
    uv run python .claude/skills/str-drafter/scripts/draft_and_verify.py 31009260000101 --world showcase-memory \
        --llm fake-hallucinate

The verification report is always printed before the draft. A draft that fails verification, or that names an
innocent holder, is never printed. Exit codes: 0 all selected drafts pass, 1 a draft failed, 2 usage / data error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles (cp1252)

from wapsi.models import (  # noqa: E402
    INNOCENT_ROLES,
    CaseBundle,
    CheckResult,
    Draft,
    DraftKind,
    VerificationReport,
)


def load_ledger(world: str):
    if world in ("showcase-memory", "fixture"):
        from wapsi.fixtures.showcase import showcase_ledger

        return showcase_ledger()
    path = Path(world)
    if not path.exists():
        path = ROOT / "data" / "generated" / world
    if not path.exists():
        raise SystemExit(f"world not found: {world} (generate it with the synth-fraud-ledger skill)")
    from wapsi.adapters.parquet_ledger import ParquetLedger

    return ParquetLedger(path)


def make_llm(kind: str):
    try:
        from evals.harness import make_llm as harness_llm

        return harness_llm(kind)
    except ImportError as exc:
        if kind not in ("fake", "fake-hallucinate"):
            raise SystemExit(f"--llm {kind} needs evals.harness; use fake") from exc
        from wapsi.adapters.llm_fake import FakeLLM

        return FakeLLM(hallucinate=kind == "fake-hallucinate")


def load_search():
    try:
        from wapsi.search.index import LocalDocSearch

        return LocalDocSearch.load()
    except Exception as exc:  # index not built yet, or module missing
        print(
            f"(citation quotes: local doc index unavailable — {type(exc).__name__}; citation ids still checked)"
        )
        return None


def resolve_ids(ledger, ncrp_ids: list[str], group: bool) -> list[str]:
    for n in ncrp_ids:
        ledger.complaint(n)
    if not group:
        return sorted(set(ncrp_ids))
    from wapsi.pipeline.run import group_complaints

    out = set(ncrp_ids)
    for g in group_complaints(ledger, ledger.complaints()):
        if out & set(g):
            out |= set(g)
    return sorted(out)


def innocent_guard(draft: Draft, bundle: CaseBundle, ledger) -> list[CheckResult]:
    """SOP 5(xii): no automatic STR on an innocent holder (bona fide recipient or unwitting conduit; for a conduit the
    officer decides on a separate STR). Error if the STR body names one; warning if a sheet does."""
    if draft.kind != DraftKind.STR:
        return []
    innocent = [a for a, c in bundle.classifications.items() if c.role in INNOCENT_ROLES]
    if bundle.plan:
        innocent += [d.account_id for d in bundle.plan.decisions if d.role in INNOCENT_ROLES]
    innocent = sorted(set(innocent))
    if not innocent:
        return [
            CheckResult(
                name="innocent_holder",
                passed=True,
                severity="error",
                message="no innocent holders in this case",
            )
        ]
    accounts = ledger.accounts(innocent)
    names = ledger.customers([a.customer_id for a in accounts.values() if a.customer_id])
    needles = set(innocent) | {c.name for c in names.values() if c.name}
    in_body = sorted(n for n in needles if n in draft.body or n in draft.title)
    in_sheets = sorted(n for n in needles if n in json.dumps(draft.structured, ensure_ascii=False))
    out = [
        CheckResult(
            name="innocent_holder",
            passed=not in_body,
            severity="error",
            message="STR narrative must not name an innocent holder (SOP 5(xii))"
            if in_body
            else "STR narrative names no innocent holder",
            spans=in_body,
        )
    ]
    if in_sheets:
        out.append(
            CheckResult(
                name="innocent_holder",
                passed=False,
                severity="warning",
                message="innocent holder appears in STR sheets (counterparty only?) — review",
                spans=in_sheets,
            )
        )
    return out


def print_report(draft: Draft, report: VerificationReport, extra: list[CheckResult]) -> bool:
    from wapsi.verify.report import label_of

    checks = list(report.checks) + extra
    blocking = [c for c in checks if not c.passed and c.severity == "error"]
    status = "PASS" if report.passed and not blocking else "FAIL"
    print(f"\n### Verification — {draft.kind.value} {draft.account_id or '(case)'} — {status}\n")
    print("| Check | Result | Severity | Detail |")
    print("|---|---|---|---|")
    for c in checks:
        spans = f" — spans: {', '.join(c.spans[:5])}" if c.spans else ""
        print(f"| {c.name} | {'ok' if c.passed else 'FAILED'} | {c.severity} | {label_of(c)}{spans} |")
    return status == "PASS"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ncrp_ids", nargs="+")
    ap.add_argument("--world", default="demo")
    ap.add_argument("--group", action="store_true")
    ap.add_argument("--kind", default="STR", help="draft kind to show (STR, HOLD_INSTRUCTION, ...) or ALL")
    ap.add_argument("--llm", default="fake", help="fake | fake-hallucinate | openai:<base_url>|<model>")
    ap.add_argument(
        "--tamper", nargs=2, metavar=("OLD", "NEW"), help="replace text in the selected draft(s), re-verify"
    )
    ap.add_argument("--structured", action="store_true", help="also print the FINnet-style structured JSON")
    ap.add_argument("--out", help="directory to write passing drafts (<draft_id>.md / .json)")
    args = ap.parse_args(argv)

    try:
        import wapsi.draft.drafter  # noqa: F401  (import-time check of the drafting modules)
        from wapsi.pipeline.run import process_case
        from wapsi.verify.runner import verify_draft
    except (ImportError, AttributeError) as exc:
        print(f"drafting pipeline not importable in this checkout ({type(exc).__name__}: {exc})")
        return 2

    ledger = load_ledger(args.world)
    try:
        ids = resolve_ids(ledger, args.ncrp_ids, args.group)
    except KeyError as exc:
        print(f"unknown NCRP id in world {args.world}: {exc}")
        return 2
    search = load_search()
    try:
        bundle = process_case(ledger, ids, llm=make_llm(args.llm), search=search, draft=True)
    except ImportError as exc:
        print(f"drafting pipeline not available in this checkout yet: {exc}")
        return 2

    wanted = [d for d in bundle.drafts if args.kind.upper() == "ALL" or d.kind.value == args.kind.upper()]
    print(f"## Drafts — {bundle.case.case_id} ({', '.join(ids)}) · LLM {args.llm}")
    if not wanted:
        kinds = sorted({d.kind.value for d in bundle.drafts})
        print(
            f"\nNo {args.kind} draft for this case (drafts produced: {', '.join(kinds) or 'none'}). "
            "An STR is drafted only when a MULE holds tainted money or cashed out (docs/draft-contract.md)."
        )
        return 0

    failures = 0
    for d in wanted:
        report = bundle.verifications[d.draft_id]
        if args.tamper:
            old, new = args.tamper
            if old not in d.body:
                print(f"\n(--tamper: '{old}' not found in {d.draft_id}; draft unchanged)")
            else:
                d = d.model_copy(update={"body": d.body.replace(old, new, 1), "version": d.version + 1})
                print(f"\n(--tamper: replaced '{old}' with '{new}' in {d.draft_id}; re-verifying)")
                report = verify_draft(d, bundle, ledger, search=search)
        extra = innocent_guard(d, bundle, ledger)
        if not print_report(d, report, extra):
            failures += 1
            print(
                "\nNOT READY: this draft failed verification and is withheld. Redraft (max 2) or route to a human."
            )
            continue
        print(f"\n### {d.title}\n")
        print(d.body)
        print(
            f"\nCitations: {' '.join(f'[{c}]' for c in d.citation_ids)} · model: {d.llm_model or 'template only'}"
        )
        if args.structured and d.structured:
            print("\n```json\n" + json.dumps(d.structured, indent=2, ensure_ascii=False) + "\n```")
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{d.draft_id}.md").write_text(d.body, encoding="utf-8")
            (out / f"{d.draft_id}.json").write_text(d.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"\n{len(wanted) - failures}/{len(wanted)} selected draft(s) passed verification. "
        "Status: Recommended, pending supervisor approval."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
