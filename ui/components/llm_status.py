"""Which model wrote the prose, and from where: the status chip and the "recorded run" banner (pure functions).

Two different questions, answered from two different sources:

- **What is this deployment set up to do?** ``llm_status()`` reads the configuration: is a live OpenAI-compatible
  endpoint configured (``WAPSI_LLM_BASE_URL`` / ``WAPSI_LLM_API_KEY`` / ``WAPSI_LLM_MODEL``), is there a recording to
  replay? ``banner()`` turns that into the page banner ("Recorded run — live AMD endpoint not configured").
- **Who actually wrote these drafts?** ``served_chip(bundle)`` reads the drafts themselves: every draft with LLM
  prose records its writer (``Draft.llm_model``, sealed into the audit chain) and whether the prose was replaced by
  the fixed paragraph (``structured["llm_fallback"]``). So the chip is true even when a live call failed half-way
  and the recording took over: it reports what happened, not what was configured.

Nothing here reads a key or calls a network.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from wapsi.adapters.llm_env import (
    API_KEY_ENV,
    BASE_URL_ENV,
    MODEL_ENV,
    EndpointConfig,
    Writer,
    endpoint_config,
    parse_served,
)

UI_LLM_ENV = "WAPSI_UI_LLM"
MODE_FAKE, MODE_AUTO, MODE_RECORDED, MODE_LIVE = "fake", "auto", "recorded", "live"
MODES = (MODE_FAKE, MODE_AUTO, MODE_RECORDED, MODE_LIVE)
BANNER_RECORDED = "Recorded run — live AMD endpoint not configured"
BANNER_RECORDED_OFF = "Recorded run — live model calls are switched off"
BANNER_OFFLINE = "Offline run — live AMD endpoint not configured"
NUMBERS_NOTE = "Every number is filled from the ledger and re-checked by the verifier, live."


def ui_llm_mode(env: Mapping[str, str] | None = None) -> str:
    """``WAPSI_UI_LLM``: ``fake`` (default: the offline stub, as the app always ran), ``auto`` (live endpoint when
    configured, else the recording, else the stub: what the public demo uses), ``recorded`` or ``live``."""
    import os

    raw = ((os.environ if env is None else env).get(UI_LLM_ENV) or "").strip().lower()
    return raw if raw in MODES else MODE_FAKE


@dataclass(frozen=True)
class LlmStatus:
    """What the deployment is configured to do (not what a given draft got: see ``served_chip``)."""

    mode: str  # WAPSI_UI_LLM
    live: bool  # a live endpoint is configured and the mode allows it
    endpoint: EndpointConfig
    recording_entries: int = 0
    recording_writers: tuple[str, ...] = ()
    recorded_on: str | None = None

    @property
    def recorded(self) -> bool:
        """Replay is what a visitor gets: no live endpoint, and a recording to replay."""
        return not self.live and self.mode in (MODE_AUTO, MODE_RECORDED) and self.recording_entries > 0

    def describe(self) -> dict[str, Any]:
        """Non-secret summary (health output, tests)."""
        return {"mode": self.mode, "live": self.live, "endpoint": self.endpoint.describe(),
                "recording_entries": self.recording_entries, "recording_writers": list(self.recording_writers),
                "recorded_on": self.recorded_on}


def llm_status(env: Mapping[str, str] | None = None, recording: Any | None = None) -> LlmStatus:
    """``recording``: a ``wapsi.adapters.llm_replay.Recording`` (or ``None``)."""
    mode = ui_llm_mode(env)
    try:
        cfg = endpoint_config(env)
    except ValueError:
        cfg = EndpointConfig()
    summary = recording.summary() if recording is not None and len(recording) else {}
    return LlmStatus(mode=mode, live=cfg.configured and mode in (MODE_AUTO, MODE_LIVE), endpoint=cfg,
                     recording_entries=int(summary.get("entries") or 0),
                     recording_writers=tuple(summary.get("served_by") or ()),
                     recorded_on=summary.get("recorded_to"))


def _writers_text(names: Iterable[str]) -> str:
    labels = []
    for name in names:
        w = parse_served(name)
        if w is None:
            continue
        text = w.model or w.name
        if w.platform:
            text += f" on {w.platform}"
        if w.host:
            text += f" ({w.host})"
        labels.append(text)
    return " and ".join(dict.fromkeys(labels))


def banner(status: LlmStatus) -> tuple[str, str] | None:
    """(headline, detail) of the page banner, or ``None`` when there is nothing to disclose: the plain local app
    (``fake`` mode) and a deployment with a live endpoint."""
    if status.mode == MODE_FAKE or status.live:
        return None
    switched_off = status.endpoint.configured  # an endpoint is there, but this mode does not call it
    if switched_off:
        how = f"To call the model live, set {UI_LLM_ENV}=auto."
    else:
        missing = ", ".join(status.endpoint.missing or (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV))
        how = f"To call the model live, set {missing}."
    if status.recorded:
        who = _writers_text(status.recording_writers) or "an open model"
        when = f" on {status.recorded_on}" if status.recorded_on else ""
        return (BANNER_RECORDED_OFF if switched_off else BANNER_RECORDED,
                f"The prose in these drafts was written by {who}{when} and is replayed from that recording; cases "
                f"without a recorded answer use the offline stub, and each draft says which it got. {NUMBERS_NOTE} "
                f"{how}")
    return (BANNER_OFFLINE,
            f"No recording is bundled, so prose comes from the deterministic offline stub (no model call). "
            f"{NUMBERS_NOTE} {how}")


# --------------------------------------------------------------------------- what the drafts say


@dataclass(frozen=True)
class Chip:
    text: str
    tone: str  # good | accent | gray | warn (ui.components.html.chip)
    title: str = ""


@dataclass(frozen=True)
class Served:
    """Prose drafts of one case by who wrote them."""

    live: int = 0
    recorded: int = 0
    stub: int = 0
    other: int = 0
    fixed: int = 0  # the deterministic paragraph replaced the model's prose (llm_fallback)
    writers: tuple[Writer, ...] = ()  # distinct writers of the prose that was kept, most used first

    @property
    def total(self) -> int:
        return self.live + self.recorded + self.stub + self.other + self.fixed


def served(bundle: Any) -> Served:
    """Count the case's prose drafts by writer, from ``Draft.llm_model`` and ``structured["llm_fallback"]``."""
    counts: Counter[str] = Counter()
    writers: Counter[Writer] = Counter()
    for d in getattr(bundle, "drafts", None) or ():
        w = parse_served(getattr(d, "llm_model", None))
        if w is None:
            continue  # a draft without a prose field
        if bool((getattr(d, "structured", None) or {}).get("llm_fallback")) or w.kind == "template":
            counts["fixed"] += 1
            continue
        kind = {"endpoint": "live", "recorded": "recorded", "stub": "stub"}.get(w.kind, "other")
        counts[kind] += 1
        writers[w] += 1
    return Served(live=counts["live"], recorded=counts["recorded"], stub=counts["stub"], other=counts["other"],
                  fixed=counts["fixed"], writers=tuple(w for w, _ in writers.most_common()))


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def served_chip(bundle: Any) -> Chip | None:
    """The status chip for a case: which model and endpoint host served its drafts' prose, e.g. ``Gemma 4 on AMD
    via developer.amd.com.cn``. ``None`` when the case has no draft with LLM prose yet."""
    s = served(bundle)
    if not s.total:
        return None
    numbers = "Every number comes from case tables; the verifier recomputes them."
    if s.fixed == s.total:
        return Chip("Fixed paragraphs · model not reached", "warn",
                    f"The model's prose was rejected or the model could not be reached for {_n(s.fixed, 'draft')}, "
                    f"so the deterministic paragraph was used. {numbers}")
    main = s.writers[0]
    kinds = [k for k in ("live", "recorded", "stub", "other") if getattr(s, k)]
    full = f"{main.model}" + (f" on {main.platform}" if main.platform else "") if main.model else main.name
    where = f", served from {main.host}" if main.host else ""
    if kinds == ["live"] and not s.fixed:
        return Chip(main.label, "good",
                    f"Prose of {_n(s.live, 'draft')} written live by {full}{where}. {numbers}")
    if kinds == ["recorded"] and not s.fixed:
        when = f" on {main.recorded_on}" if main.recorded_on else ""
        return Chip(f"Recorded run · {main.label}", "accent",
                    f"Prose of {_n(s.recorded, 'draft')} was written by {full}{where}{when} and is replayed from "
                    f"that recording. {numbers}")
    if kinds == ["stub"] and not s.fixed:
        return Chip("Offline stub · no model call", "gray",
                    f"Prose of {_n(s.stub, 'draft')} comes from the deterministic offline stub. {numbers}")
    if kinds == ["other"] and not s.fixed:
        return Chip(main.label, "good", f"Prose of {_n(s.other, 'draft')} written by {main.name}. {numbers}")
    words = (("live", "live"), ("recorded", "recorded"), ("stub", "offline stub"), ("other", "other model"),
             ("fixed", "fixed"))
    parts = [f"{getattr(s, k)} {word}" for k, word in words if getattr(s, k)]
    lead = main.label if main.kind != "stub" else "Offline stub"
    return Chip(f"{lead} · {', '.join(parts)}", "warn" if s.fixed else "accent",
                f"This case's prose drafts by writer: {', '.join(parts)}. 'Fixed' is the deterministic paragraph "
                f"used when the model's prose was rejected or the model was not reached. {numbers}")


def writer_note(draft: Any) -> str:
    """One line under a draft: who wrote its prose."""
    w = parse_served(getattr(draft, "llm_model", None))
    tail = "every number is filled from case tables."
    if w is None:
        return tail[:1].upper() + tail[1:]
    if bool((getattr(draft, "structured", None) or {}).get("llm_fallback")) or w.kind == "template":
        return f"Prose: the fixed paragraph (the model's answer was rejected or the model was not reached); {tail}"
    if w.kind == "endpoint":
        return f"Prose by {w.label} (model {w.model}, live call); {tail}"
    if w.kind == "recorded":
        when = f" on {w.recorded_on}" if w.recorded_on else ""
        return f"Prose by {w.label} (model {w.model}), recorded{when} and replayed; {tail}"
    if w.kind == "stub":
        return f"Prose by the offline stub (no model call); {tail}"
    return f"Prose by {w.name}; {tail}"
