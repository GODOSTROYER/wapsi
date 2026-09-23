"""``ModelArmorGuard``: ``wapsi.ports.Guard`` backed by Google Cloud Model Armor (GCP edition).

``screen(text)`` calls Model Armor ``SanitizeUserPrompt`` against a template
(``projects/{project}/locations/{location}/templates/{template_id}``) on the regional endpoint
``modelarmor.{location}.rep.googleapis.com`` and maps the filter results to ``GuardResult`` categories:

=====================  ====================================  ===================================
Model Armor filter     category                              flags by default
=====================  ====================================  ===================================
``pi_and_jailbreak``   ``model_armor:prompt_injection``      yes
``malicious_uris``     ``model_armor:malicious_uri``         yes
``csam``               ``model_armor:csam``                  yes
``rai``                ``model_armor:rai:<type>``            no (fraud narratives describe threats
                                                             and harassment; recorded only)
``sdp``                ``model_armor:sensitive_data``        no (complaints always carry PII)
=====================  ====================================  ===================================

The deterministic ``RulesGuard`` always runs too (``combine_with_rules``): Model Armor catches generic
jailbreaks, the rules catch domain actions such as "release all holds" and give quotable spans. The result is the
union. Categories that do not flag are kept in ``last_findings`` rather than in ``GuardResult.categories``.

Fail-open: if Model Armor errors, times out (``timeout_s``, enforced with a thread deadline even if the client
ignores its own timeout) or reports ``invocation_result == FAILURE``, the guard answers with the fallback guard
(``RulesGuard`` by default) and records the fallback in ``fallback_events`` / ``last_source`` and the log.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wapsi.models import GuardResult
from wapsi.ports import Guard
from wapsi.timeutil import utcnow

log = logging.getLogger("wapsi.guard.model_armor")

MATCH_FOUND = "MATCH_FOUND"
PREFIX = "model_armor"
CATEGORY = {
    "pi_and_jailbreak": f"{PREFIX}:prompt_injection",
    "malicious_uris": f"{PREFIX}:malicious_uri",
    "csam": f"{PREFIX}:csam",
    "sdp": f"{PREFIX}:sensitive_data",
    "rai": f"{PREFIX}:rai",
    "virus_scan": f"{PREFIX}:virus",
}
DEFAULT_FLAGGING = frozenset({"pi_and_jailbreak", "malicious_uris", "csam", "virus_scan"})
WITHHELD = "[withheld: flagged by Model Armor]"
_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-armor")


class ModelArmorUnavailable(RuntimeError):
    pass


@dataclass
class FallbackEvent:
    at: datetime
    reason: str
    operation: str


@dataclass
class Findings:
    """Normalised Model Armor verdict for one call."""

    match: bool = False
    filters: dict[str, bool] = field(default_factory=dict)  # filter key -> MATCH_FOUND
    categories: list[str] = field(default_factory=list)  # every matched category (flagging or not)
    invocation_result: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def _to_dict(resp: Any) -> dict[str, Any]:
    if isinstance(resp, Mapping):
        return dict(resp)
    to_dict = getattr(type(resp), "to_dict", None)
    if to_dict is not None:  # proto-plus message
        return to_dict(resp, use_integers_for_enums=False, preserving_proto_field_name=True)
    raise TypeError(f"unsupported Model Armor response type {type(resp).__name__}")


def _enum(value: Any) -> str:
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    if name:
        return str(name)
    if isinstance(value, int):  # FilterMatchState numbers
        return {1: "NO_MATCH_FOUND", 2: MATCH_FOUND}.get(value, "UNSPECIFIED")
    return str(value)


def _inner(result: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """FilterResult is a oneof: return (oneof field name, payload)."""
    for k, v in result.items():
        if k.endswith("_result") and isinstance(v, Mapping) and v:
            return k, v
    return "", {}


def parse_sanitization(resp: Any) -> Findings:
    data = _to_dict(resp)
    sr = data.get("sanitization_result") or data.get("sanitizationResult") or {}
    f = Findings(
        match=_enum(sr.get("filter_match_state", sr.get("filterMatchState", ""))) == MATCH_FOUND,
        invocation_result=_enum(sr.get("invocation_result", sr.get("invocationResult", ""))),
    )
    results = sr.get("filter_results") or sr.get("filterResults") or {}
    for key, result in results.items():
        _, payload = _inner(result)
        if key == "sdp":
            payload = payload.get("inspect_result") or payload.get("deidentify_result") or {}
        matched = _enum(payload.get("match_state", "")) == MATCH_FOUND
        f.filters[key] = matched
        if not matched:
            continue
        if key == "rai":
            types = payload.get("rai_filter_type_results") or {}
            hits = sorted(t for t, r in types.items() if _enum((r or {}).get("match_state", "")) == MATCH_FOUND)
            f.categories += [f"{CATEGORY['rai']}:{t}" for t in hits] or [CATEGORY["rai"]]
        else:
            f.categories.append(CATEGORY.get(key, f"{PREFIX}:{key}"))
        if key == "malicious_uris":
            f.detail["uris"] = [i.get("uri", "") for i in payload.get("malicious_uri_matched_items", [])]
        if key == "pi_and_jailbreak":
            f.detail["pi_confidence"] = _enum(payload.get("confidence_level", ""))
        if key == "sdp":
            f.detail["sdp_info_types"] = sorted(
                {str(x.get("info_type") or "") for x in payload.get("findings", [])} - {""}
            )
    return f


class ModelArmorGuard:
    """Implements ``wapsi.ports.Guard``."""

    def __init__(
        self,
        project: str,
        location: str,
        template_id: str,
        client: Any | None = None,
        fallback: Guard | None = None,
        *,
        timeout_s: float = 3.0,
        combine_with_rules: bool = True,
        flagging_filters: Iterable[str] = DEFAULT_FLAGGING,
        endpoint: str | None = None,
    ) -> None:
        if not (project and location and template_id):
            raise ValueError("project, location and template_id are required")
        from wapsi.verify.injection import RulesGuard

        self.project = project
        self.location = location
        self.template_id = template_id
        self.template = f"projects/{project}/locations/{location}/templates/{template_id}"
        self.endpoint = endpoint or f"modelarmor.{location}.rep.googleapis.com"
        self.timeout_s = float(timeout_s)
        self.fallback: Guard = fallback if fallback is not None else RulesGuard()
        self.rules: Guard | None = (fallback if fallback is not None else RulesGuard()) if combine_with_rules else None
        self.flagging_filters = frozenset(flagging_filters)
        self._client = client
        self.fallback_events: list[FallbackEvent] = []
        self.last_source: str = ""  # "model_armor" | "fallback"
        self.last_findings: Findings | None = None

    # ------------------------------------------------------------------ client
    @property
    def client(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions  # lazy
            from google.cloud import modelarmor_v1  # lazy

            self._client = modelarmor_v1.ModelArmorClient(
                transport="rest", client_options=ClientOptions(api_endpoint=self.endpoint)
            )
        return self._client

    def _user_prompt_request(self, text: str) -> Any:
        try:
            from google.cloud import modelarmor_v1  # lazy

            return modelarmor_v1.SanitizeUserPromptRequest(
                name=self.template, user_prompt_data=modelarmor_v1.DataItem(text=text)
            )
        except ImportError:  # pragma: no cover - library not installed; fakes accept dicts
            return {"name": self.template, "user_prompt_data": {"text": text}}

    def _model_response_request(self, text: str, prompt: str | None) -> Any:
        try:
            from google.cloud import modelarmor_v1  # lazy

            kwargs: dict[str, Any] = {"name": self.template, "model_response_data": modelarmor_v1.DataItem(text=text)}
            if prompt:
                kwargs["user_prompt"] = prompt
            return modelarmor_v1.SanitizeModelResponseRequest(**kwargs)
        except ImportError:  # pragma: no cover
            return {"name": self.template, "model_response_data": {"text": text}, "user_prompt": prompt or ""}

    def _call(self, operation: str, request: Any) -> Findings:
        method = getattr(self.client, operation)
        future = _POOL.submit(method, request=request, timeout=self.timeout_s)
        try:
            resp = future.result(timeout=self.timeout_s + 0.25)
        except cf.TimeoutError as exc:
            future.cancel()
            raise ModelArmorUnavailable(f"timed out after {self.timeout_s}s") from exc
        findings = parse_sanitization(resp)
        if findings.invocation_result == "FAILURE":
            raise ModelArmorUnavailable("invocation_result=FAILURE")
        return findings

    # ------------------------------------------------------------------ Guard protocol
    def screen(self, text: str) -> GuardResult:
        return self._screen(text, "sanitize_user_prompt", self._user_prompt_request(text))

    def screen_response(self, text: str, prompt: str | None = None) -> GuardResult:
        """Screen model output (``SanitizeModelResponse``); same mapping and fallback as :meth:`screen`."""
        return self._screen(text, "sanitize_model_response", self._model_response_request(text, prompt))

    @property
    def fell_back(self) -> bool:
        return self.last_source == "fallback"

    def _screen(self, text: str, operation: str, request: Any) -> GuardResult:
        if not text or not text.strip():
            self.last_source, self.last_findings = "model_armor", Findings()
            return GuardResult(flagged=False, sanitized_text=text or "")
        try:
            findings = self._call(operation, request)
        except Exception as exc:  # noqa: BLE001 - fail open to the deterministic guard
            reason = f"{type(exc).__name__}: {exc}"[:300]
            self.fallback_events.append(FallbackEvent(at=utcnow(), reason=reason, operation=operation))
            self.last_source, self.last_findings = "fallback", None
            log.warning("Model Armor unavailable (%s); using %s", reason, type(self.fallback).__name__)
            return self.fallback.screen(text)

        self.last_source, self.last_findings = "model_armor", findings
        flagging = [k for k, hit in findings.filters.items() if hit and k in self.flagging_filters]
        ma_categories = [
            c for c in findings.categories
            if any(c == CATEGORY.get(k) or c.startswith(f"{CATEGORY.get(k)}:") for k in flagging)
        ]
        rules = self.rules.screen(text) if self.rules is not None else GuardResult(flagged=False, sanitized_text=text)
        flagged = bool(ma_categories) or rules.flagged
        categories = list(dict.fromkeys(ma_categories + list(rules.categories)))
        spans = list(rules.quoted_spans)
        sanitized = rules.sanitized_text or text
        if ma_categories and not rules.flagged:
            # Model Armor gives no spans: quote the text (bounded) and withhold it from any prompt
            spans.append(text.strip()[:300])
            sanitized = WITHHELD
        return GuardResult(flagged=flagged, categories=categories, quoted_spans=spans, sanitized_text=sanitized)
