"""Local Whisper through ``transformers`` on torch (implements ``wapsi.intake.asr.SpeechToText``).

    uv sync --extra asr                       # torch + transformers (the ``amd`` extra has them too)
    WAPSI_ASR_BACKEND=local WAPSI_ASR_MODEL=openai/whisper-large-v3-turbo

Device: ``auto`` picks the GPU when torch sees one (a ROCm build of torch reports an AMD GPU as ``cuda``) and the
CPU otherwise; half precision on a GPU, float32 on the CPU. A 30 second voice note takes a few seconds on a GPU
and stays usable on a laptop CPU. The model is loaded on first use (and downloaded from Hugging Face if it is
not cached), so nothing heavy happens at import or construction time.

The audio is decoded and resampled to 16 kHz here (``wapsi.intake.asr.decode_audio``), so torchaudio is not
needed; WAV works out of the box, other containers need ffmpeg or soundfile. The task is always *transcribe*.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from typing import Any

from wapsi.intake.asr import (
    DEFAULT_ASR_MODEL,
    DEFAULT_LANGUAGE,
    TARGET_RATE,
    ASRError,
    Transcription,
    clean_segments,
    decode_audio,
    language_code,
    real_time_factor,
    requested_language,
)

DEVICES = ("auto", "cpu", "cuda")


def torch_hardware(device: str = "auto") -> dict[str, Any]:
    """Where torch would run: ``{"device", "accelerator", "gpu_name", "torch", "on_amd"}``. ``accelerator`` is
    ``rocm`` for an AMD GPU (a ROCm build of torch), ``cuda`` for another GPU, ``cpu`` otherwise."""
    import torch

    want = (device or "auto").strip().lower()
    use_gpu = want != "cpu" and bool(torch.cuda.is_available())
    if want.startswith("cuda") and not use_gpu:
        raise ASRError("WAPSI_ASR_DEVICE=cuda but torch sees no GPU (is this a ROCm / CUDA build?)", "ASR_UNAVAILABLE")
    info: dict[str, Any] = {"device": "cpu", "accelerator": "cpu", "gpu_name": None, "torch": torch.__version__,
                            "on_amd": False}  # fmt: skip
    if use_gpu:
        hip = getattr(getattr(torch, "version", None), "hip", None)
        info["device"] = want if want.startswith("cuda:") else "cuda:0"
        info["accelerator"] = "rocm" if hip else "cuda"
        try:
            info["gpu_name"] = str(torch.cuda.get_device_name(0))
        except Exception:  # noqa: BLE001 - the name is a nicety
            info["gpu_name"] = None
        info["on_amd"] = bool(hip)
        if hip:
            info["rocm"] = str(hip)
    return info


def device_label(hw: dict[str, Any]) -> str:
    if hw.get("accelerator") == "cpu":
        return "cpu"
    kind = "ROCm" if hw.get("accelerator") == "rocm" else "CUDA"
    name = f": {hw['gpu_name']}" if hw.get("gpu_name") else ""
    return f"{hw.get('device')} ({kind}{name})"


class TransformersASR:
    """Whisper (or any ``automatic-speech-recognition`` model) running in this process."""

    backend = "local"

    def __init__(
        self,
        model: str = DEFAULT_ASR_MODEL,
        *,
        device: str = "auto",
        language: str | None = DEFAULT_LANGUAGE,
        prompt: str | None = None,
        name: str | None = None,
        pipeline_factory: Callable[..., Any] | None = None,
        hardware: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        if (device or "auto").split(":")[0].lower() not in DEVICES:
            raise ValueError(f"device must be one of {', '.join(DEVICES)}")
        self.model = model
        self.name = name or model
        self.device = device or "auto"
        self.language = language
        self.prompt = prompt
        self._factory = pipeline_factory
        self._hardware = hardware
        self._clock = clock
        self._pipe: Any = None
        self._lock = threading.Lock()
        self.load_s: float | None = None  # wall time of the one-off model load

    def __repr__(self) -> str:
        return f"TransformersASR(model={self.model!r}, device={self.device!r})"

    @property
    def hardware(self) -> dict[str, Any]:
        if self._hardware is None:
            self._hardware = torch_hardware(self.device)
        return self._hardware

    @property
    def loaded(self) -> bool:
        return self._pipe is not None

    def _build(self) -> Any:
        hw = self.hardware
        if self._factory is not None:
            return self._factory(model=self.model, device=hw["device"])
        try:
            import torch
            from transformers import pipeline
        except ImportError as exc:
            raise ASRError(
                "the local speech recogniser needs torch and transformers: uv sync --extra asr "
                "(or set WAPSI_ASR_BASE_URL to use a transcription endpoint)",
                "ASR_UNAVAILABLE",
            ) from exc
        dtype = torch.float16 if hw["accelerator"] != "cpu" else torch.float32
        params = inspect.signature(pipeline).parameters
        kwargs: dict[str, Any] = {"model": self.model, "device": hw["device"]}
        kwargs["dtype" if "dtype" in params else "torch_dtype"] = dtype  # renamed in transformers 4.56+
        try:
            return pipeline("automatic-speech-recognition", **kwargs)
        except Exception as exc:  # noqa: BLE001 - missing weights, no network, out of memory ...
            raise ASRError(
                f"could not load the speech model {self.model} on {hw['device']}: {type(exc).__name__}: {exc}"[:500],
                "ASR_UNAVAILABLE",
            ) from exc

    @property
    def pipe(self) -> Any:
        """The transformers pipeline, built once (thread-safe)."""
        if self._pipe is None:
            with self._lock:
                if self._pipe is None:
                    t0 = self._clock()
                    self._pipe = self._build()
                    self.load_s = round(self._clock() - t0, 3)
        return self._pipe

    def _generate_kwargs(self, pipe: Any, language: str | None, prompt: str | None) -> dict[str, Any]:
        cfg = getattr(getattr(pipe, "model", None), "generation_config", None)
        if not getattr(cfg, "is_multilingual", True):  # English-only checkpoints (``*.en``) reject both arguments
            return {}
        out: dict[str, Any] = {"task": "transcribe"}
        if language:
            out["language"] = language
        if prompt:
            ids = getattr(getattr(pipe, "tokenizer", None), "get_prompt_ids", None)
            if callable(ids):
                prompt_ids = ids(prompt, return_tensors="pt")
                device = getattr(pipe, "device", None)
                if device is not None and hasattr(prompt_ids, "to"):
                    prompt_ids = prompt_ids.to(device)  # the prompt has to sit on the model's device
                out["prompt_ids"] = prompt_ids
        return out

    def transcribe(
        self, audio: bytes, mime_type: str, *, language: str | None = None, prompt: str | None = None
    ) -> Transcription:
        lang = requested_language(language if language is not None else self.language)
        hint = prompt if prompt is not None else self.prompt
        samples, duration = decode_audio(audio, mime_type, TARGET_RATE)
        pipe = self.pipe  # the one-off load is not part of the real-time factor
        call: dict[str, Any] = {"return_timestamps": True}  # also what Whisper needs for clips over 30 s
        gen = self._generate_kwargs(pipe, lang, hint)
        if gen:
            call["generate_kwargs"] = gen
        if lang is None:
            call["return_language"] = True
        t0 = self._clock()
        try:
            with self._lock:  # one generation at a time per model: a GPU does not get faster by interleaving
                out = pipe({"raw": samples, "sampling_rate": TARGET_RATE}, **call)
        except ASRError:
            raise
        except Exception as exc:  # noqa: BLE001 - report the recogniser's failure to the officer
            raise ASRError(f"speech recognition failed: {type(exc).__name__}: {exc}"[:400]) from exc
        elapsed = self._clock() - t0
        if not isinstance(out, dict):
            raise ASRError("the speech pipeline returned an unexpected result")
        chunks = out.get("chunks") or []
        detected = next((c.get("language") for c in chunks if isinstance(c, dict) and c.get("language")), None)
        hw = self.hardware
        return Transcription(
            text=str(out.get("text") or "").strip(),
            language=language_code(str(detected or "")) or (lang or ""),
            segments=clean_segments(chunks, duration),
            duration_s=duration,
            elapsed_s=round(elapsed, 4),
            rtf=real_time_factor(elapsed, duration),
            model=self.model,
            backend=self.backend,
            device=device_label(hw),
            on_amd=bool(hw.get("on_amd")),
        )
