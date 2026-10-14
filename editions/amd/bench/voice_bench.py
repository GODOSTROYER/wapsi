"""Voice-intake benchmark: speech-to-text real-time factor and extraction latency on the open path.

    # Gemma 4 on the AMD Token Factory structures the transcripts (key and URL from the environment); the bundled
    # samples' scripts stand in for the recogniser, so only the LLM is measured
    uv run python -m editions.amd.bench.voice_bench --asr scripts --out editions/amd/bench/results/<stamp>
    # the whole open path as configured (WAPSI_ASR_BASE_URL endpoint, or local Whisper: ROCm GPU / CPU)
    uv run python -m editions.amd.bench.voice_bench --asr local --repeat 3
    # anywhere, offline (fake recogniser + fake LLM; checks the harness, measures nothing about any model)
    uv run python -m editions.amd.bench.voice_bench --fake

For every voice note (the bundled synthetic samples, or ``--audio`` files) it runs the real intake
(``wapsi.intake.intake_voice_note``: transcribe, extract, validate, mask, guard, reconcile against the sample's
ledger) and records:

- ``asr_s``, ``audio_s`` and the real-time factor ``asr_s / audio_s`` (below 1 = faster than real time);
- extraction latency, prompt / output tokens, the cost the endpoint reports, attempts and whether the JSON had
  to be repaired before it validated;
- whether the result is what the sample documents (matched NCRP ids, MATCHED / MISMATCH / NOT_FOUND counts,
  injection flag) and how many extracted values agree with the sample's expected answer.

Provenance is part of the record, never assumed: ``hardware.asr`` says where the recogniser ran (ROCm GPU, CPU,
an endpoint, or ``stand-in``), ``hardware.llm`` who served the text model, and ``provenance`` spells out what the
numbers are. Numbers from the AMD Token Factory are AMD-served but are not MI300X droplet benchmarks; CPU numbers
are rehearsal numbers. ``report.py`` copies those lines next to the table.

The text model is a chain (``wapsi.intake.llm_chain``): a model that fails its health probe or returns garbage
is skipped and the next one answers. Every run records ``structured_by`` (the model whose answer was accepted)
and ``models_tried``; the summary counts answers per model, so a number is never credited to a model that did
not produce it.

LLM calls are sequential and at least ``--min-interval`` seconds apart (the Token Factory allows 20 requests a
minute per team). ``--cache-dir`` keeps each LLM answer on disk with the latency measured when it was first made;
a replay is marked ``llm_cached`` and reuses that measurement instead of timing the cache. Health probes are
never cached.

Writes ``voice_bench.json`` + ``voice_bench.md`` (``--name``) into ``--out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from editions.amd.bench.common import (  # noqa: E402
    SCHEMA_VERSION,
    dist,
    env_info,
    md_table,
    results_dir,
    utc_now,
    write_json,
)
from wapsi.intake.llm_chain import PROBE_MAX_TOKENS  # noqa: E402
from wapsi.intake.open_voice import friendly_model  # noqa: E402

ASR_CHOICES = ("auto", "scripts", "endpoint", "local", "chat-audio")
# When the bundled samples are "received": shortly after the complaints they retell (August / September 2026).
# The date is part of the prompt, so a fixed one keeps relative dates stable and lets the answer cache replay.
DEFAULT_RECEIVED_AT = "2026-09-16T11:00:00+05:30"
SCALAR_FIELDS = ("victim_name", "phone", "bank", "account_last4", "date_time", "payment_mode", "fraud_type",
                 "beneficiary")  # fmt: skip


class Pacer:
    """Keeps live calls at least ``min_interval_s`` apart across every model of a chain (one shared rate limit)."""

    def __init__(self, min_interval_s: float = 0.0, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.min_interval_s = max(0.0, min_interval_s)
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None
        self.live_calls = 0

    def wait(self) -> None:
        if self._last is not None and self.min_interval_s:
            pause = self.min_interval_s - (self._clock() - self._last)
            if pause > 0:
                self._sleep(pause)

    def done(self) -> None:
        self._last = self._clock()
        self.live_calls += 1


class CachedLLM:
    """A text LLM whose answers are kept on disk (``wapsi.ports.LLM`` plus ``complete_with_usage``).

    The cache key is a hash of the model, the messages and the request options. Each entry stores the answer, the
    token usage, the reported cost and the latency measured on the live call, so a replay costs no request and
    still reports the original measurement (``calls[-1]["cached"]`` says which it was). Nothing secret is stored.
    """

    def __init__(self, inner: Any, cache_dir: Path | None, *, min_interval_s: float = 0.0,
                 pacer: Pacer | None = None, calls: list[dict[str, Any]] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.perf_counter) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", type(inner).__name__)
        self.model = getattr(inner, "model", self.name)
        self.base_url = getattr(inner, "base_url", "")
        self.json_mode = getattr(inner, "json_mode", None)
        self.cache_dir = cache_dir
        self.pacer = pacer or Pacer(min_interval_s, sleep=sleep, clock=clock)
        self._clock = clock
        self.calls: list[dict[str, Any]] = calls if calls is not None else []  # shared by a chain's wrappers
        self.last_cost_usd: float | None = None
        self.last_usage: Any = None

    def _key(self, messages: Sequence[Mapping[str, str]], max_tokens: int, extra: Mapping[str, Any] | None) -> str:
        blob = json.dumps({"model": self.model, "messages": [dict(m) for m in messages], "max_tokens": max_tokens,
                           "extra": dict(extra or {})}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete_with_usage(self, messages: Sequence[Mapping[str, str]], *, temperature: float = 0.0,
                            max_tokens: int = 1024, json_schema: dict | None = None,
                            extra_body: Mapping[str, Any] | None = None) -> tuple[str, Any]:
        from wapsi.adapters.llm_openai_compat import TokenUsage

        probe = max_tokens <= PROBE_MAX_TOKENS  # a health probe asks about now: never replayed, never stored
        path = None
        if self.cache_dir and not probe:
            path = self.cache_dir / f"{self._key(messages, max_tokens, extra_body)}.json"
        if path is not None and path.exists():
            hit = json.loads(path.read_text(encoding="utf-8"))
            usage = TokenUsage(hit.get("prompt_tokens") or 0, hit.get("completion_tokens") or 0,
                               (hit.get("prompt_tokens") or 0) + (hit.get("completion_tokens") or 0), 1)
            self.last_usage, self.last_cost_usd = usage, hit.get("cost_usd")
            self.calls.append({**{k: hit.get(k) for k in ("elapsed_s", "prompt_tokens", "completion_tokens",
                                                          "cost_usd", "measured_at")},
                               "model": self.model, "cached": True, "probe": False})
            return hit["text"], usage
        self.pacer.wait()
        t0 = self._clock()
        try:
            if callable(getattr(self.inner, "complete_with_usage", None)):
                text, usage = self.inner.complete_with_usage(messages, temperature=temperature,
                                                             max_tokens=max_tokens, extra_body=extra_body)
            else:
                text = self.inner.complete(messages, temperature=temperature, max_tokens=max_tokens,
                                           json_schema=json_schema)
                usage = None
        finally:
            self.pacer.done()
        elapsed = round(self._clock() - t0, 4)
        cost = getattr(self.inner, "last_cost_usd", None)
        entry = {"text": text, "elapsed_s": elapsed, "prompt_tokens": getattr(usage, "prompt_tokens", None),
                 "completion_tokens": getattr(usage, "completion_tokens", None), "cost_usd": cost,
                 "model": self.model, "measured_at": utc_now().isoformat()}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        self.last_usage, self.last_cost_usd = usage, cost
        self.calls.append({k: v for k, v in entry.items() if k != "text"} | {"cached": False, "probe": probe})
        return text, usage

    def complete(self, messages: Sequence[Mapping[str, str]], *, temperature: float = 0.0, max_tokens: int = 1024,
                 json_schema: dict | None = None, extra_body: Mapping[str, Any] | None = None) -> str:
        return self.complete_with_usage(messages, temperature=temperature, max_tokens=max_tokens,
                                        json_schema=json_schema, extra_body=extra_body)[0]


# --------------------------------------------------------------------------- inputs


def _ledgers() -> Callable[[str], Any]:
    cache: dict[str, Any] = {}

    def get(world: str) -> Any:
        if world not in cache:
            if world == "demo":  # the synthetic world the demo-world samples retell (seed 7; built in memory)
                from wapsi.synth.export import frames_to_memory_ledger
                from wapsi.synth.world import WorldConfig, build_world

                cache[world] = frames_to_memory_ledger(build_world(WorldConfig(name="demo", size="demo", seed=7))[0])
            else:
                from wapsi.fixtures.showcase import showcase_ledger

                cache[world] = showcase_ledger()
        return cache[world]

    return get


def load_items(sample_ids: Sequence[str] | None, audio: Sequence[str], world: str) -> list[dict[str, Any]]:
    """What to read: bundled samples (all, or the ids given) and / or audio files (reconciled against ``world``)."""
    from wapsi.intake.samples import load_samples

    items: list[dict[str, Any]] = []
    if sample_ids is not None:
        known = {s.sample_id: s for s in load_samples()}
        missing = [i for i in sample_ids if i not in known]
        if missing:
            raise SystemExit(f"unknown sample(s): {', '.join(missing)}; available: {', '.join(known)}")
        for s in (known[i] for i in sample_ids) if sample_ids else known.values():
            items.append({"id": s.sample_id, "audio": s.read_bytes(), "filename": s.filename, "world": s.world,
                          "sample": s})
    for path in audio:
        p = Path(path)
        items.append({"id": p.name, "audio": p.read_bytes(), "filename": p.name, "world": world, "sample": None})
    return items


class CallLog:
    """What ``build_model`` returns next to the model: every text-LLM call of the chain, in order."""

    def __init__(self, calls: list[dict[str, Any]], pacer: Pacer) -> None:
        self.calls = calls
        self.pacer = pacer


def build_model(asr: str, *, fake: bool, cache_dir: Path | None, min_interval_s: float,
                env: Mapping[str, str] | None = None, text_llm: Any | None = None,
                recogniser: Any | None = None) -> tuple[Any, CallLog | None]:
    """The open voice model to measure and the log of its text-LLM calls (None for ``--fake``). Every model of
    the chain is wrapped in the answer cache and shares one pacer."""
    from wapsi.intake import asr as A
    from wapsi.intake.backends import (
        VOICE_BACKEND_ENV,
        VoiceChoice,
        build_asr,
        build_voice_model,
        llm_provider,
        resolve_asr,
        text_model_chain,
    )
    from wapsi.intake.extract import TranscriptExtractor
    from wapsi.intake.open_voice import FakeTranscriptLLM, OpenVoiceLLM

    e = dict(os.environ if env is None else env)
    if fake:
        return OpenVoiceLLM(A.FakeASR(), TranscriptExtractor(FakeTranscriptLLM())), None
    pacer, calls = Pacer(min_interval_s), []
    chain = text_model_chain(e, text_llm=text_llm,
                             wrap=lambda m: CachedLLM(m, cache_dir, pacer=pacer, calls=calls))
    inner = chain.primary
    base = str(getattr(inner, "base_url", "") or "")
    if recogniser is None:
        if asr == "scripts":
            recogniser = A.FakeASR(name="sample scripts")
        else:
            kind = asr
            if asr == "auto":
                kind, _ = resolve_asr(e, explicit=True, llm_base_url=base, llm_model=str(getattr(inner, "model", "")))
                if not kind:
                    raise SystemExit("no speech recogniser is configured: set WAPSI_ASR_BASE_URL, install the asr "
                                     "extra and pass --asr local, or pass --asr scripts to measure the LLM alone")
            recogniser = build_asr(kind, e, llm_base_url=base, llm_model=str(getattr(inner, "model", "")))
    served_by, on_amd = llm_provider(base, e)
    e[VOICE_BACKEND_ENV] = "open"
    model = build_voice_model(e, choice=VoiceChoice("open", "open", asr=getattr(recogniser, "backend", "")),
                              text_llm=chain, asr=recogniser, served_by=served_by, llm_on_amd=on_amd)
    return model, CallLog(calls, pacer)


# --------------------------------------------------------------------------- measuring


def _agreement(result: Any, expected: Mapping[str, Any]) -> tuple[int, int, list[str]]:
    """(values that agree, values compared, what differs) against a sample's expected answer: the scalar fields
    (the phone compared after masking), the set of rupee amounts and the set of transaction references."""
    from wapsi.intake.voice import mask_phone, normalize_ref

    want_fields = expected.get("fields") or {}
    agree, total, diffs = 0, 0, []
    for key in SCALAR_FIELDS:
        want = str((want_fields.get(key) or {}).get("value") or "")
        if key == "phone":
            want = mask_phone(want)
        got_field = result.field(key)
        got = got_field.value if got_field is not None else ""
        if key == "date_time":  # the same instant, however it is written
            from wapsi.intake.reconcile import parse_claimed_dt

            same = parse_claimed_dt(got) == parse_claimed_dt(want)
        else:
            same = " ".join(got.lower().split()) == " ".join(want.lower().split())
        total += 1
        agree += 1 if same else 0
        if not same:
            diffs.append(f"{key}: got {got!r}, expected {want!r}")
    want_amounts = sorted(float(a["value"]) for a in expected.get("amounts") or [])
    got_amounts = sorted(float(a.value) for a in result.amounts)
    want_refs = sorted(normalize_ref(r["ref"]) for r in expected.get("txn_refs") or [])
    got_refs = sorted(r.ref for r in result.txn_refs)
    for name, got_set, want_set in (("amounts", got_amounts, want_amounts), ("txn_refs", got_refs, want_refs)):
        total += 1
        agree += 1 if got_set == want_set else 0
        if got_set != want_set:
            diffs.append(f"{name}: got {got_set}, expected {want_set}")
    return agree, total, diffs


def run_one(model: Any, item: Mapping[str, Any], ledger: Any, llm: CallLog | None,
            clock: Callable[[], float] = time.perf_counter, received_at: Any = None) -> dict[str, Any]:
    from wapsi.adapters.llm_openai_compat import LLMError
    from wapsi.intake.voice import VoiceIntakeError, intake_voice_note
    from wapsi.verify.injection import RulesGuard

    calls_before = len(llm.calls) if llm is not None else 0
    rec: dict[str, Any] = {"id": item["id"], "world": item["world"], "ok": False}
    t0 = clock()
    try:
        r = intake_voice_note(item["audio"], llm=model, ledger=ledger, guard=RulesGuard(), received_at=received_at,
                              filename=item["filename"], sample_id=item["id"] if item.get("sample") else None)
    except (VoiceIntakeError, LLMError) as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["total_s"] = round(clock() - t0, 4)
        return rec
    p = r.pipeline
    calls = [c for c in llm.calls[calls_before:] if not c.get("probe")] if llm is not None else []
    cached = bool(calls) and all(c["cached"] for c in calls)
    # the latency of the model calls themselves: not the rate-limit wait before a live call, and for a replayed
    # answer the latency measured when the call was first made, never the cache read
    extract_s = round(sum(c.get("elapsed_s") or 0.0 for c in calls), 4) if calls else (p.extract_s if p else None)
    rec.update(
        ok=True,
        audio_s=p.audio_s if p else r.duration_s,
        asr_s=p.asr_s if p else None,
        rtf=p.rtf if p else None,
        asr_language=p.asr_language if p else "",
        extract_s=extract_s,
        llm_cached=cached,
        llm_measured_at=next((c.get("measured_at") for c in calls if c.get("measured_at")), None),
        total_s=round((p.asr_s or 0.0) + (extract_s or 0.0), 4) if p else round(clock() - t0, 4),
        prompt_tokens=p.prompt_tokens if p else None,
        completion_tokens=p.completion_tokens if p else None,
        cost_usd=p.cost_usd if p else None,
        attempts=p.attempts if p else None,
        repaired=p.repaired if p else None,
        fixes=list(p.fixes) if p else [],
        json_mode=p.json_mode if p else "",
        structured_by=p.structured_by if p else r.extracted_by,
        models_tried=list(p.models_tried) if p else [],
        failover=bool(p.failover) if p else False,
        matched_ncrp_ids=list(r.matched_ncrp_ids),
        counts=r.counts(),
        injection_flagged=r.injection_flagged,
        segments=len(r.segments),
    )
    sample = item.get("sample")
    if sample is not None:
        expect = sample.expect
        rec["as_documented"] = (rec["matched_ncrp_ids"] == list(expect.get("matched", []))
                                and rec["counts"] == expect.get("counts")
                                and rec["injection_flagged"] is bool(expect.get("injection")))
        rec["values_agree"], rec["values_compared"], rec["differences"] = _agreement(r, sample.expected)
    return rec


def _hardware(model: Any) -> dict[str, Any]:
    d = model.describe()
    asr = model.asr
    info: dict[str, Any] = {"model": d["transcribed_by"], "backend": d["asr_backend"], "on_amd": d["asr_on_amd"]}
    hw = getattr(asr, "_hardware", None)  # known once a local model has run; never forces a torch import
    if isinstance(hw, dict):
        info.update({k: hw.get(k) for k in ("device", "accelerator", "gpu_name", "torch", "rocm") if hw.get(k)})
        info["on_amd"] = bool(hw.get("on_amd"))
    if getattr(asr, "load_s", None) is not None:
        info["load_s"] = asr.load_s
    return {"asr": info, "llm": {"model": d["structured_by"], "served_by": d["served_by"],
                                 "on_amd": d["llm_on_amd"], "json_mode": d["json_mode"]}}


def provenance(hardware: Mapping[str, Any], *, fake: bool) -> list[str]:
    """Plain statements of what the numbers are (copied into the report next to the table)."""
    if fake:
        return ["Offline harness check: a fake recogniser and a fake text model. No model ran; these are not "
                "measurements of any hardware."]
    asr, llm = hardware["asr"], hardware["llm"]
    lines = []
    if asr["backend"] == "fake":
        lines.append("Speech-to-text: stand-in (the samples' scripts were used as transcripts). No speech model "
                     "ran, so there is no real-time factor in this run.")
    elif asr.get("accelerator") == "rocm":
        lines.append(f"Speech-to-text: {asr['model']} on an AMD GPU through ROCm ({asr.get('gpu_name') or 'GPU'}).")
    elif asr["backend"] == "local":
        lines.append(f"Speech-to-text: {asr['model']} on {asr.get('device') or 'this machine'}. CPU / non-AMD "
                     "rehearsal number, not an AMD GPU measurement.")
    elif asr["on_amd"]:
        lines.append(f"Speech-to-text: {asr['model']} at an endpoint the operator marked as running on AMD.")
    else:
        lines.append(f"Speech-to-text: {asr['model']} at an endpoint; where it runs is not established here.")
    answered = llm.get("answered") or {}
    who = ", ".join(f"{m} ({n} answer{'s' if n != 1 else ''})" for m, n in answered.items()) or llm["model"]
    if llm["on_amd"] and llm["served_by"] == "AMD Token Factory":
        lines.append(f"Translation and field extraction: {who}, served by the AMD Token Factory (AMD GPUs on the "
                     "provider side). Latency includes the network; this is not an MI300X droplet benchmark.")
    elif llm["on_amd"]:
        lines.append(f"Translation and field extraction: {who}, served by {llm['served_by']}.")
    else:
        lines.append(f"Translation and field extraction: {who} at {llm['served_by']}; not established as AMD "
                     "hardware.")
    if list(answered) not in ([], [llm["model"]]):
        lines.append(f"The first model in the chain is {llm['model']}; where another model answered, the first "
                     "one failed its health probe or returned unusable output and was rejected (see models_tried).")
    return lines


def summarise(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in runs if r.get("ok")]
    documented = [r for r in ok if "as_documented" in r]
    agree = sum(r.get("values_agree") or 0 for r in documented)
    compared = sum(r.get("values_compared") or 0 for r in documented)
    costs = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]
    return {
        "runs": len(runs),
        "ok": len(ok),
        "errors": len(runs) - len(ok),
        "audio_s_total": round(sum(r.get("audio_s") or 0.0 for r in ok), 2),
        "asr_rtf": dist(r.get("rtf") for r in ok if r.get("rtf") is not None),
        "asr_s": dist(r.get("asr_s") for r in ok if r.get("rtf") is not None),
        "extract_s": dist(r.get("extract_s") for r in ok if r.get("extract_s") is not None),
        "total_s": dist(r.get("total_s") for r in ok if r.get("total_s") is not None),
        "prompt_tokens": dist((r.get("prompt_tokens") for r in ok if r.get("prompt_tokens") is not None), 1),
        "completion_tokens": dist((r.get("completion_tokens") for r in ok
                                   if r.get("completion_tokens") is not None), 1),
        "cost_usd_total": round(sum(costs), 8) if costs else None,
        "llm_cached_runs": sum(1 for r in ok if r.get("llm_cached")),
        "repaired_runs": sum(1 for r in ok if r.get("repaired")),
        "retried_runs": sum(1 for r in ok if (r.get("attempts") or 1) > 1),
        "failover_runs": sum(1 for r in ok if r.get("failover")),
        "answers_by_model": {m: sum(1 for r in ok if r.get("structured_by") == m)
                             for m in sorted({str(r.get("structured_by")) for r in ok})},
        "extract_s_by_model": {m: dist(r.get("extract_s") for r in ok
                                       if r.get("structured_by") == m and r.get("extract_s") is not None)
                               for m in sorted({str(r.get("structured_by")) for r in ok})},
        "samples_as_documented": sum(1 for r in documented if r["as_documented"]),
        "samples_checked": len(documented),
        "value_agreement": round(agree / compared, 4) if compared else None,
    }


def run_bench(items: Sequence[Mapping[str, Any]], model: Any, llm: CallLog | None, *, repeat: int = 1,
              fake: bool = False, asr: str = "auto", min_interval_s: float = 0.0, progress: bool = False,
              received_at: str | None = DEFAULT_RECEIVED_AT, record_host: bool = False) -> dict[str, Any]:
    """``received_at`` (ISO 8601) is when the notes count as received; ``None`` = now (the prompt then changes
    every minute and nothing replays from the cache). The client's host name is left out unless ``record_host``:
    this bench often runs on a developer's own machine and its results are committed."""
    from datetime import datetime

    received = datetime.fromisoformat(received_at) if received_at else None
    ledger_for = _ledgers()
    started = utc_now()
    runs: list[dict[str, Any]] = []
    for n in range(max(1, repeat)):
        for item in items:
            rec = run_one(model, item, ledger_for(item["world"]), llm, received_at=received)
            rec["repeat"] = n
            runs.append(rec)
            if progress:
                state = "ok" if rec["ok"] else f"ERROR {rec.get('error')}"
                print(f"  {rec['id']:<30} {state}  rtf {rec.get('rtf')}  extract {rec.get('extract_s')} s  "
                      f"by {rec.get('structured_by')}  as documented: {rec.get('as_documented')}", flush=True)
    hardware = _hardware(model)
    metrics = summarise(runs)
    hardware["llm"]["answered"] = metrics["answers_by_model"]
    answered = list(metrics["answers_by_model"])  # the label names the model(s) that actually answered
    label = model.label(llm_model=" / ".join(friendly_model(m) for m in answered)) if answered else model.label()
    return {
        "kind": "voice_bench",
        "schema_version": SCHEMA_VERSION,
        "generated_at": started.isoformat(),
        "settings": {"asr": "fake" if fake else asr, "repeat": max(1, repeat), "min_interval_s": min_interval_s,
                     "items": [i["id"] for i in items], "fake": fake, "received_at": received_at or "now"},
        "label": label,
        "hardware": hardware,
        "provenance": provenance(hardware, fake=fake),
        "env": env_info() if record_host else {**env_info(), "host": "not recorded"},
        "metrics": metrics,
        "runs": runs,
    }


