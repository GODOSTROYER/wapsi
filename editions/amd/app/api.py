"""Small FastAPI for the AMD edition (automation, benchmarks, health).

    uvicorn editions.amd.app.api:app --host 0.0.0.0 --port 8080

Endpoints
    GET  /healthz                     liveness + non-secret configuration (no network calls)
    GET  /cases                       processed cases in the store (newest first)
    GET  /cases/{case_id}             one case summary (``?full=1`` returns the whole CaseBundle)
    POST /cases/{ids}/process         run the pipeline: ``ids`` = NCRP id(s), comma-separated, or ``CASE-<ncrp>``;
                                      ``?draft=0`` stops after the hold plan
    GET  /intake/voice                which models read voice notes (speech-to-text + text LLM) and the bundled samples
    POST /intake/voice                read a voice note: raw audio bytes as the request body (``Content-Type: audio/*``,
                                      WAV / MP3 / M4A / OGG / WebM, up to 15 MB) or ``?sample=<id>`` for a bundled
                                      synthetic sample; returns transcript, translation, fields, the ledger
                                      reconciliation and the pipeline timings (ASR real-time factor, extraction latency)
    GET  /metrics                     Prometheus text format (counters, latency sums, GNN / judge / voice stats)

Port 8080 is bound to 127.0.0.1 by ``docker-compose.yml``. If ``WAPSI_API_TOKEN`` is set, POST requires
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from .services import Services, get_services, process, resolve_ncrp_ids, run_voice_intake, voice_model
from .summary import case_summary

log = logging.getLogger("wapsi.amd.api")


class Metrics:
    """Process-local counters rendered as Prometheus text (no client library needed)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {
            "wapsi_cases_processed_total": 0,
            "wapsi_case_failures_total": 0,
            "wapsi_drafts_total": 0,
            "wapsi_drafts_verified_total": 0,
            "wapsi_llm_drafts_total": 0,
            "wapsi_prose_fallbacks_total": 0,
            "wapsi_injection_flags_total": 0,
            "wapsi_case_seconds_sum": 0.0,
            "wapsi_time_to_hold_plan_seconds_sum": 0.0,
            "wapsi_voice_intakes_total": 0,
            "wapsi_voice_failures_total": 0,
            "wapsi_voice_matched_total": 0,
            "wapsi_voice_injection_flags_total": 0,
            "wapsi_voice_audio_seconds_sum": 0.0,
            "wapsi_voice_asr_seconds_sum": 0.0,
            "wapsi_voice_extract_seconds_sum": 0.0,
            "wapsi_voice_seconds_sum": 0.0,
        }

    def record(self, summary: dict[str, Any], wall_s: float) -> None:
        with self._lock:
            c = self.counters
            c["wapsi_cases_processed_total"] += 1
            c["wapsi_drafts_total"] += summary["drafts"]
            c["wapsi_drafts_verified_total"] += summary["drafts_passed"]
            c["wapsi_llm_drafts_total"] += summary["llm_drafts"]
            c["wapsi_prose_fallbacks_total"] += summary["prose_fallbacks"]
            c["wapsi_injection_flags_total"] += summary["injection_flags"]
            c["wapsi_case_seconds_sum"] += wall_s
            c["wapsi_time_to_hold_plan_seconds_sum"] += summary["timings_s"].get("time_to_hold_plan_s", 0.0)

    def failure(self) -> None:
        with self._lock:
            self.counters["wapsi_case_failures_total"] += 1

    def record_voice(self, result: Any, wall_s: float) -> None:
        """One voice intake. ASR seconds / audio seconds over a scrape window is the mean real-time factor."""
        p = getattr(result, "pipeline", None)
        with self._lock:
            c = self.counters
            c["wapsi_voice_intakes_total"] += 1
            c["wapsi_voice_matched_total"] += 1 if result.matched_ncrp_ids else 0
            c["wapsi_voice_injection_flags_total"] += 1 if result.injection_flagged else 0
            c["wapsi_voice_seconds_sum"] += wall_s
            if p is not None:
                if p.audio_s and p.asr_s is not None:  # only when both are known, so the ratio stays a real RTF
                    c["wapsi_voice_audio_seconds_sum"] += p.audio_s
                    c["wapsi_voice_asr_seconds_sum"] += p.asr_s
                c["wapsi_voice_extract_seconds_sum"] += p.extract_s or 0.0

    def voice_failure(self) -> None:
        with self._lock:
            self.counters["wapsi_voice_failures_total"] += 1

    def render(self, services: Services | None) -> str:
        with self._lock:
            c = dict(self.counters)
        lines = []
        for name, value in c.items():
            kind = "counter" if name.endswith("_total") or name.endswith("_sum") else "gauge"
            lines += [f"# TYPE {name} {kind}", f"{name} {value:g}"]
        lines += ["# TYPE wapsi_case_seconds_count counter",
                  f"wapsi_case_seconds_count {c['wapsi_cases_processed_total']:g}"]
        if services is not None:
            desc = services.describe()
            llm = str(desc.get("llm_name", "")).replace('"', "'")
            lines += ["# TYPE wapsi_info gauge",
                      f'wapsi_info{{llm="{llm}",demo_mode="{int(bool(desc.get("demo_mode")))}"}} 1']
            stats = getattr(services.scorer, "stats", None)
            if stats is not None and hasattr(stats, "as_dict"):
                s = stats.as_dict()
                for k in ("calls", "gnn_ok", "failures", "fallback_rows"):
                    lines += [f"# TYPE wapsi_gnn_{k}_total counter", f"wapsi_gnn_{k}_total {s[k]:g}"]
            jstats = getattr(services.guard, "stats", None)
            if jstats is not None and hasattr(jstats, "as_dict"):
                s = jstats.as_dict()
                for k in ("judge_calls", "judge_errors", "judge_flagged", "rules_flagged"):
                    lines += [f"# TYPE wapsi_{k}_total counter", f"wapsi_{k}_total {s[k]:g}"]
        return "\n".join(lines) + "\n"


