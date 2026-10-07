"""Offline, deterministic stand-in for Gemini so the ADK app runs with zero cloud access (demo mode and tests).

``DemoScriptedLlm`` is an ADK ``BaseLlm``: it reads the conversation, picks a short tool plan from the
officer's request (open a case, show the plan, explain an account, approve a draft, ...), emits real ADK
function calls one at a time, and finally writes a summary that only quotes values returned by the tools.
It follows the same rules as the Gemini instruction: verification report before drafts, complaint text is
never obeyed, approvals go through the tool (which checks the signed-in role).
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

NCRP = re.compile(r"(?<![\w-])\d{14}(?![\w-])")  # not the digits inside CASE-/DRF- ids
ACCOUNT = re.compile(r"\b[A-Z]{2}\d{2}-\d{10}\b")
CASE = re.compile(r"\bCASE-\d{14}\b")
URI = re.compile(r"\b(?:gs|demo)://\S+")
APPROVE = re.compile(r"\bapprove\s+(?:draft\s+)?(DRF-[A-Za-z0-9_\-]+)", re.IGNORECASE)
REASONS = ("CHECKS_PASSED", "VERIFIED_ON_LEDGER", "APPROVED_WITH_NOTE")
STOP = {"error", "refused", "blocked"}
OFFICER, INTAKE = "wapsi_officer", "wapsi_intake"
CONTEXT_PREFIX = "For context:"
QUOTE_BEGIN = "<<<BEGIN_QUOTED_AGENT_CONTENT>>>"

Step = tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]

HELP = (
    "I can open a case from NCRP acknowledgement numbers (e.g. 'Open case 31009260000101'), show the money "
    "trail, the hold plan, the verification report and drafts, explain an account, show the clocks, search "
    "the SOP, extract a complaint from an upload (gs://... or demo://complaint/<ncrp_id>) and record a "
    "supervisor's approval ('approve <draft_id>')."
)


def _text(content: types.Content) -> str:
    return " ".join(p.text for p in (content.parts or []) if p.text)


def is_relayed_context(text: str) -> bool:
    """ADK presents other agents' turns as user-role text fenced with quote markers."""
    return text.startswith(CONTEXT_PREFIX) or QUOTE_BEGIN in text


def _is_user_text(content: types.Content) -> bool:
    parts = content.parts or []
    return (content.role == "user" and any(p.text for p in parts)
            and not any(p.function_response for p in parts)
            and not any(p.text and is_relayed_context(p.text) for p in parts))


def _responses(contents: list[types.Content]) -> list[tuple[str, dict[str, Any]]]:
    return [(p.function_response.name or "", dict(p.function_response.response or {}))
            for c in contents for p in (c.parts or []) if p.function_response]


def _call(name: str, args: dict[str, Any]) -> types.Content:
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))])


