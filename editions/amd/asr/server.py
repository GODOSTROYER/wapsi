"""Whisper on the AMD GPU behind an OpenAI-compatible transcription endpoint.

    # on the MI300X droplet (host) or in a GPU notebook: torch is the ROCm build there, no Docker needed
    uv sync --extra asr --extra amd-app
    WAPSI_ASR_MODEL=openai/whisper-large-v3-turbo uv run uvicorn editions.amd.asr.server:app --host 127.0.0.1 --port 8002
    # then, for the app:  WAPSI_ASR_BASE_URL=http://127.0.0.1:8002/v1  WAPSI_ASR_ON_AMD=1

Endpoints
    GET  /healthz                     liveness, model, device (no model load)
    GET  /v1/models                   the one served model
    POST /v1/audio/transcriptions     multipart form: ``file`` (WAV / MP3 / M4A / OGG / WebM, up to 15 MB), optional
                                      ``language`` (default ``WAPSI_ASR_LANGUAGE`` = hi), ``prompt``,
                                      ``response_format`` (``verbose_json`` default, ``json``, ``text``)

It serves ``wapsi.adapters.asr_transformers.TransformersASR`` (Whisper through transformers on torch: ROCm GPU
when torch sees one, CPU otherwise) and answers in OpenAI's shape, so ``wapsi.adapters.asr_openai_compat`` or any
OpenAI client can call it. ``verbose_json`` also carries ``x_wapsi``: the measured processing time, the
real-time factor and the device, so a caller can record where the speech model really ran. vLLM can serve Whisper
on the same route (``vllm serve openai/whisper-large-v3-turbo``) where its audio extras are installed; this small
server has no such requirement.

If ``WAPSI_ASR_API_KEY`` is set, requests need ``Authorization: Bearer <key>``. The model is loaded on the first
request (or at start-up with ``WAPSI_ASR_PRELOAD=1``). Audio is processed in memory and never written to a log.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.parser import BytesParser
from email.policy import HTTP
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from wapsi.intake.asr import (
    ASR_API_KEY_ENV,
    ASR_DEVICE_ENV,
    ASR_LANGUAGE_ENV,
    ASR_MODEL_ENV,
    ASR_PROMPT_ENV,
    DEFAULT_ASR_MODEL,
    DEFAULT_LANGUAGE,
    ASRError,
    SpeechToText,
)
from wapsi.intake.voice import MAX_AUDIO_BYTES, VoiceIntakeError, sniff_audio

log = logging.getLogger("wapsi.amd.asr")

_recogniser: SpeechToText | None = None
_lock = threading.Lock()


def build_recogniser() -> SpeechToText:
    from wapsi.adapters.asr_transformers import TransformersASR

    return TransformersASR(
        os.environ.get(ASR_MODEL_ENV, "").strip() or DEFAULT_ASR_MODEL,
        device=os.environ.get(ASR_DEVICE_ENV, "").strip() or "auto",
        language=os.environ.get(ASR_LANGUAGE_ENV, "").strip() or DEFAULT_LANGUAGE,
        prompt=os.environ.get(ASR_PROMPT_ENV, "").strip() or None,
    )


def get_recogniser() -> SpeechToText:
    global _recogniser
    if _recogniser is None:
        with _lock:
            if _recogniser is None:
                _recogniser = build_recogniser()
    return _recogniser


def set_recogniser(recogniser: SpeechToText | None) -> None:
    """Install (or clear) the recogniser; used by tests."""
    global _recogniser
    with _lock:
        _recogniser = recogniser


def require_key(authorization: Annotated[str | None, Header()] = None) -> None:
    key = os.environ.get(ASR_API_KEY_ENV, "").strip()
    if not key:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode("utf-8"), key.encode("utf-8")):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def parse_form(content_type: str, body: bytes) -> tuple[dict[str, str], bytes | None, str]:
    """(text fields, the uploaded file's bytes, its file name) from a multipart/form-data body. Standard library
    only, so the server needs no multipart package."""
    if "multipart/form-data" not in (content_type or "").lower():
        raise HTTPException(status_code=415, detail="send multipart/form-data with a 'file' field")
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    audio: bytes | None = None
    filename = ""
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True) or b""
        if name == "file":
            audio, filename = payload, str(part.get_filename() or "")
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, audio, filename


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("WAPSI_ASR_PRELOAD", "").strip().lower() in ("1", "true", "yes", "on"):
        await run_in_threadpool(lambda: getattr(get_recogniser(), "pipe", None))  # load the weights now
    yield


app = FastAPI(title="Wapsi ASR (Whisper on AMD)", version="0.1.0", lifespan=_lifespan,
              description="OpenAI-compatible transcription endpoint for voice intake (synthetic data only).")


def _describe() -> dict[str, Any]:
    stt = get_recogniser()
    out: dict[str, Any] = {"model": getattr(stt, "model", getattr(stt, "name", "")),
                           "loaded": bool(getattr(stt, "loaded", True)),
                           "language": getattr(stt, "language", None)}
    hw = getattr(stt, "_hardware", None)  # known once the model has loaded; /healthz never forces a load
    if isinstance(hw, dict):
        out.update({k: hw.get(k) for k in ("device", "accelerator", "gpu_name", "on_amd") if k in hw})
    return out


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", **_describe()}


@app.get("/v1/models", dependencies=[Depends(require_key)])
def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": _describe()["model"], "object": "model", "owned_by": "wapsi"}]}


@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_key)], response_model=None)
async def transcriptions(request: Request) -> Any:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES + 64 * 1024:
        raise HTTPException(status_code=413, detail=f"audio larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    fields, audio, filename = parse_form(request.headers.get("content-type", ""), await request.body())
    if not audio:
        raise HTTPException(status_code=400, detail="the 'file' field is missing or empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail=f"audio larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    try:
        mime = sniff_audio(audio, filename)  # by magic bytes, never by the file name
    except VoiceIntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fmt = (fields.get("response_format") or "verbose_json").lower()
    if fmt not in ("verbose_json", "json", "text"):
        raise HTTPException(status_code=400, detail="response_format must be verbose_json, json or text")
    stt = get_recogniser()
    try:
        out = await run_in_threadpool(stt.transcribe, audio, mime, language=fields.get("language") or None,
                                      prompt=fields.get("prompt") or None)
    except ASRError as exc:
        status = 400 if exc.code == "ASR_DECODE" else 503 if exc.code == "ASR_UNAVAILABLE" else 500
        raise HTTPException(status_code=status, detail=str(exc)[:400]) from exc
    log.info("transcribed %.1f s of audio in %.2f s on %s", out.duration_s or 0.0, out.elapsed_s, out.device)
    if fmt == "text":
        return PlainTextResponse(out.text)
    if fmt == "json":
        return {"text": out.text}
    return {
        "task": "transcribe",
        "language": out.language,
        "duration": out.duration_s,
        "text": out.text,
        "segments": [{"id": i, "start": s.start, "end": s.end, "text": s.text} for i, s in enumerate(out.segments)],
        "x_wapsi": {"model": out.model, "elapsed_s": out.elapsed_s, "rtf": out.rtf, "device": out.device,
                    "on_amd": out.on_amd},
    }
