"""Speech-to-text port for voice intake: audio bytes -> transcript, language, timed segments, real-time factor.

The open voice path (``wapsi.intake.open_voice``) splits what Gemini does in one multimodal call into two steps
that both run on open models: a speech recogniser (this port) and a text LLM that structures the transcript
(``wapsi.intake.extract``). Adapters:

- ``wapsi.adapters.asr_openai_compat.OpenAICompatASR``: any server with ``POST /v1/audio/transcriptions`` (Whisper
  served by vLLM on an AMD GPU, the bundled ``editions/amd/asr/server.py``, faster-whisper-server, ...);
- ``wapsi.adapters.asr_openai_compat.ChatAudioASR``: an audio-capable chat model behind ``/chat/completions``
  (probed at runtime, used only when the endpoint accepts audio parts);
- ``wapsi.adapters.asr_transformers.TransformersASR``: Whisper through ``transformers`` on torch (ROCm GPU on AMD,
  CPU fallback) behind the ``asr`` extra;
- ``FakeASR`` (here): deterministic, offline; answers for the bundled synthetic samples by SHA-256.

Configuration (all optional):

| Variable | Default | Meaning |
|---|---|---|
| ``WAPSI_ASR_BACKEND`` | ``auto`` | ``auto`` \\| ``endpoint`` \\| ``local`` \\| ``chat-audio`` \\| ``fake`` |
| ``WAPSI_ASR_BASE_URL`` | unset | OpenAI-compatible base URL ending in ``/v1`` (enables ``endpoint``) |
| ``WAPSI_ASR_API_KEY`` | unset | bearer token; falls back to ``WAPSI_LLM_API_KEY`` only for the same host |
| ``WAPSI_ASR_MODEL`` | ``openai/whisper-large-v3-turbo`` | served model name, or the Hugging Face id / path for ``local`` |
| ``WAPSI_ASR_LANGUAGE`` | ``hi`` | Whisper language hint; ``auto`` lets the model detect it |
| ``WAPSI_ASR_PROMPT`` | unset | optional initial prompt (spelling of domain words: UTR, IMPS, NCRP ...) |
| ``WAPSI_ASR_DEVICE`` | ``auto`` | ``local`` only: ``auto`` \\| ``cpu`` \\| ``cuda`` (ROCm reports as cuda) |
| ``WAPSI_ASR_TIMEOUT_S`` | ``120`` | endpoint request timeout |
| ``WAPSI_ASR_CHAT_AUDIO`` | ``probe`` | ``probe`` \\| ``on`` \\| ``off``: chat-audio fallback when nothing else is available |
| ``WAPSI_ASR_ON_AMD`` | unset | ``1`` = the operator states the ASR endpoint runs on AMD hardware (labels only) |

Hindi / Hinglish: keep ``WAPSI_ASR_LANGUAGE=hi``. Whisper then writes Hindi words in Devanagari and keeps most
English words; with ``en`` it tends to translate or drop the Hindi, and auto-detection is unreliable on short
code-mixed clips. The task is always *transcribe* (never Whisper's own translate): the English translation is
written by the text LLM, which also joins digits read one by one and converts ``chaar lakh assi hazaar``.

Real-time factor (``rtf``) = processing seconds / audio seconds; below 1 is faster than real time.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from wapsi.adapters.llm_openai_compat import LLMError

DEFAULT_ASR_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_LANGUAGE = "hi"
TARGET_RATE = 16_000

ASR_BACKEND_ENV = "WAPSI_ASR_BACKEND"
ASR_BASE_URL_ENV = "WAPSI_ASR_BASE_URL"
ASR_API_KEY_ENV = "WAPSI_ASR_API_KEY"
ASR_MODEL_ENV = "WAPSI_ASR_MODEL"
ASR_LANGUAGE_ENV = "WAPSI_ASR_LANGUAGE"
ASR_PROMPT_ENV = "WAPSI_ASR_PROMPT"
ASR_DEVICE_ENV = "WAPSI_ASR_DEVICE"
ASR_TIMEOUT_ENV = "WAPSI_ASR_TIMEOUT_S"
ASR_CHAT_AUDIO_ENV = "WAPSI_ASR_CHAT_AUDIO"
ASR_ON_AMD_ENV = "WAPSI_ASR_ON_AMD"
ASR_BACKENDS = ("auto", "endpoint", "local", "chat-audio", "fake")

# a spoken-domain prompt operators can copy into WAPSI_ASR_PROMPT (not applied by default: a Roman-script prompt
# nudges Whisper towards Roman Hinglish, which is a choice, not a fix)
HINGLISH_PROMPT = "UPI, IMPS, NEFT, RTGS, UTR, NCRP, 1930, OTP, KYC, bank account, lakh, hazaar, rupaye."

_LANGUAGE_NAMES = {
    "hindi": "hi", "english": "en", "marathi": "mr", "gujarati": "gu", "bengali": "bn", "tamil": "ta",
    "telugu": "te", "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "panjabi": "pa", "urdu": "ur",
}  # fmt: skip
_EXT = {"audio/wav": "wav", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/ogg": "ogg", "audio/webm": "webm"}


class ASRError(LLMError):
    """Speech-to-text failed. ``code`` is stable for UIs and tools (``ASR_DECODE``, ``ASR_UNAVAILABLE``,
    ``ASR_FAILED``); the message never contains a key."""

    def __init__(
        self, message: str, code: str = "ASR_FAILED", *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.code = code


class AsrSegment(BaseModel):
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str = ""


class Transcription(BaseModel):
    """What a speech recogniser returns. JSON-safe."""

    text: str = ""
    language: str = ""  # ISO 639-1 as detected or requested; "" = unknown
    segments: list[AsrSegment] = Field(default_factory=list)
    duration_s: float | None = None  # audio length
    elapsed_s: float = 0.0  # wall time of the transcription
    rtf: float | None = None  # elapsed / duration; < 1 is faster than real time
    model: str = ""
    backend: str = ""  # endpoint | local | chat-audio | fake
    device: str = ""  # "cpu", "cuda:0 (ROCm ...)", "remote"
    on_amd: bool = False  # the recogniser ran on AMD hardware (ROCm GPU, or an endpoint the operator marked)


class VoicePipelineInfo(BaseModel):
    """Which models read a voice note and how long each step took (``VoiceIntakeResult.pipeline``)."""

    backend: str = ""  # open | gemini | fake
    label: str = ""  # what the UI shows: "Whisper large-v3-turbo + Gemma 4 31B on AMD"
    transcribed_by: str = ""
    asr_backend: str = ""
    asr_device: str = ""
    asr_on_amd: bool = False
    asr_language: str = ""
    audio_s: float | None = None
    asr_s: float | None = None
    rtf: float | None = None
    structured_by: str = ""  # the text model whose answer was accepted
    served_by: str = ""  # "AMD Token Factory", "vLLM-ROCm", a host name
    llm_on_amd: bool = False
    models_tried: list[str] = Field(default_factory=list)  # what each model in the chain did with this note
    failover: bool = False  # the first model was skipped or rejected and another one answered
    withheld: int = 0  # instruction-like passages removed from the transcript before the text model read it
    extract_s: float | None = None
    json_mode: str = ""
    attempts: int = 0
    repaired: bool = False  # the model's JSON needed repair or normalisation before it validated
    fixes: list[str] = Field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None  # as reported by the endpoint (``usage.cost``), when it reports one


@runtime_checkable
class SpeechToText(Protocol):
    """A speech recogniser. ``language`` / ``prompt`` override the adapter's defaults for one call."""

    name: str

    def transcribe(
        self, audio: bytes, mime_type: str, *, language: str | None = None, prompt: str | None = None
    ) -> Transcription: ...


