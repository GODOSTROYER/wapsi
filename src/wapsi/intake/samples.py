"""The bundled synthetic voice notes (``data/samples/voice``) and their expected extractions.

``manifest.json`` lists each sample: the WAV file, its SHA-256, the TTS script it was read from, the expected
model answer (``*.expected.json``, valid against ``wapsi.intake.voice.VOICE_SCHEMA``) and the NCRP complaint it
belongs to. The offline ``FakeLLM`` answers ``extract_media`` from ``canned_extraction(sha256)``, so the whole
voice flow runs with zero network access. Regenerate the audio with ``scripts/make_voice_samples.py``.

All voices are Windows offline TTS reading synthetic scripts; no real person's voice or data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

SAMPLES_ENV = "WAPSI_VOICE_SAMPLES"
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "samples" / "voice"
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class VoiceSample:
    sample_id: str
    title: str
    path: Path
    sha256: str
    script: str
    expected: dict[str, Any]
    ncrp_ids: tuple[str, ...] = ()
    expect: dict[str, Any] = field(default_factory=dict)  # matched ids, statuses, injection (tests and docs)
    voice: str = ""
    notes: str = ""
    duration_s: float | None = None
    world: str = "showcase"  # the synthetic ledger it retells: "showcase" (fixture) or "demo" (synth seed 7)

    @property
    def filename(self) -> str:
        return self.path.name

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def samples_dir() -> Path:
    raw = os.environ.get(SAMPLES_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_DIR


@lru_cache(maxsize=4)
def _load(directory: str) -> tuple[VoiceSample, ...]:
    root = Path(directory)
    path = root / MANIFEST
    if not path.exists():
        return ()
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for s in data.get("samples", []):
        script_path = root / s["script"]
        out.append(
            VoiceSample(
                sample_id=s["id"],
                title=s.get("title", s["id"]),
                path=root / s["file"],
                sha256=s["sha256"],
                script=script_path.read_text(encoding="utf-8").strip() if script_path.exists() else "",
                expected=json.loads((root / s["expected"]).read_text(encoding="utf-8")),
                ncrp_ids=tuple(s.get("ncrp_ids", [])),
                expect=dict(s.get("expect", {})),
                voice=s.get("voice", ""),
                notes=s.get("notes", ""),
                duration_s=s.get("duration_s"),
                world=s.get("world", "showcase"),
            )
        )
    return tuple(out)


def load_samples(directory: Path | None = None) -> tuple[VoiceSample, ...]:
    """Every bundled sample (empty when the samples directory is absent, e.g. a slim image)."""
    return _load(str(directory or samples_dir()))


def get_sample(sample_id: str, directory: Path | None = None) -> VoiceSample:
    for s in load_samples(directory):
        if s.sample_id == sample_id:
            return s
    raise KeyError(sample_id)


def canned_extraction(sha256: str, directory: Path | None = None) -> dict[str, Any] | None:
    """The expected model answer for a bundled sample's audio hash (a deep copy), else None."""
    for s in load_samples(directory):
        if s.sha256 == sha256:
            return json.loads(json.dumps(s.expected))
    return None


def sample_for_hash(sha256: str, directory: Path | None = None) -> VoiceSample | None:
    return next((s for s in load_samples(directory) if s.sha256 == sha256), None)
