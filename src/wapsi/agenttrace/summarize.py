"""One-line summaries and verdicts derived from a tool's own result (numbers come from data, never re-computed).

Every function here is total: an unexpected result shape degrades to a generic summary instead of raising, because
tracing must never break an agent run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from wapsi.agenttrace.model import GuardVerdict, VerifierVerdict
from wapsi.agenttrace.redact import redact_text
from wapsi.money import find_account_ids, find_amounts, r2

CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def status_of(result: Any, error: str | None = None) -> str:
    if error:
        return "exception"
    if isinstance(result, dict):
        return str(result.get("status") or "ok")
    return "ok"


def case_of(args: dict[str, Any] | None, result: Any) -> str | None:
    """The case a tool call is about: the result's ``case_id`` (the tool resolved it), else the argument."""
    for src in (result if isinstance(result, dict) else {}, args or {}):
        cid = src.get("case_id")
        if isinstance(cid, str) and CASE_ID_RE.match(cid.strip()):
            return cid.strip()
    return None


def _n(x: Any) -> int:
    return len(x) if isinstance(x, (list, tuple, dict)) else 0


def _passfail(ok: Any) -> str:
    return "passed" if ok else "FAILED"


def _summ(tool: str, r: dict[str, Any], args: dict[str, Any]) -> str:
    if tool == "open_case":
        flagged = r.get("untrusted_instructions_found_in") or []
        out = (f"{r.get('case_id')} · {_n(r.get('complaints'))} complaint(s) · {r.get('accounts_on_trail')} accounts "
               f"on the trail · {r.get('drafts')} drafts")
        if r.get("reused_existing_case"):
            out += " · existing case reused"
        if flagged:
            out += f" · instruction-like text in {', '.join(map(str, flagged))} (quoted, not followed)"
        return out
    if tool == "trace_money":
        t = r.get("totals") or {}
        return (f"stolen {t.get('stolen')} · holdable {t.get('holdable')} · cashed out {t.get('cashed_out')} · "
                f"other banks {t.get('outbound')} · {r.get('accounts_total')} accounts")
    if tool == "hold_plan":
        t = r.get("totals") or {}
        check = r.get("policy_check") or {}
        return (f"{r.get('decisions_total')} decisions · total hold {t.get('total_hold')} · recoverable "
                f"{t.get('total_recoverable')} · whole-account freeze would be {t.get('whole_account_freeze_baseline')}"
                f" · policy check {_passfail(check.get('passed'))}")
    if tool == "verification_report":
        s = r.get("summary") or {}
        plan = r.get("plan") or {}
        return (f"plan check {_passfail(plan.get('passed'))} · {s.get('passed')} of {s.get('drafts')} drafts "
                "passed every check")
    if tool == "draft_documents":
        drafts = r.get("drafts") or []
        ok = sum(1 for d in drafts if isinstance(d, dict) and d.get("verified"))
        return f"{len(drafts)} drafts shown · {ok} verified"
    if tool == "approve_draft":
        acts = ", ".join(f"{a.get('kind')} {a.get('status')}" for a in r.get("actions") or [] if isinstance(a, dict))
        return (f"approved {r.get('draft_id')} ({r.get('reason_code')}) by {r.get('approver')} · "
                f"actions: {acts or 'none'}")
    if tool == "explain_account":
        c = r.get("classification") or {}
        d = r.get("decision") or {}
        return (f"{r.get('account_id')} · layer {r.get('layer')} · role {c.get('role')} · "
                f"{d.get('disposition', 'no decision')} {d.get('hold_amount', '')}".rstrip())
    if tool == "case_clocks":
        by = r.get("by_status") or {}
        return f"{_n(r.get('clocks'))} clocks · " + (", ".join(f"{v} {k}" for k, v in by.items()) or "none")
    if tool == "search_sop":
        ids = [h.get("citation_id") or h.get("doc_id") for h in r.get("results") or [] if isinstance(h, dict)]
        return f"{len(ids)} passages: " + (", ".join(f"[{i}]" for i in ids if i) or "none")
    if tool == "list_cases":
        return f"{_n(r.get('cases'))} cases"
    if tool == "extract_complaint":
        f = r.get("fields") or {}
        chk = r.get("ledger_check") or {}
        match = "matches the ledger" if chk.get("complaint_found") and chk.get("amount_matches") else \
            "does NOT match the ledger"
        return f"complaint {f.get('ncrp_id')} ({f.get('amount_claimed')}) {match}"
    if tool == "load_memory":
        return f"{_n(r.get('memories'))} related memories"
    keys = [k for k in r if k != "status"][:6]
    return "returned " + (", ".join(keys) if keys else "nothing")


def summarize_result(tool: str, result: Any, args: dict[str, Any] | None = None, *,
                     error: str | None = None) -> str:
    """One line (redacted) built only from values the tool returned."""
    args = args or {}
    if error:
        return redact_text(f"raised {error}", max_len=240)
    if tool == "transfer_to_agent":
        return redact_text(f"handed over to {args.get('agent_name') or 'another agent'}", max_len=240)
    if not isinstance(result, dict):
        return "returned no data" if result is None else redact_text(f"returned {type(result).__name__}", max_len=240)
    status = str(result.get("status") or "ok")
    if status in {"error", "refused", "blocked"}:
        why = result.get("reason") or result.get("error") or "no reason given"
        return redact_text(f"{status}: {why}", max_len=240)
    try:
        text = _summ(tool, result, args)
    except Exception:  # noqa: BLE001 - a summary is a convenience
        text = f"returned {len(result)} fields"
    return redact_text(text, max_len=240)