def to_markdown(result: Mapping[str, Any]) -> str:
    m, hw = result["metrics"], result["hardware"]
    head = [
        ["Voice notes read", f"{m['ok']}/{m['runs']}"],
        ["ASR real-time factor p50 / p95", f"{_cell(m['asr_rtf']['p50'])} / {_cell(m['asr_rtf']['p95'])}"],
        ["ASR seconds p50", m["asr_s"]["p50"]],
        ["Extraction latency p50 / p95 (s)", f"{_cell(m['extract_s']['p50'])} / {_cell(m['extract_s']['p95'])}"],
        ["Voice note to checked fields p50 (s)", m["total_s"]["p50"]],
        ["Prompt / output tokens (mean)", f"{_cell(m['prompt_tokens']['mean'])} / "
                                           f"{_cell(m['completion_tokens']['mean'])}"],
        ["Reported cost, all runs (USD)", m["cost_usd_total"]],
        ["Samples reconciled as documented", f"{m['samples_as_documented']}/{m['samples_checked']}"],
        ["Extracted values agreeing with the expected answer", m["value_agreement"]],
        ["Answers repaired before validation / retried", f"{m['repaired_runs']} / {m['retried_runs']}"],
        ["Answers by model", ", ".join(f"{k}: {v}" for k, v in m["answers_by_model"].items()) or None],
        ["Runs answered by a fallback model", m["failover_runs"]],
        ["Runs replayed from the LLM cache", m["llm_cached_runs"]],
    ]
    rows = [[r["id"], r.get("audio_s"), r.get("asr_s") if r.get("rtf") is not None else None, r.get("rtf"),
             r.get("structured_by"), r.get("extract_s"), r.get("prompt_tokens"), r.get("completion_tokens"), r.get("attempts"), r.get("as_documented"),
             f"{r.get('values_agree')}/{r.get('values_compared')}" if r.get("values_compared") else None,
             r.get("error") or ("cached" if r.get("llm_cached") else "")] for r in result["runs"]]
    return "\n".join([
        f"# Voice intake benchmark — {result['label']}",
        "",
        f"- Speech-to-text: `{hw['asr']['model']}` ({hw['asr']['backend']}"
        + (f", {hw['asr'].get('device')}" if hw["asr"].get("device") else "") + ")"
        f" · text model: `{hw['llm']['model']}` served by {hw['llm']['served_by']} · JSON mode "
        f"{hw['llm']['json_mode']} · generated {result['generated_at']}",
        *[f"- {line}" for line in result["provenance"]],
        "",
        md_table(["Metric", "Value"], head),
        "",
        md_table(["Voice note", "Audio (s)", "ASR (s)", "RTF", "Text model", "Extract (s)", "Prompt tok",
                  "Output tok", "Attempts", "As documented", "Values agree", "Note"], rows),
        "",
    ])


