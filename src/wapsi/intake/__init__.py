"""Complaint intake from untrusted media (voice notes). Everything extracted is a *claim*; the ledger decides.

- ``wapsi.intake.voice``: audio -> transcript, English translation, structured fields (validated against one
  schema whatever model read the audio), phone masking, injection screening, reconciliation.
- ``wapsi.intake.backends``: which models read a voice note (``WAPSI_VOICE_BACKEND = auto | open | gemini | fake``).
- ``wapsi.intake.asr`` + ``wapsi.intake.extract`` + ``wapsi.intake.open_voice``: the open path. A speech recogniser
  (Whisper) writes the transcript; an open text LLM (Gemma 4 on AMD) translates and fills the fields, with JSON
  repair, schema validation and numbers checked against the transcript; ``wapsi.intake.llm_chain`` skips a model
  that returns garbage and lets the next one answer.
- ``wapsi.intake.reconcile``: every number the complainant claims is checked against the ledger
  (MATCHED / MISMATCH / NOT_FOUND).
- ``wapsi.intake.samples``: the synthetic Hinglish voice notes in ``data/samples/voice`` and their expected
  extractions (the offline ``FakeLLM`` answers from these, keyed by the audio's SHA-256).
"""

from wapsi.intake.reconcile import MATCHED, MISMATCH, NOT_FOUND, ReconRow, reconcile
from wapsi.intake.voice import (
    VOICE_SCHEMA,
    AudioLLM,
    VoiceExtraction,
    VoiceIntakeError,
    VoiceIntakeResult,
    intake_voice_note,
    sniff_audio,
    validate_extraction,
)

__all__ = [
    "MATCHED",
    "MISMATCH",
    "NOT_FOUND",
    "VOICE_SCHEMA",
    "AudioLLM",
    "ReconRow",
    "VoiceExtraction",
    "VoiceIntakeError",
    "VoiceIntakeResult",
    "intake_voice_note",
    "reconcile",
    "sniff_audio",
    "validate_extraction",
]