def verifier_of(tool: str, result: Any) -> VerifierVerdict | None:
    """The deterministic check a tool result carries, if any."""
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "ok")
    try:
        if tool == "verification_report" and status == "ok":
            s = result.get("summary") or {}
            plan = result.get("plan")
            plan_ok = bool(plan.get("passed")) if isinstance(plan, dict) else True
            drafts, failed = int(s.get("drafts") or 0), int(s.get("failed") or 0)
            return VerifierVerdict(name="verifier", passed=plan_ok and failed == 0,
                                   checked=drafts + (1 if isinstance(plan, dict) else 0),
                                   failed=failed + (0 if plan_ok else 1),
                                   detail=f"plan {_passfail(plan_ok)}; {drafts - failed} of {drafts} drafts passed "
                                          "numeric, citation, policy and injection checks")
        if tool == "hold_plan" and status == "ok" and isinstance(result.get("policy_check"), dict):
            check = result["policy_check"]
            errs = check.get("errors") or []
            return VerifierVerdict(name="policy_check", passed=bool(check.get("passed")), checked=1,
                                   failed=0 if check.get("passed") else 1,
                                   detail="hard hold rules re-checked independently"
                                          + (f": {len(errs)} error(s)" if errs else ""))
        if tool == "draft_documents" and status == "ok":
            drafts = [d for d in result.get("drafts") or [] if isinstance(d, dict)]
            bad = sum(1 for d in drafts if not d.get("verified"))
            return VerifierVerdict(name="drafts_verified", passed=bad == 0, checked=len(drafts), failed=bad,
                                   detail=f"{len(drafts) - bad} of {len(drafts)} drafts shown as verified")
        if tool == "approve_draft":
            if status == "approved":
                return VerifierVerdict(name="re_verification", passed=True, checked=1,
                                       detail="draft re-verified against the ledger before the approval was recorded")
            reason = str(result.get("reason") or "")
            if status == "refused" and reason.startswith("cannot approve"):
                return VerifierVerdict(name="re_verification", passed=False, checked=1, failed=1,
                                       detail=redact_text(reason, max_len=200))
    except Exception:  # noqa: BLE001
        return None
    return None


def result_guards(tool: str, result: Any) -> list[GuardVerdict]:
    """Guard verdicts a tool reports on the untrusted text it returned (complaint narratives)."""
    if not isinstance(result, dict):
        return []
    try:
        if tool == "open_case" and str(result.get("status")) == "ok":
            flagged = [str(x) for x in result.get("untrusted_instructions_found_in") or []]
            cats: list[str] = []
            for c in result.get("complaints") or []:
                n = (c or {}).get("narrative") or {}
                if n.get("flagged"):
                    cats += [str(x) for x in n.get("categories") or []]
            n_complaints = len(result.get("complaints") or [])
            return [GuardVerdict(
                stage="tool_result", source=str(result.get("guard") or ""), flagged=bool(flagged),
                action="quoted" if flagged else "passed", categories=list(dict.fromkeys(cats)),
                detail=(f"instruction-like text in complaint {', '.join(flagged)}: quoted to the officer, not followed"
                        if flagged else f"{n_complaints} complaint narrative(s) screened, wrapped as untrusted data"))]
        if tool == "extract_complaint" and str(result.get("status")) == "ok":
            n = result.get("narrative") or {}
            flagged = bool(n.get("flagged"))
            return [GuardVerdict(stage="tool_result", source=str(result.get("guard") or ""), flagged=flagged,
                                 action="quoted" if flagged else "passed",
                                 categories=[str(x) for x in n.get("categories") or []],
                                 detail="uploaded complaint narrative screened, wrapped as untrusted data")]
    except Exception:  # noqa: BLE001
        return []
    return []


# --------------------------------------------------------------------------- reply grounding


def grounding_facts(texts: Iterable[str]) -> tuple[set[float], set[str]]:
    """Rupee amounts and account numbers that appear in ``texts`` (tool results the model has seen)."""
    amounts: set[float] = set()
    accounts: set[str] = set()
    for t in texts:
        if not t:
            continue
        amounts.update(r2(v) for _, v in find_amounts(t))
        accounts.update(canon for _, canon in find_account_ids(t))
    return amounts, accounts


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def reply_grounding(reply: str, amounts: set[float], accounts: set[str]) -> VerifierVerdict:
    """Every rupee amount and account number in the agent's reply must appear in a tool result."""
    found_amounts = {r2(v): span for span, v in find_amounts(reply or "")}  # distinct values
    found_accounts = {canon: span for span, canon in find_account_ids(reply or "")}
    missing = [span for v, span in found_amounts.items() if v not in amounts]
    missing += [span for canon, span in found_accounts.items() if canon not in accounts]
    checked = len(found_amounts) + len(found_accounts)
    if checked == 0:
        detail = "no amounts or account numbers in the reply"
    elif not missing:
        detail = (f"all {len(found_amounts)} amounts and {len(found_accounts)} account numbers in the reply "
                  "appear in tool results")
    else:
        shown = "; ".join(redact_text(m) for m in missing[:3])
        detail = f"{len(missing)} value(s) in the reply not found in any tool result: {shown}"
    return VerifierVerdict(name="reply_grounding", passed=not missing, checked=checked, failed=len(missing),
                           detail=detail)