def _cell(v: Any) -> str:
    return "—" if v is None else (f"{v:,.3f}" if isinstance(v, float) else str(v))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asr", choices=ASR_CHOICES, default="auto",
                    help="recogniser: auto (as configured), scripts (sample scripts stand in: measures the LLM "
                         "alone), endpoint, local, chat-audio")
    ap.add_argument("--samples", nargs="*", default=None, help="bundled sample ids (default: all, unless --audio)")
    ap.add_argument("--audio", nargs="*", default=[], help="audio files to read as well")
    ap.add_argument("--world", choices=["showcase", "demo"], default="showcase",
                    help="ledger that --audio files are reconciled against")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--received-at", default=DEFAULT_RECEIVED_AT,
                    help="when the notes count as received (ISO 8601; 'now' for the current time). A fixed date "
                         "keeps the prompt identical between runs, so cached answers can replay")
    ap.add_argument("--min-interval", type=float, default=4.0, help="seconds between live LLM calls")
    ap.add_argument("--cache-dir", default=str(Path(__file__).resolve().parent / ".cache" / "voice"),
                    help="LLM answer cache ('' disables it)")
    ap.add_argument("--fake", action="store_true", help="offline harness check (fake recogniser + fake LLM)")
    ap.add_argument("--record-host", action="store_true",
                    help="record this machine's host name (off by default: results are committed)")
    ap.add_argument("--out", default=None, help="results directory (default bench/results/<utc stamp>)")
    ap.add_argument("--name", default="voice_bench", help="output file stem (report.py picks up voice_bench*.json)")
    args = ap.parse_args(argv)
    if not args.name.startswith("voice_bench"):
        ap.error("--name must start with voice_bench")
    sample_ids = args.samples if args.samples is not None else (None if args.audio else [])
    items = load_items(sample_ids, args.audio, args.world)
    if not items:
        ap.error("nothing to read: no bundled samples in this build and no --audio")
    cache = Path(args.cache_dir) if args.cache_dir and not args.fake else None
    model, llm = build_model(args.asr, fake=args.fake, cache_dir=cache, min_interval_s=args.min_interval)
    print(f"reading {len(items)} voice note(s) x {max(1, args.repeat)} with {model.label()}", flush=True)
    result = run_bench(items, model, llm, repeat=args.repeat, fake=args.fake, asr=args.asr,
                       min_interval_s=args.min_interval, progress=True,
                       received_at=None if args.received_at.strip().lower() == "now" else args.received_at,
                       record_host=args.record_host)
    out = results_dir(args.out)
    write_json(out / f"{args.name}.json", result)
    (out / f"{args.name}.md").write_text(to_markdown(result), encoding="utf-8", newline="\n")
    print(to_markdown(result))
    print(f"wrote {out / (args.name + '.json')}")
    return 0 if result["metrics"]["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
