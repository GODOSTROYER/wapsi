"""Deterministic offline LLM for tests, demos and evals (implements ``wapsi.ports.LLM``).

It reads the JSON request that ``wapsi.draft.prose`` sends (task, placeholders, facts) and writes prose that uses
only the offered ``⟦slot⟧`` placeholders and no digits.

- ``hallucinate=True`` adds a plausible but wrong raw rupee amount (lets evals measure how LLM number errors
  are caught).
- ``inject_obedience=True`` copies an instruction-like sentence from a complaint narrative (injection tests).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from wapsi.money import find_amounts, fmt_inr, r2

_MARKERS = re.compile(r"<<<(?:END_)?UNTRUSTED_COMPLAINT_TEXT>>>")
_INSTRUCTION = re.compile(
    r"(?i)\b(ignore|disregard|override|forget|release|unfreeze|lift|remove|cancel|approve|transfer|instruct\w*|"
    r"system prompt|assistant|you must|you should|do not hold|don't hold)\b"
)
_WORDS = [
    "no",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
]
_GROUND_PHRASE = {
    "PROCEEDS_OF_CRIME": "give reasonable grounds to suspect that the funds are proceeds of crime",
    "UNUSUAL_COMPLEXITY": "were routed with unusual and unjustified complexity",
    "NO_ECONOMIC_RATIONALE": "have no apparent economic rationale or bona fide purpose",
    "TERROR_FINANCING": "give reasonable grounds to suspect a link to the financing of terrorism",
}


def _w(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else "many"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


_ACRONYMS = {"kyc": "KYC", "upi": "UPI", "otp": "OTP", "atm": "ATM", "sim": "SIM", "qr": "QR"}


def _letters(s: str) -> str:
    return " ".join(_ACRONYMS.get(w, w) for w in re.sub(r"[^A-Za-z]+", " ", s).lower().split())


def _is_zero_amount(value: str) -> bool:
    found = find_amounts(value)
    return len(found) == 1 and found[0][0] == value.strip() and r2(found[0][1]) == 0


def _find_payload(messages: Sequence[dict[str, str]]) -> dict[str, Any] | None:
    for m in messages:
        if m.get("role") != "user":
            continue
        try:
            data = json.loads(m.get("content", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and "task" in data:
            return data
    return None


def _accounts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for layer in facts.get("trail", {}).get("layers", []) for a in layer.get("accounts", [])]


def _str_grounds(facts: dict[str, Any], ph: Callable[[str], str | None]) -> list[str]:
    complaints = facts.get("complaints", [])
    n = len(complaints)
    cats = sorted({_letters(c.get("category", "")) for c in complaints} - {""})
    subjects = [
        a
        for a in _accounts(facts)
        if a.get("is_home_bank") and a.get("layer", 0) >= 1 and a.get("role") in ("MULE", "UNKNOWN")
    ]
    out = []
    lead = f"{_cap(_w(n))} {'complaint' if n == 1 else 'complaints'}"
    if ph("ncrp_ids"):
        lead += f" ({ph('ncrp_ids')})"
    loss = f" of {ph('total_stolen')}" if ph("total_stolen") else ""
    how = " and ".join(cats) if cats else "cyber fraud"
    out.append(f"{lead} {'reports' if n == 1 else 'report'} a loss{loss} through {how}.")
    if subjects:
        out.append(
            f"Within hours the money passed through {_w(len(subjects))} "
            f"{'account' if len(subjects) == 1 else 'accounts'} held with this bank that "
            f"{'shows' if len(subjects) == 1 else 'show'} mule indicators such as recent account opening, rapid "
            "onward transfers and devices shared between accounts."
        )
    parts = []
    if ph("total_cashed_out"):
        parts.append(f"{ph('total_cashed_out')} was withdrawn in cash")
    if ph("total_outbound"):
        parts.append(f"{ph('total_outbound')} was moved to accounts at other banks")
    if parts:
        out.append("Of the stolen money, " + " and ".join(parts) + ".")
    ground = (facts.get("case") or {}).get("str_ground") or "PROCEEDS_OF_CRIME"
    out.append(f"The transactions {_GROUND_PHRASE.get(ground, _GROUND_PHRASE['PROCEEDS_OF_CRIME'])}.")
    return out


def _edd_rationale(facts: dict[str, Any], ph: Callable[[str], str | None]) -> list[str]:
    focus = facts.get("focus_account") or {}
    sig = focus.get("signals") or {}
    fri = str(sig.get("fri_risk") or "NONE")
    fri_text = "not raised" if fri == "NONE" else f"{_letters(fri)}"
    prior = int(sig.get("ncrp_prior_reports") or 0)
    acct = ph("account_id") or "the account"
    out = [f"The holder of {acct} appears to be an innocent recipient rather than a money mule."]
    out.append(
        f"The account has a long and regular history, its fraud-risk indicator is {fri_text}, and it has "
        f"{_w(prior)} earlier {'complaint' if prior == 1 else 'complaints'} on record."
    )
    if ph("disputed_amount"):
        out.append(
            f"The disputed credit of {ph('disputed_amount')} fits the account's usual pattern of receipts."
        )
    if ph("hold_amount"):
        out.append(
            f"An amount-specific hold of {ph('hold_amount')} protects the victim without escalating the matter or "
            "reporting the holder, and the holder should be invited to file a grievance with supporting documents."
        )
    return out


def _case_summary(facts: dict[str, Any], ph: Callable[[str], str | None]) -> list[str]:
    n = len(facts.get("complaints", []))
    lead = f"{_cap(_w(n))} {'complaint' if n == 1 else 'complaints'}"
    if ph("ncrp_ids"):
        lead += f" ({ph('ncrp_ids')})"
    out = [f"{lead} {'reports' if n == 1 else 'report'} {ph('total_stolen') or 'money'} stolen."]
    parts = []
    if ph("total_hold"):
        parts.append(f"{ph('total_hold')} is still held in accounts at this bank")
    if ph("total_outbound"):
        parts.append(
            f"{ph('total_outbound')} rests in accounts at other banks, for which hold requests are raised "
            "through CFCFRMS"
        )
    if ph("total_cashed_out"):
        parts.append(f"{ph('total_cashed_out')} has already been cashed out")
    if parts:
        out.append("The trail shows that " + "; ".join(parts) + ".")
    if ph("total_recoverable") and ph("baseline_freeze_amount"):
        out.append(
            f"Amount-specific holds and outbound requests together cover {ph('total_recoverable')}, while freezing "
            f"the touched accounts outright would have immobilised {ph('baseline_freeze_amount')}."
        )
    return out


def _generic(facts: dict[str, Any], ph: Callable[[str], str | None]) -> list[str]:
    return ["The case facts have been reviewed and the recommended actions follow the hold plan."]


_BUILDERS = {"str_grounds": _str_grounds, "edd_rationale": _edd_rationale, "case_summary": _case_summary}


def _wrong_amount(facts: dict[str, Any], values: Sequence[str]) -> float:
    allowed = {r2(v) for value in values for _, v in find_amounts(value)}
    base = float((facts.get("totals") or {}).get("stolen") or 100000.0)
    cand = max(100.0, round(base * 0.97 / 100.0) * 100.0)
    while r2(cand) in allowed:
        cand += 100.0
    return cand


def _instruction_sentence(facts: dict[str, Any]) -> str | None:
    for c in facts.get("complaints", []):
        narrative = c.get("narrative")
        text = narrative.get("text", "") if isinstance(narrative, dict) else str(narrative or "")
        text = _MARKERS.sub(" ", text)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if _INSTRUCTION.search(sentence):
                return sentence.strip()
    return None


class FakeLLM:
    """Implements ``wapsi.ports.LLM`` without any network access."""

    def __init__(
        self, name: str = "fake-llm", hallucinate: bool = False, inject_obedience: bool = False
    ) -> None:
        self.name = name
        self.hallucinate = hallucinate
        self.inject_obedience = inject_obedience
        self.calls: list[list[dict[str, str]]] = []

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        self.calls.append([dict(m) for m in messages])
        payload = _find_payload(messages)
        if payload is None:
            text = "No structured drafting request was found."
        else:
            text = self._prose(payload)
        if json_schema is not None:
            return json.dumps({"text": text}, ensure_ascii=False)
        return text

    def _prose(self, payload: dict[str, Any]) -> str:
        facts = payload.get("facts") or {}
        offered = {
            p.get("name"): str(p.get("value", ""))
            for p in payload.get("placeholders", [])
            if isinstance(p, dict) and p.get("name")
        }

        def ph(name: str) -> str | None:
            if name not in offered or _is_zero_amount(offered[name]):
                return None
            return f"⟦{name}⟧"

        sentences = _BUILDERS.get(str(payload.get("task")), _generic)(facts, ph)
        if self.hallucinate:
            wrong = fmt_inr(_wrong_amount(facts, list(offered.values())))
            sentences.append(f"A further {wrong} was moved through the same accounts the next day.")
        if self.inject_obedience:
            copied = _instruction_sentence(facts)
            if copied:
                sentences.append(copied)
        return " ".join(sentences)