# --------------------------------------------------------------------------- small helpers


def real_time_factor(elapsed_s: float, duration_s: float | None) -> float | None:
    if not duration_s or duration_s <= 0:
        return None
    return round(elapsed_s / duration_s, 4)


def language_code(raw: str | None) -> str:
    """'hindi' / 'Hindi' / 'hi' / '<|hi|>' -> 'hi'; unknown names pass through lower-cased; None -> ''."""
    value = re.sub(r"[<|>]", "", (raw or "")).strip().lower()
    return _LANGUAGE_NAMES.get(value, value)


def requested_language(value: str | None) -> str | None:
    """The language hint to send: None for '', 'auto' or 'detect'."""
    code = language_code(value)
    return None if code in ("", "auto", "detect", "none") else code


def audio_filename(mime_type: str) -> str:
    """A neutral upload name with the right extension (the caller's file name may hold a name or a number)."""
    return f"voice-note.{_EXT.get(mime_type, 'wav')}"


def clean_segments(items: Any, duration_s: float | None = None) -> list[AsrSegment]:
    """Segments from loosely shaped dicts (``start`` / ``end`` / ``text`` or a ``timestamp`` pair): clamped,
    ordered, empty text dropped. A missing end takes the next start (or the audio length)."""
    raw: list[tuple[float | None, float | None, str]] = []
    for it in items or []:
        if not isinstance(it, Mapping):
            continue
        text = str(it.get("text") or "").strip()
        stamp = it.get("timestamp")
        start, end = it.get("start"), it.get("end")
        if isinstance(stamp, list | tuple) and len(stamp) == 2:
            start, end = stamp
        if not text:
            continue
        raw.append((_num(start), _num(end), text))
    out: list[AsrSegment] = []
    for i, (start, end, text) in enumerate(raw):
        s = max(0.0, start if start is not None else (out[-1].end if out else 0.0))
        nxt = raw[i + 1][0] if i + 1 < len(raw) else None
        e = end if end is not None else (nxt if nxt is not None else (duration_s or s))
        out.append(AsrSegment(start=round(s, 2), end=round(max(s, e), 2), text=text))
    return out


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


