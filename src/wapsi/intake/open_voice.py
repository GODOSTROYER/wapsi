"""The open voice path: a speech recogniser plus an open text LLM, behind the same ``AudioLLM`` interface the
Gemini multimodal path implements.

    audio --(SpeechToText: Whisper)--> transcript, language, timed segments, real-time factor
          --(TranscriptExtractor: Gemma 4 on AMD)--> translation, fields with confidence, amounts, UTRs,
                                                     instruction-like text  (validated against VOICE_SCHEMA)

``OpenVoiceLLM.extract_media`` returns JSON text matching ``VOICE_SCHEMA`` exactly like ``GeminiLLM.extract_media``,
so ``wapsi.intake.voice.intake_voice_note`` masks the phone, runs the injection guard and reconciles every claim
against the ledger with the same code for every backend. ``extract_voice`` also returns a ``VoiceRun`` (which
models ran, where, how long, tokens) that ``intake_voice_note`` stores on the result.

Instruction-like text never reaches the text model when the guard recognises it: the transcript is screened
first and the model reads the sanitized copy (each passage replaced by a marker). The officer still sees the
full transcript with those passages quoted, because ``intake_voice_note`` screens the stored transcript itself.
What the guard does not recognise still meets the prompt's UNTRUSTED markers and the schema.

``FakeTranscriptLLM`` is the offline stand-in for the text LLM (bundled samples only); together with
``wapsi.intake.asr.FakeASR`` it exercises the whole open pipeline with no network and no model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from wapsi.intake.asr import AsrSegment, SpeechToText, Transcription, VoicePipelineInfo
from wapsi.intake.extract import (
    TRANSCRIPT_CLOSE,
    TRANSCRIPT_OPEN,
    WITHHELD_MARK,
    ExtractionStats,
    TranscriptExtractor,
)
from wapsi.ports import Guard


@dataclass
class VoiceRun:
    """One pass of the open pipeline: what ``intake_voice_note`` stores next to the extraction."""

    info: VoicePipelineInfo
    segments: list[AsrSegment] = field(default_factory=list)
    transcription: Transcription | None = None
    extraction: ExtractionStats | None = None
    name: str = ""  # "<speech model> + <text model that answered>": the result's ``extracted_by``


def friendly_model(name: str) -> str:
    """'openai/whisper-large-v3-turbo' -> 'Whisper large-v3-turbo'; 'Gemma-4-31B-IT' and 'google/gemma-4-31B-it'
    -> 'Gemma 4 31B'; anything else keeps its own name (without the organisation prefix)."""
    base = (name or "").strip().rstrip("/").rsplit("/", 1)[-1]
    low = base.lower()
    if low.startswith("whisper-"):
        return "Whisper " + base[len("whisper-") :]
    if low.startswith("faster-whisper-"):
        return "Whisper " + base[len("faster-whisper-") :]
    m = re.fullmatch(r"gemma-(\d+n?)-(.+?)(?:-it)?", base, re.IGNORECASE)
    if m:
        return f"Gemma {m.group(1)} {m.group(2)}"
    return base or name


def pipeline_label(
    asr_model: str,
    llm_model: str,
    *,
    asr_backend: str = "",
    asr_on_amd: bool = False,
    llm_on_amd: bool = False,
    asr_device: str = "",
) -> str:
    """What the UI shows, with the hardware claim kept honest: 'on AMD' covers a model only when it is known to
    run on AMD hardware (the Token Factory, a ROCm GPU, or an endpoint the operator marked)."""
    asr, llm = friendly_model(asr_model), friendly_model(llm_model)
    if asr_backend == "chat-audio":
        asr += " (audio)"
    if asr_on_amd and llm_on_amd:
        return f"{asr} + {llm} on AMD"
    if llm_on_amd:
        where = {"local": "local", "fake": "stand-in", "endpoint": "endpoint", "chat-audio": "endpoint"}.get(asr_backend)
        if asr_backend == "local" and asr_device.startswith("cpu"):
            where = "local CPU"
        return f"{asr} ({where}) + {llm} on AMD" if where else f"{asr} + {llm} on AMD"
    if asr_on_amd:
        return f"{asr} on AMD + {llm}"
    return f"{asr} + {llm}"


class OpenVoiceLLM:
    """``AudioLLM`` built from a ``SpeechToText`` and a text LLM (through ``TranscriptExtractor``)."""

    backend = "open"

    def __init__(
        self,
        asr: SpeechToText,
        extractor: TranscriptExtractor,
        *,
        served_by: str = "",
        llm_on_amd: bool = False,
        asr_on_amd: bool | None = None,
        guard: Guard | None = None,
        name: str | None = None,
    ) -> None:
        self.asr = asr
        self.extractor = extractor
        self.guard = guard  # screens the transcript before the text model reads it (None = the model reads it all)
        self.served_by = served_by
        self.llm_on_amd = llm_on_amd
        self._asr_on_amd = asr_on_amd
        self.asr_model = str(getattr(asr, "model", None) or getattr(asr, "name", type(asr).__name__))
        self.llm_model = str(getattr(extractor.llm, "model", None) or extractor.name)  # first in the chain
        self.name = name or f"{self.asr_model.rsplit('/', 1)[-1]} + {self.llm_model.rsplit('/', 1)[-1]}"

    def __repr__(self) -> str:
        return f"OpenVoiceLLM(asr={self.asr!r}, llm={self.extractor.llm!r})"

    @property
    def asr_backend(self) -> str:
        return str(getattr(self.asr, "backend", "") or "")

    def _asr_on_amd_now(self, heard: Transcription | None = None) -> bool:
        if heard is not None and heard.on_amd:
            return True
        if self._asr_on_amd is not None:
            return self._asr_on_amd
        return bool(getattr(self.asr, "on_amd", False))

    def label(self, heard: Transcription | None = None, llm_model: str | None = None) -> str:
        """The UI label. After a run, ``llm_model`` is the model that actually answered (a failover shows)."""
        return pipeline_label(
            self.asr_model,
            llm_model or self.llm_model,
            asr_backend=self.asr_backend,
            asr_on_amd=self._asr_on_amd_now(heard),
            llm_on_amd=self.llm_on_amd,
            asr_device=heard.device if heard is not None else "",
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret description for labels, ``/healthz`` and benchmark records."""
        return {
            "backend": self.backend,
            "label": self.label(),
            "transcribed_by": self.asr_model,
            "asr_backend": self.asr_backend,
            "asr_on_amd": self._asr_on_amd_now(),
            "structured_by": self.llm_model,
            "model_chain": self.extractor.chain.models,
            "served_by": self.served_by,
            "llm_on_amd": self.llm_on_amd,
            "json_mode": self.extractor.json_mode,
        }

    def extract_voice(
        self,
        data: bytes,
        mime_type: str,
        *,
        system: str = "",
        prompt: str = "",
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> tuple[str, VoiceRun]:
        """Audio -> (JSON text matching ``VOICE_SCHEMA``, what ran). ``prompt`` carries the received-at date line;
        ``system`` / ``json_schema`` are the multimodal ones and are replaced by the transcript prompt."""
        heard = self.asr.transcribe(data, mime_type)
        shown, withheld = heard.text, 0
        if self.guard is not None and heard.text.strip():
            screened = self.guard.screen(heard.text)
            if screened.flagged and screened.sanitized_text.strip():
                shown, withheld = screened.sanitized_text, max(1, len(screened.quoted_spans))
        ext, stats = self.extractor.extract(heard.text, context=prompt, language=heard.language, shown=shown)
        answered = stats.model or self.llm_model
        info = VoicePipelineInfo(
            backend=self.backend,
            label=self.label(heard, answered),
            transcribed_by=heard.model or self.asr_model,
            asr_backend=heard.backend or self.asr_backend,
            asr_device=heard.device,
            asr_on_amd=self._asr_on_amd_now(heard),
            asr_language=heard.language,
            audio_s=heard.duration_s,
            asr_s=heard.elapsed_s,
            rtf=heard.rtf,
            structured_by=answered,
            served_by=self.served_by,
            llm_on_amd=self.llm_on_amd,
            models_tried=list(stats.tried),
            failover=stats.failover,
            withheld=withheld,
            extract_s=stats.elapsed_s,
            json_mode=stats.json_mode,
            attempts=stats.attempts,
            repaired=stats.repaired,
            fixes=list(stats.fixes),
            prompt_tokens=stats.prompt_tokens,
            completion_tokens=stats.completion_tokens,
            cost_usd=stats.cost_usd,
        )
        text = json.dumps(ext.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        name = f"{(heard.model or self.asr_model).rsplit('/', 1)[-1]} + {answered.rsplit('/', 1)[-1]}"
        return text, VoiceRun(
            info=info, segments=list(heard.segments), transcription=heard, extraction=stats, name=name
        )

    def extract_media(
        self,
        data: bytes,
        mime_type: str,
        *,
        system: str = "",
        prompt: str = "",
        json_schema: dict | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        return self.extract_voice(
            data, mime_type, system=system, prompt=prompt, json_schema=json_schema, temperature=temperature,
            max_tokens=max_tokens,
        )[0]  # fmt: skip


# --------------------------------------------------------------------------- offline text LLM


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\wऀ-ॿ]+", " ", text or "").lower().split())


class FakeTranscriptLLM:
    """Offline stand-in for the text LLM of the open path (implements ``wapsi.ports.LLM``): when the transcript
    in the prompt is the script of a bundled sample it returns that sample's expected extraction (without the
    transcript, as a real model would); otherwise an empty one. No network, deterministic."""

    def __init__(self, name: str = "fake-transcript-llm", canned: dict[str, dict[str, Any]] | None = None) -> None:
        self.name = name
        self.model = name
        self.canned = canned  # normalised transcript -> answer; None = the bundled samples
        self.calls: list[list[dict[str, str]]] = []

    def _answers(self) -> dict[str, dict[str, Any]]:
        if self.canned is not None:
            return {_norm(k): v for k, v in self.canned.items()}
        from wapsi.intake.samples import load_samples  # lazy: no data read unless the fake is used

        return {_norm(s.script): s.expected for s in load_samples() if s.script}

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        json_schema: dict | None = None,
    ) -> str:
        self.calls.append([dict(m) for m in messages])
        user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        start, end = user.find(TRANSCRIPT_OPEN), user.find(TRANSCRIPT_CLOSE)
        said = user[start + len(TRANSCRIPT_OPEN) : end] if 0 <= start < end else ""
        answers = self._answers()
        answer = answers.get(_norm(said))
        withheld = WITHHELD_MARK in said
        if answer is None and withheld:  # a passage was withheld: the rest of the script still identifies it
            parts = [_norm(p) for p in said.split(WITHHELD_MARK)]
            parts = [p for p in parts if p]
            for script, candidate in answers.items():
                if parts and all(p in script for p in parts) and sum(map(len, parts)) >= 0.5 * len(script):
                    answer = candidate
                    break
        if answer is None:
            from wapsi.adapters.llm_fake import empty_audio_extraction

            answer = empty_audio_extraction()
        answer = {k: v for k, v in json.loads(json.dumps(answer)).items() if k != "transcript"}
        if withheld:  # like a real model, it cannot quote or translate what it was never shown
            for sentence in answer.get("instruction_like") or []:
                answer["translation_en"] = str(answer.get("translation_en") or "").replace(sentence, WITHHELD_MARK)
            answer["instruction_like"] = []
        return json.dumps(answer, ensure_ascii=False, sort_keys=True)
