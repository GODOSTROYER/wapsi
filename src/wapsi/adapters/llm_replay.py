"""Recorded prose for the public demo, and the LLM the demo uses: live endpoint, else the recording, else the stub.

A public demo must stay up when the model endpoint is rate limited, down or not configured at all. So the prose an
open model on AMD wrote for the demo cases is **recorded** once (``scripts/record_demo_prose.py``) and replayed when
the live endpoint cannot answer. A replay is always labelled as one: the draft records
``<model> on <platform> @ <host> (recorded <date>)`` as its writer (``wapsi.adapters.llm_env.served_name``), the UI
shows "Recorded run", and the audit chain seals that name with the draft.

**What a recording holds.** One entry per prose field: the model's accepted answer (the ``{"text": ...}`` JSON whose
text uses ``⟦slot⟧`` placeholders, never a number), keyed by ``prose_signature``: the SHA-256 of the task name and
the placeholders offered (names and values). The key ignores the wording of the prompt, so rewording an instruction
does not orphan a recording, and it changes whenever the case's figures change, so a stale answer is never replayed
onto different numbers. Numbers are not in the recording at all: they are slots filled from the ledger at replay
time, and the replayed prose goes through the same validation and the same verifier as a live answer.

**``DemoLLM``** (implements ``wapsi.ports.LLM``) tries its sources in order and says who answered:

- ``prefer="live"``: live endpoint -> recording -> stub;  ``prefer="recorded"``: recording -> live endpoint -> stub.
- A live failure closes a shared ``LiveGate`` for a cool-down (the server's ``Retry-After`` when it sent one), so a
  rate-limited endpoint is not hammered and the page does not hang on every draft. A live answer that is not prose
  at all (HTTP 200 with degenerate output, ``prose_answer_problem``) counts as a failure too.
- ``name`` is the writer of the latest answer; the drafter reads it right after each prose call, so every draft
  records its own writer.

All data is synthetic. A recording is replay material for the demo, never training data (AGENTS.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wapsi.adapters.llm_env import RECORDED_NOTE

RECORDING_VERSION = 1
RECORDING_PURPOSE = "demo replay recording of LLM prose (placeholders only, synthetic cases); never training data"
RECORDING_ENV = "WAPSI_PROSE_RECORDING"
DEFAULT_COOLDOWN_S = 60.0
LIVE, RECORDED, STUB = "live", "recorded", "stub"


def prose_signature(messages: Sequence[Mapping[str, str]]) -> str | None:
    """Key of a ``wapsi.draft.prose`` request: SHA-256 over its task name and placeholders (name, value). ``None``
    when the messages are not such a request (another kind of call is never replayed)."""
    for m in messages:
        if m.get("role") != "user":
            continue
        try:
            data = json.loads(m.get("content", ""))
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("task"), str):
            return None
        placeholders = data.get("placeholders")
        if not isinstance(placeholders, list):
            return None
        pairs = [[str(p.get("name", "")), str(p.get("value", ""))] for p in placeholders if isinstance(p, dict)]
        blob = json.dumps({"v": RECORDING_VERSION, "task": data["task"], "placeholders": pairs}, sort_keys=True,
                          ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return None


def prose_answer_problem(messages: Sequence[Mapping[str, str]], text: str) -> str | None:
    """Why a model's answer to a prose request is unusable, or ``None``: an endpoint can return HTTP 200 with
    degenerate output (observed on a shared endpoint: a run of slashes instead of a paragraph). Such an answer is
    never cached, recorded or shown; the next source answers instead. Other kinds of request are not judged."""
    if prose_signature(messages) is None:
        return None
    from wapsi.draft.prose import not_prose_reason, parse_response

    parsed = parse_response(text)
    if not parsed.strip():
        return "the answer is empty"
    return not_prose_reason(parsed)


@dataclass
class Recording:
    """Recorded prose answers by ``prose_signature`` (one JSON file; see the module docstring)."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None) -> Recording:
        """The recording at ``path``; an empty one when the file is missing or unreadable (the demo then uses the
        stub and says so)."""
        if not path:
            return cls()
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=p)
        if not isinstance(data, dict) or data.get("v") != RECORDING_VERSION:
            return cls(path=p)
        entries = {k: v for k, v in (data.get("entries") or {}).items()
                   if isinstance(v, dict) and isinstance(v.get("text"), str) and v.get("served_by")}
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return cls(entries=entries, meta=dict(meta), path=p)

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else self.path
        if p is None:
            raise ValueError("no path to save the recording to")
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"v": RECORDING_VERSION, "purpose": RECORDING_PURPOSE, "meta": self.meta,
                "entries": dict(sorted(self.entries.items()))}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, p)
        self.path = p
        return p

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, signature: str | None) -> dict[str, Any] | None:
        return self.entries.get(signature) if signature else None

    def put(self, signature: str, text: str, *, served_by: str, task: str = "", case_id: str | None = None,
            draft_id: str | None = None, attempts: int = 1, recorded_at: datetime | None = None,
            **extra: Any) -> None:
        """``served_by``: the live writer's name (``served_name`` without a recorded note). ``extra``: measured,
        non-secret facts about the call (``latency_s``, ``usage``)."""
        at = (recorded_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        self.entries[signature] = {"text": text, "served_by": served_by, "recorded_at": at.isoformat(), "task": task,
                                   "case_id": case_id, "draft_id": draft_id, "attempts": int(attempts),
                                   **{k: v for k, v in extra.items() if v is not None}}

    @staticmethod
    def writer_name(entry: Mapping[str, Any]) -> str:
        """The name a replay of ``entry`` records: its live writer plus ``(recorded <date>)``."""
        return f"{entry['served_by']} ({RECORDED_NOTE} {str(entry.get('recorded_at') or '')[:10]})".replace(" )", ")")

    def summary(self) -> dict[str, Any]:
        """Entries, the distinct live writers and the date range (for the banner and the health page)."""
        dates = sorted(str(e.get("recorded_at") or "")[:10] for e in self.entries.values() if e.get("recorded_at"))
        return {"entries": len(self.entries),
                "served_by": sorted({str(e["served_by"]) for e in self.entries.values()}),
                "cases": len({e.get("case_id") for e in self.entries.values() if e.get("case_id")}),
                "recorded_from": dates[0] if dates else None, "recorded_to": dates[-1] if dates else None}


class UnusableLiveAnswer(RuntimeError):
    """The live endpoint answered with something that is not prose (``prose_answer_problem``)."""


class LiveGate:
    """Shared switch that keeps callers off a failing live endpoint for a cool-down (thread-safe)."""

    def __init__(self, cooldown_s: float = DEFAULT_COOLDOWN_S, *, max_cooldown_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cooldown_s = cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self._clock = clock
        self._until = 0.0
        self._lock = threading.Lock()
        self.last_error: str | None = None  # class and status only; never a body or a key
        self.trips = 0

    def is_open(self) -> bool:
        with self._lock:
            return self._clock() >= self._until

    def trip(self, exc: BaseException | None = None) -> None:
        hinted = getattr(exc, "retry_after_s", None)
        wait = float(hinted) if isinstance(hinted, int | float) and not isinstance(hinted, bool) else self.cooldown_s
        wait = max(1.0, min(wait, self.max_cooldown_s))
        with self._lock:
            self._until = max(self._until, self._clock() + wait)
            self.trips += 1
            if exc is not None:
                code = getattr(exc, "status_code", None)
                self.last_error = type(exc).__name__ + (f" (HTTP {code})" if code else "")

    def reset(self) -> None:
        with self._lock:
            self._until = 0.0


@dataclass
class Answer:
    """One ``DemoLLM.complete`` call: who answered and how long the live call took."""

    source: str  # live | recorded | stub
    name: str
    signature: str | None = None
    latency_s: float | None = None
    from_cache: bool = False


class DemoLLM:
    """``wapsi.ports.LLM`` for the demo: live endpoint, recording and stub in the order ``prefer`` gives (see the
    module docstring). ``name`` names the writer of the latest answer. Not shared between threads: build one per
    pipeline run (the recording, the live client and the gate are shared)."""

    def __init__(self, *, live: Any | None = None, recording: Recording | None = None, stub: Any | None = None,
                 prefer: str = LIVE, gate: LiveGate | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        if prefer not in (LIVE, RECORDED):
            raise ValueError("prefer must be 'live' or 'recorded'")
        self.live = live
        self.recording = recording if recording is not None and len(recording) else None
        self.stub = stub
        self.prefer = prefer
        self.gate = gate or LiveGate()
        self._clock = clock
        self.answers: list[Answer] = []
        self.counts: Counter[str] = Counter()
        self.last_error: BaseException | None = None
        self._name = self._idle_name()

    def _idle_name(self) -> str:
        for source in self._order():
            if source == LIVE and self.live is not None:
                return str(getattr(self.live, "name", "live endpoint"))
            if source == RECORDED and self.recording is not None:
                return "recorded run"
            if source == STUB and self.stub is not None:
                return str(getattr(self.stub, "name", "offline stub"))
        return "no model"

    def _order(self) -> tuple[str, ...]:
        return (LIVE, RECORDED, STUB) if self.prefer == LIVE else (RECORDED, LIVE, STUB)

    @property
    def name(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return (f"DemoLLM(prefer={self.prefer!r}, live={getattr(self.live, 'name', None)!r}, "
                f"recorded={len(self.recording) if self.recording else 0}, stub={getattr(self.stub, 'name', None)!r})")

    def _done(self, answer: Answer, text: str) -> str:
        self.answers.append(answer)
        self.counts[answer.source] += 1
        self._name = answer.name
        return text

    def complete(self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0, max_tokens: int = 1024,
                 json_schema: dict | None = None) -> str:
        signature = prose_signature(messages)
        # a recorded answer is offered once per prose field: a retry (the drafter's feedback turn follows the
        # first two messages) means it was rejected, e.g. the recording is stale for this build
        entry = self.recording.get(signature) if self.recording is not None and len(messages) <= 2 else None
        for source in self._order():
            if source == RECORDED and entry is not None:
                return self._done(Answer(RECORDED, Recording.writer_name(entry), signature), entry["text"])
            if source == LIVE and self.live is not None and self.gate.is_open():
                started = self._clock()
                try:
                    text = self.live.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                              json_schema=json_schema)
                    problem = prose_answer_problem(messages, text)
                    if problem:
                        raise UnusableLiveAnswer(f"the live model's answer is unusable: {problem}")
                except Exception as exc:  # noqa: BLE001 - any live failure fails over to the next source
                    self.last_error = exc
                    self.counts["live_failed"] += 1
                    self.gate.trip(exc)
                    continue
                return self._done(Answer(LIVE, str(getattr(self.live, "name", "live endpoint")), signature,
                                         latency_s=round(self._clock() - started, 3),
                                         from_cache=bool(getattr(self.live, "last_from_cache", False))), text)
            if source == STUB and self.stub is not None:
                text = self.stub.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                          json_schema=json_schema)
                return self._done(Answer(STUB, str(getattr(self.stub, "name", "offline stub")), signature), text)
        if self.last_error is not None:
            raise self.last_error
        raise RuntimeError("no model, recording or stub can answer this request")