# --------------------------------------------------------------------------- decoding (local recogniser)


def wav_info(data: bytes) -> tuple[int, int, int, int] | None:
    """(rate, channels, sample width, frames) of a PCM WAV, else None."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
    except (wave.Error, EOFError, ValueError):
        return None


def audio_duration_s(data: bytes, mime_type: str = "audio/wav") -> float | None:
    info = wav_info(data) if mime_type == "audio/wav" else None
    if info is None or not info[0]:
        return None
    return round(info[3] / info[0], 2)


def decode_wav(data: bytes) -> tuple[Any, int]:
    """PCM WAV (8 / 16 / 24 / 32 bit) -> (mono float32 samples in [-1, 1], sample rate)."""
    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        rate, channels, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(frames)
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ASRError(f"unsupported WAV sample width ({width} bytes)", "ASR_DECODE")
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    return x.astype(np.float32), rate


def resample(samples: Any, rate: int, target: int = TARGET_RATE) -> Any:
    """Resample mono float32 audio to ``target`` Hz (polyphase filter with scipy, linear interpolation without)."""
    import numpy as np

    if rate == target or len(samples) == 0:
        return np.asarray(samples, dtype=np.float32)
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(rate), int(target))
        return resample_poly(samples, target // g, rate // g).astype(np.float32)
    except ImportError:
        n = int(round(len(samples) * target / rate))
        pos = np.linspace(0.0, len(samples) - 1, num=max(n, 1))
        return np.interp(pos, np.arange(len(samples)), samples).astype(np.float32)


def _decode_ffmpeg(data: bytes, mime_type: str, target: int) -> tuple[Any, int] | None:
    exe = shutil.which("ffmpeg")
    if exe is None:
        return None
    import numpy as np

    # a real file, not a pipe: MP4 / M4A keep their index at the end and ffmpeg has to seek for it
    with tempfile.TemporaryDirectory(prefix="wapsi-asr-") as tmp:
        src = os.path.join(tmp, audio_filename(mime_type))
        with open(src, "wb") as fh:
            fh.write(data)
        cmd = [exe, "-nostdin", "-v", "error", "-i", src, "-ac", "1", "-ar", str(target), "-f", "f32le", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
    if out.returncode != 0 or not out.stdout:
        return None
    return np.frombuffer(out.stdout, dtype="<f4").astype(np.float32), target


def _decode_soundfile(data: bytes) -> tuple[Any, int] | None:
    if importlib.util.find_spec("soundfile") is None:
        return None
    try:
        import soundfile as sf

        x, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - libsndfile rejects the container; try the next decoder
        return None
    return x.mean(axis=1).astype("float32"), int(rate)


def decode_audio(data: bytes, mime_type: str, target: int = TARGET_RATE) -> tuple[Any, float]:
    """Audio bytes -> (mono float32 samples at ``target`` Hz, duration in seconds).

    WAV is decoded with the standard library; OGG / Opus, MP3, M4A and WebM need ``soundfile`` (libsndfile) or an
    ``ffmpeg`` binary on PATH. Raises ``ASRError(code="ASR_DECODE")`` with what to install."""
    decoded: tuple[Any, int] | None = None
    if mime_type == "audio/wav":
        try:
            decoded = decode_wav(data)
        except (wave.Error, EOFError, ValueError):
            decoded = None  # e.g. IEEE-float or extensible WAV: let the other decoders try
    if decoded is None:
        decoded = _decode_soundfile(data) or _decode_ffmpeg(data, mime_type, target)
    if decoded is None:
        kind = _EXT.get(mime_type, "this").upper()
        raise ASRError(
            f"the local speech recogniser cannot decode {kind} audio here: install ffmpeg (or the soundfile "
            "package), send a WAV file, or point WAPSI_ASR_BASE_URL at a transcription endpoint",
            "ASR_DECODE",
        )
    samples, rate = decoded
    if rate <= 0 or len(samples) == 0:
        raise ASRError("the recording holds no audio samples", "ASR_DECODE")
    duration = len(samples) / float(rate)
    return resample(samples, rate, target), round(duration, 2)


# --------------------------------------------------------------------------- offline fake


def _script_segments(text: str, duration_s: float | None) -> list[AsrSegment]:
    """Sentence segments with timestamps proportional to their length (a stand-in for real alignment)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?।])\s+", text.strip()) if p.strip()]
    total = sum(len(p) for p in parts) or 1
    length = duration_s or round(total / 15.0, 2)  # ~15 characters a second when the length is unknown
    out, at = [], 0.0
    for p in parts:
        end = at + length * len(p) / total
        out.append(AsrSegment(start=round(at, 2), end=round(end, 2), text=p))
        at = end
    return out


