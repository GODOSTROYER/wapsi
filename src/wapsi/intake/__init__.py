"""Complaint intake from untrusted media (voice notes). Everything extracted is a *claim*; the ledger decides.

- ``wapsi.intake.voice``: audio -> transcript, English translation, structured fields (one multimodal call with a
  response schema), injection screening, reconciliation.
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
