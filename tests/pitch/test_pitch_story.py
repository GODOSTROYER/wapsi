"""The AI Builder Cup story holds together: the video fits under three minutes with the freeze-vs-hold number first,
the voice-note and agent-trace beats are real features (not workarounds), and the removed edition stays removed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AIBC = ROOT / "pitch" / "aibc"
VIDEO = AIBC / "VIDEO_SCRIPT.md"
DECK = AIBC / "DECK_OUTLINE.md"

SEGMENT_RE = re.compile(r"^## (\d+):(\d{2}) to (\d+):(\d{2})\. (.+)$", re.M)
TOTAL_RE = re.compile(r"\*\*(\d+) s = (\d+):(\d{2})\*\*")


def _seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


def _segments(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, title, body) for every '## M:SS to M:SS. Title' section of the video script."""
    marks = list(SEGMENT_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end_of_body = marks[i + 1].start() if i + 1 < len(marks) else text.find("## Timing check", m.end())
        out.append((_seconds(m.group(1), m.group(2)), _seconds(m.group(3), m.group(4)), m.group(5),
                    text[m.end():end_of_body]))
    return out


def test_video_beats_are_contiguous_and_under_three_minutes():
    text = VIDEO.read_text(encoding="utf-8")
    segments = _segments(text)
    assert len(segments) >= 6
    assert segments[0][0] == 0
    for (_, end, _, _), (start, _, _, _) in zip(segments, segments[1:], strict=False):
        assert start == end, "each beat starts where the previous one ends"
    total = segments[-1][1]
    assert total < 180, "hard limit: under 3:00"
    # the title and the timing table state the same length as the beats add up to
    m = TOTAL_RE.search(text)
    assert m is not None and int(m.group(1)) == total == _seconds(m.group(2), m.group(3))
    assert text.splitlines()[0].startswith(f"# AI Builder Cup video: {total // 60}:{total % 60:02d} ")
    table = re.findall(r"^\| [^|*]+ \| (\d+):(\d{2}) \| (\d+) \|$", text, re.M)
    assert sum(int(s) for _, _, s in table) == total
    assert [_seconds(a, b) for a, b, _ in table] == [s for s, _, _, _ in segments]


def test_video_opens_with_the_freeze_versus_hold_number():
    first = _segments(VIDEO.read_text(encoding="utf-8"))[0]
    assert first[1] <= 20
    for token in ("₹35.26 Cr", "₹29.19 L", "0 false freezes"):
        assert token in first[3], token


def test_voice_note_and_agent_trace_are_shown_as_real_features():
    video = VIDEO.read_text(encoding="utf-8")
    deck = DECK.read_text(encoding="utf-8")
    titles = " ".join(title for _, _, title, _ in _segments(video)).lower()
    assert "voice note" in titles and "trace" in titles
    assert "Voice Intake" in video and "Agent trace" in video
    assert "Voice Intake" in deck and "Agent trace" in deck
    # the old workarounds for features that did not exist yet are gone
    for stale in ("If the voice-note intake is built", "no tool-trace panel", "WIN_PLAN"):
        assert stale not in video, stale
    roadmap = deck[deck.index("## 11. Roadmap"):deck.index("## 12.")]
    assert "voice-note intake with Gemini audio" not in roadmap


@pytest.mark.parametrize("pattern", ["snowflake", "cortex"])
def test_removed_edition_is_not_mentioned_in_docs_or_pitch(pattern):
    files = [ROOT / "README.md", ROOT / "mkdocs.yml", *sorted((ROOT / "docs").rglob("*.md")),
             *sorted((ROOT / "pitch").rglob("*.md"))]
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1)
            if re.search(rf"\b{pattern}\b", line, re.I)]
    assert hits == []