class FakeASR:
    """Deterministic offline recogniser: the bundled samples (by SHA-256) return the script they were read from;
    anything else returns an empty transcript (it cannot hear). ``canned`` maps a SHA-256 to a transcript.

    No speech model runs, so by default it reports no processing time and no real-time factor. ``rtf`` makes it
    report one (tests of the timing plumbing)."""

    backend = "fake"

    def __init__(
        self,
        canned: Mapping[str, str] | None = None,
        *,
        name: str = "fake-asr",
        language: str = "hi",
        rtf: float | None = None,
    ) -> None:
        self.name = name
        self.model = name
        self.canned = dict(canned) if canned is not None else None
        self.language = language
        self.fixed_rtf = rtf
        self.calls: list[dict[str, Any]] = []

    def _lookup(self, digest: str) -> tuple[str, float | None]:
        if self.canned is not None:
            return self.canned.get(digest, ""), None
        from wapsi.intake.samples import sample_for_hash  # lazy: no data read unless a sample is transcribed

        sample = sample_for_hash(digest)
        return (sample.script, sample.duration_s) if sample is not None else ("", None)

    def transcribe(
        self, audio: bytes, mime_type: str, *, language: str | None = None, prompt: str | None = None
    ) -> Transcription:
        digest = hashlib.sha256(audio).hexdigest()
        self.calls.append({"sha256": digest, "mime_type": mime_type, "language": language, "prompt": prompt})
        text, known = self._lookup(digest)
        duration = audio_duration_s(audio, mime_type) or known
        elapsed = round((duration or 0.0) * self.fixed_rtf, 4) if self.fixed_rtf is not None else 0.0
        return Transcription(
            text=text,
            language=(requested_language(language) or self.language) if text else "",
            segments=_script_segments(text, duration) if text else [],
            duration_s=duration,
            elapsed_s=elapsed,
            rtf=real_time_factor(elapsed, duration) if self.fixed_rtf is not None else None,
            model=self.name,
            backend="fake",
            device="offline",
        )