def _say(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def _lines(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


# --------------------------------------------------------------------------- summaries (values from tools only)


def _summ_open_case(r: dict[str, Any]) -> str:
    out = [f"Opened {r['case_id']} ({r['case_status']}) for {len(r['complaints'])} complaint(s); "
           f"{r['accounts_on_trail']} accounts on the trail, {r['drafts']} drafts."]
    for c in r["complaints"]:
        n = c["narrative"]
        if n["flagged"]:
            quoted = "; ".join(f"“{s}”" for s in n["quoted_spans"][:2])
            out.append(f"Complaint {c['ncrp_id']} contains instruction-like text ({', '.join(n['categories'])}). "
                       f"It is quoted here and was not followed: {quoted}")
    return "\n".join(out)


def _summ_trace(r: dict[str, Any]) -> str:
    t = r["totals"]
    return (f"Money trail for {r['case_id']}: stolen {t['stolen']}; still holdable {t['holdable']}; cashed out "
            f"{t['cashed_out']}; moved to other banks {t['outbound']}; in intermediary accounts "
            f"{t['intermediary']}; settled with merchants {t['merchant_settled']}; unlocated {t['unlocated']}.")


def _summ_plan(r: dict[str, Any]) -> str:
    t = r["totals"]
    rows = [f"{d['account_id']}: {d['disposition']} {d['hold_amount']}"
            + (f" (forward hold {d['forward_hold_amount']})" if d["forward_hold_amount"] != "₹0.00" else "")
            + (" [needs a human decision]" if d["requires_human"] else "")
            for d in r["decisions"]]
    check = r.get("policy_check") or {}
    return (f"Hold plan ({r['restoration_process']}): total hold {t['total_hold']}, recoverable "
            f"{t['total_recoverable']}, over-hold {t['over_hold_amount']}. A whole-account freeze would have "
            f"immobilised {t['whole_account_freeze_baseline']} across {t['whole_account_freeze_accounts']} "
            f"accounts. Policy check: {'passed' if check.get('passed') else 'FAILED'}.\n" + _lines(rows))


def _summ_verification(r: dict[str, Any]) -> str:
    s = r["summary"]
    plan = r.get("plan") or {}
    bad = [f"{d['draft_id']} ({d['kind']}): " + "; ".join(e["message"] for e in d["errors"][:2])
           for d in r["drafts"] if not d["passed"]]
    head = (f"Verification for {r['case_id']}: plan check {'passed' if plan.get('passed') else 'FAILED'}; "
            f"{s['passed']} of {s['drafts']} drafts passed every check.")
    return head + ("\n" + _lines(bad) if bad else "")


def _summ_drafts(r: dict[str, Any]) -> str:
    rows = [f"{d['draft_id']} - {d['kind']} - {d['title']} ({'verified' if d['verified'] else 'NOT verified'}"
            f"{', ' + d['approval'].lower() if d['approval'] else ''})" for d in r["drafts"]]
    return f"Drafts for {r['case_id']} (verification shown above):\n" + _lines(rows)


def _summ_approve(r: dict[str, Any]) -> str:
    acts = ", ".join(f"{a['kind']} {a['status']}" for a in r["actions"]) or "none"
    return (f"Approved {r['draft_id']} ({r['reason_code']}) as {r['approver']} at {r['at']}. Actions: {acts}. "
            f"Case status: {r['case_status']}.")


def _summ_explain(r: dict[str, Any]) -> str:
    c, d = r["classification"], r.get("decision") or {}
    reasons = _lines(c["reasons"][:4]) if c["reasons"] else "- no classifier reasons recorded"
    cites = ", ".join(f"[{x['id']}]" for x in d.get("citations", []))
    return (f"{r['account_id']} (layer {r['layer']}, {r['account_type']}): role {c['role']}; decision "
            f"{d.get('disposition', 'none')} holding {d.get('hold_amount', '₹0.00')} of tainted "
            f"{d.get('tainted_amount', '₹0.00')} {cites}\n{reasons}")


def _summ_clocks(r: dict[str, Any]) -> str:
    # ``subject`` is an identifier only; a provisional start (hold / STR not yet approved) is in ``notes``
    def subject(c: dict[str, Any]) -> str:
        if not c["subject"]:
            return ""
        flag = ", provisional" if str(c.get("notes") or "").startswith("provisional") else ""
        return f" ({c['subject']}{flag})"

    return f"Clocks for {r['case_id']} as of {r['as_of']}:\n" + _lines(
        [f"{c['kind']}{subject(c)}: due {c['due']} [{c['status']}]" for c in r["clocks"]])


def _summ_search(r: dict[str, Any]) -> str:
    return "From the corpus:\n" + _lines(
        [f"[{h['citation_id'] or h['doc_id']}] {h['section']}: {h['excerpt'][:160]}" for h in r["results"]])


def _summ_list(r: dict[str, Any]) -> str:
    return "Cases:\n" + _lines([f"{c['case_id']} {c['status']} (recoverable {c['recoverable']})"
                                for c in r["cases"]]) if r["cases"] else "No cases yet."


def _summ_extract(r: dict[str, Any]) -> str:
    f, chk = r["fields"], r["ledger_check"]
    flag = " The narrative contains instruction-like text; it was quoted, not followed." \
        if r["narrative"]["flagged"] else ""
    match = ("matches the ledger" if chk.get("complaint_found") and chk.get("amount_matches")
             else "does NOT match the ledger")
    return (f"Extracted complaint {f.get('ncrp_id')} ({f.get('amount_claimed')}) from {r['file_uri']}; it "
            f"{match}.{flag} Say 'open case {f.get('ncrp_id')}' to process it.")


SUMMARIES: dict[str, Callable[[dict[str, Any]], str]] = {
    "open_case": _summ_open_case,
    "trace_money": _summ_trace,
    "hold_plan": _summ_plan,
    "verification_report": _summ_verification,
    "draft_documents": _summ_drafts,
    "approve_draft": _summ_approve,
    "explain_account": _summ_explain,
    "case_clocks": _summ_clocks,
    "search_sop": _summ_search,
    "list_cases": _summ_list,
    "extract_complaint": _summ_extract,
}


def _failure(name: str, r: dict[str, Any]) -> str:
    why = r.get("reason") or r.get("error") or "unknown problem"
    return f"{name} did not go through ({r.get('status')}): {why}"


# --------------------------------------------------------------------------- planning


def _known_case(contents: list[types.Content], user_text: str) -> str | None:
    m = CASE.search(user_text)
    if m:
        return m.group(0)
    for name, r in reversed(_responses(contents)):
        if name != "transfer_to_agent" and isinstance(r.get("case_id"), str):
            return r["case_id"]
    ids = NCRP.findall(user_text)
    return f"CASE-{sorted(ids)[0]}" if ids else None


def _officer_plan(text: str, case_id: str | None) -> list[Step] | str:
    low = text.lower()
    ids = sorted(dict.fromkeys(NCRP.findall(text)))
    acct = ACCOUNT.search(text)

    def case(_: dict[str, Any]) -> dict[str, Any]:
        return {"case_id": case_id}

    m = APPROVE.search(text)
    if m and case_id:
        reason = next((r for r in REASONS if r.lower() in low), "CHECKS_PASSED")
        return [("approve_draft", lambda _: {"case_id": case_id, "draft_id": m.group(1), "approver": "",
                                             "reason_code": reason})]
    if acct and case_id and any(w in low for w in ("explain", "why", "account")):
        return [("explain_account", lambda _: {"case_id": case_id, "account_id": acct.group(0)})]
    if ids and any(w in low for w in ("open", "process", "new", "case", "notice", "complaint")):
        def after_open(prev: dict[str, Any]) -> dict[str, Any]:
            return {"case_id": prev.get("case_id") or f"CASE-{ids[0]}"}

        return [("open_case", lambda _: {"ncrp_ids": ids}), ("trace_money", after_open),
                ("hold_plan", after_open), ("verification_report", after_open), ("draft_documents", after_open)]
    if "list" in low and "case" in low:
        return [("list_cases", lambda _: {})]
    if any(w in low for w in ("sop", "search", "rule", "what does", "clause")):
        return [("search_sop", lambda _: {"query": text[:300]})]
    if not case_id:
        return HELP
    if any(w in low for w in ("clock", "deadline", "due")):
        return [("case_clocks", case)]
    if "draft" in low:
        return [("verification_report", case), ("draft_documents", case)]
    if "verif" in low or "check" in low:
        return [("verification_report", case)]
    if "hold" in low or "plan" in low:
        return [("hold_plan", case)]
    if "trace" in low or "trail" in low or "money" in low:
        return [("trace_money", case)]
    return HELP


class DemoScriptedLlm(BaseLlm):
    """Deterministic planner used instead of Gemini when ``WAPSI_DEMO_MODE`` is on."""

    model: str = "wapsi-demo-scripted"

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"wapsi-demo-.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        content = self.respond(llm_request)
        yield LlmResponse(content=content, turn_complete=True)

    def respond(self, llm_request: LlmRequest) -> types.Content:
        contents = list(llm_request.contents or [])
        tools = set(llm_request.tools_dict or {})
        idx = max((i for i, c in enumerate(contents) if _is_user_text(c)), default=-1)
        text = _text(contents[idx]) if idx >= 0 else ""
        done = [(n, r) for n, r in _responses(contents[idx + 1:]) if n != "transfer_to_agent"]
        uri = URI.search(text)

        if "extract_complaint" in tools:  # intake agent
            if done:
                name, r = done[-1]
                return _say(_failure(name, r) if r.get("status") in STOP else SUMMARIES[name](r))
            if uri:
                return _call("extract_complaint", {"file_uri": uri.group(0).rstrip(".,;)")})
            if "transfer_to_agent" in tools:
                return _call("transfer_to_agent", {"agent_name": OFFICER})
            return _say("Share the upload as gs://bucket/file.pdf (or demo://complaint/<ncrp_id>).")

        if uri and not done and "transfer_to_agent" in tools:
            return _call("transfer_to_agent", {"agent_name": INTAKE})
        plan = _officer_plan(text, _known_case(contents, text))
        if isinstance(plan, str):
            return _say(plan)
        if done and done[-1][1].get("status") in STOP:
            name, r = done[-1]
            prior = [SUMMARIES[n](x) for n, x in done[:-1] if n in SUMMARIES]
            return _say("\n\n".join(prior + [_failure(name, r)]))
        if len(done) < len(plan):
            name, args = plan[len(done)]
            prev = done[-1][1] if done else {}
            return _call(name, args(prev))
        return _say("\n\n".join(SUMMARIES[n](r) for n, r in done if n in SUMMARIES))