METRICS = Metrics()

app = FastAPI(title="Wapsi AMD edition", version="0.1.0",
              description="Money-trail copilot: pipeline API on vLLM-ROCm (synthetic data only).")


def services_dep() -> Services:
    return get_services()


Svc = Annotated[Services, Depends(services_dep)]


def require_token(svc: Svc, authorization: Annotated[str | None, Header()] = None) -> None:
    token = svc.settings.api_token
    if not token:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


@app.get("/healthz")
def healthz(svc: Svc) -> dict[str, Any]:
    return {"status": "ok", **svc.describe()}


@app.get("/cases")
def list_cases(svc: Svc) -> dict[str, Any]:
    cases = svc.store.list_cases()
    return {"count": len(cases), "cases": [c.model_dump(mode="json") for c in cases]}


@app.get("/cases/{case_id}")
def get_case(case_id: str, svc: Svc, full: Annotated[bool, Query()] = False) -> dict[str, Any]:
    try:
        bundle = svc.store.get_bundle(case_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    return bundle.model_dump(mode="json") if full else case_summary(bundle)


@app.post("/cases/{ids}/process", dependencies=[Depends(require_token)])
def process_ids(ids: str, svc: Svc, draft: Annotated[bool, Query()] = True) -> dict[str, Any]:
    try:
        ncrp_ids = resolve_ncrp_ids(svc, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    t = time.perf_counter()
    try:
        bundle = process(svc, ncrp_ids, draft=draft)
    except Exception as exc:  # noqa: BLE001 - report, count, keep serving
        METRICS.failure()
        log.exception("process_case failed for %s", ncrp_ids)
        raise HTTPException(status_code=502, detail=f"pipeline failed: {type(exc).__name__}: {exc}"[:500]) from exc
    wall = time.perf_counter() - t
    summary = case_summary(bundle)
    METRICS.record(summary, wall)
    return {**summary, "wall_s": round(wall, 4)}


# --------------------------------------------------------------------------- voice intake

_VOICE_HTTP = {"TOO_LARGE": 413, "NOT_CONFIGURED": 503, "NO_AUDIO_MODEL": 503, "SCHEMA": 502}


def _samples() -> list[dict[str, Any]]:
    from wapsi.intake.samples import load_samples

    return [{"id": s.sample_id, "title": s.title, "world": s.world, "duration_s": s.duration_s,
             "ncrp_ids": list(s.ncrp_ids)} for s in load_samples()]


@app.get("/intake/voice")
def voice_info(svc: Svc) -> dict[str, Any]:
    """Which models read voice notes here (no key, no audio). Building the model may probe the chat endpoint
    once for audio support (``WAPSI_ASR_CHAT_AUDIO``)."""
    from wapsi.intake.backends import describe_voice_model
    from wapsi.intake.voice import MAX_AUDIO_BYTES, SUPPORTED_FORMATS

    return {"model": describe_voice_model(voice_model(svc)), "formats": SUPPORTED_FORMATS,
            "max_bytes": MAX_AUDIO_BYTES, "samples": _samples()}


@app.post("/intake/voice", dependencies=[Depends(require_token)])
async def intake_voice(request: Request, svc: Svc, sample: Annotated[str | None, Query()] = None,
                       filename: Annotated[str, Query(max_length=200)] = "") -> dict[str, Any]:
    """Read one voice note. Everything in the answer that came from the recording is a claim by the complainant;
    ``reconciliation`` says what the ledger holds. Nothing is held or opened by this call."""
    from wapsi.adapters.llm_openai_compat import LLMError
    from wapsi.intake.voice import MAX_AUDIO_BYTES, VoiceIntakeError

    sample_id: str | None = None
    if sample:
        from wapsi.intake.samples import get_sample

        try:
            chosen = get_sample(sample)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no sample voice note {sample!r}") from exc
        data, filename, sample_id = chosen.read_bytes(), chosen.filename, chosen.sample_id
    else:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"the recording is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
        data = await request.body()
    t = time.perf_counter()
    try:
        result = await run_in_threadpool(run_voice_intake, svc, data, filename=filename, sample_id=sample_id)
    except VoiceIntakeError as exc:
        METRICS.voice_failure()
        raise HTTPException(status_code=_VOICE_HTTP.get(exc.code, 422),
                            detail={"code": exc.code, "message": str(exc)[:500]}) from exc
    except LLMError as exc:  # endpoint down, quota, missing key: the message never contains the key
        METRICS.voice_failure()
        raise HTTPException(status_code=502, detail={"code": getattr(exc, "code", "MODEL_ERROR"),
                                                     "message": str(exc)[:500]}) from exc
    except Exception as exc:  # noqa: BLE001 - report, count, keep serving
        METRICS.voice_failure()
        log.exception("voice intake failed")
        raise HTTPException(status_code=502,
                            detail={"code": "INTAKE_FAILED", "message": type(exc).__name__}) from exc
    wall = time.perf_counter() - t
    METRICS.record_voice(result, wall)
    return {**result.model_dump(mode="json"), "counts": result.counts(),
            "injection_flagged": result.injection_flagged, "claims_untrusted": True, "wall_s": round(wall, 4)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(svc: Svc) -> str:
    return METRICS.render(svc)
