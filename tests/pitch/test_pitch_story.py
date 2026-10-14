"""The AMD ACT III story holds together: the video fits its limit and opens with the frozen shop and the
freeze-versus-hold number, the Track 3 brief is answered point by point (the prediction is explained and no
language model makes it), the form fields carry the right challenge and tags, the pre-event baseline is named,
and events and platforms the project no longer targets stay out of the docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PITCH = ROOT / "pitch"
AMD = PITCH / "amd"
VIDEO = AMD / "VIDEO_SCRIPT.md"
DECK = AMD / "DECK_OUTLINE.md"
FIELDS = AMD / "LABLAB_FIELDS.md"

SEGMENT_RE = re.compile(r"^## (\d+):(\d{2}) to (\d+):(\d{2})\. (.+)$", re.M)
TOTAL_RE = re.compile(r"\*\*(\d+) s = (\d+):(\d{2})\*\*")
SLIDE_RE = re.compile(r"^## (\d+)\. (.+)$", re.M)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def _slides(text: str) -> dict[int, tuple[str, str]]:
    """{number: (title, body)} for every '## N. Title' slide of the deck outline."""
    marks = list(SLIDE_RE.finditer(text))
    return {int(m.group(1)): (m.group(2), text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)])
            for i, m in enumerate(marks)}


def _doc_files() -> list[Path]:
    return [ROOT / "README.md", ROOT / "mkdocs.yml", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md",
            *sorted((ROOT / "docs").rglob("*.md")), *sorted(PITCH.rglob("*.md"))]


# ---------------------------------------------------------------- video

def test_video_beats_are_contiguous_and_inside_the_five_minute_limit():
    text = _read(VIDEO)
    segments = _segments(text)
    assert len(segments) >= 6
    assert segments[0][0] == 0
    for (_, end, _, _), (start, _, _, _) in zip(segments, segments[1:], strict=False):
        assert start == end, "each beat starts where the previous one ends"
    total = segments[-1][1]
    assert total <= 300, "hard limit: 5:00"
    assert 240 <= total <= 270, "target 4:15, with slack for a slower read"
    # the title and the timing table state the same length as the beats add up to
    m = TOTAL_RE.search(text)
    assert m is not None and int(m.group(1)) == total == _seconds(m.group(2), m.group(3))
    assert text.splitlines()[0].startswith(f"# AMD ACT III video: {total // 60}:{total % 60:02d} ")
    table = re.findall(r"^\| [^|*]+ \| (\d+):(\d{2}) \| (\d+) \|$", text, re.M)
    assert sum(int(s) for _, _, s in table) == total
    assert [_seconds(a, b) for a, b, _ in table] == [s for s, _, _, _ in segments]


def test_video_opens_with_the_frozen_shop_and_the_freeze_versus_hold_number():
    first = _segments(_read(VIDEO))[0]
    assert first[1] <= 25
    for token in ("kirana", "₹35.26 Cr", "₹29.19 L", "0 false freezes"):
        assert token in first[3], token


def test_video_follows_the_track_brief_data_to_action_then_how_the_prediction_is_calculated():
    segments = _segments(_read(VIDEO))
    titles = [title.lower() for _, _, title, _ in segments]
    action = next(i for i, t in enumerate(titles) if "action" in t)
    prediction = next(i for i, t in enumerate(titles) if "prediction" in t)
    amd = next(i for i, t in enumerate(titles) if "amd" in t)
    assert action < prediction < amd, "data to action, then the prediction, then what runs on AMD"
    body = segments[prediction][3]
    assert "no language model" in body and "arithmetic" in body
    video = _read(VIDEO)
    for shown in ("Voice Intake", "Agent trace", "Gemma 4", "Agent Development Kit", "Whisper"):
        assert shown in video, shown
    assert "Reinvent Commerce" in video


# ---------------------------------------------------------------- deck

def test_deck_has_ten_to_twelve_slides_and_answers_the_track_brief():
    slides = _slides(_read(DECK))
    assert sorted(slides) == list(range(1, len(slides) + 1))
    assert 10 <= len(slides) <= 12
    titles = {n: title.lower() for n, (title, _) in slides.items()}
    problem = next(n for n, t in titles.items() if "merchant" in t)
    action = next(n for n, t in titles.items() if "commercial action" in t)
    prediction = next(n for n, t in titles.items() if "prediction" in t)
    effect = next(n for n, t in titles.items() if "measurable effect" in t)
    amd = next(n for n, t in titles.items() if "amd" in t)
    assert problem < action < prediction < effect < amd
    # the track rule: a language model is never the forecasting method
    assert "none for prediction" in slides[prediction][1]
    assert "GraphSAGE" in slides[prediction][1] and "transparent" in slides[prediction][1]
    # the four measures: money left free, false freezes, time to plan, restoration
    for measure in ("left free", "0 false freezes", "hold plan", "recoverable"):
        assert measure in slides[effect][1], measure
    deck = _read(DECK)
    assert "Reinvent Commerce" in deck and "Voice" in deck and "Agent trace" in deck
    assert "Gemma 4" in slides[amd][1] and "Token Factory" in slides[amd][1]


# ---------------------------------------------------------------- form fields and the pack

def test_lablab_fields_carry_the_challenge_the_tags_and_a_cover_brief():
    text = _read(FIELDS)
    challenge = text[text.index("## Selected challenge"):text.index("## Technologies and tags")]
    assert "Track 3: Reinvent Commerce" in challenge
    tags = text[text.index("## Technologies and tags"):text.index("## Cover image brief")]
    default = tags.split(chr(10) * 2)[1]  # the tag line itself; the paragraph after it says when to add more
    for tag in ("AMD Token Factory", "Gemma", "ADK", "LiteLLM"):
        assert tag in default, tag
    # tag only what the submitted build used: the hardware tags of a committed notebook run, never the droplet's
    # platform tag until a droplet run is committed
    notebook_run = sorted((ROOT / "editions" / "amd" / "evidence" / "notebook").glob("*/evidence.json"))
    for tag in ("ROCm", "vLLM", "AMD Instinct"):
        assert (tag in default) == bool(notebook_run) and tag in tags, tag
    droplet_run = sorted((ROOT / "editions" / "amd" / "evidence").glob("20*/evidence.json"))
    assert ("AMD Developer Cloud" in default) == bool(droplet_run) and "AMD Developer Cloud" in tags
    assert "## Cover image brief" in text and "16:9" in text
    assert "15:00 UTC" in text, "the official deadline"


def test_pitch_pack_has_the_track_and_partner_pages_and_no_dropped_event_folder():
    assert not (PITCH / "aibc").exists()
    fit = _read(PITCH / "TRACK_FIT.md")
    for heading in ("A real customer or merchant problem", "A useful commercial action",
                    "Relevant recommendations", "A measurable effect"):
        assert heading in fit, heading
    assert "Do not use a language model as the only forecasting method" in fit
    google = _read(PITCH / "GOOGLE_PRIZE.md")
    for word in ("Gemma 4", "Agent Development Kit", "LiteLLM", "AMD"):
        assert word in google, word
    evolus = _read(PITCH / "EVOLUS_TRACK.md")
    assert "plan only" in evolus.lower() and "TODO (user)" in evolus
    assert "**Agent**" in evolus and "**Workflow**" in evolus, "at least two named building blocks"
    start = _read(PITCH / "START_HERE_FOR_JUDGES.md")
    assert "Reinvent Commerce" in start and "TRACK_FIT.md" in start


def test_a_shared_endpoint_result_is_never_pitched_as_an_mi300x_benchmark():
    """Rows measured by the live smoke run say 'AMD Token Factory' and do not name the MI300X."""
    numbers = _read(PITCH / "NUMBERS.md")
    rows = [line for line in numbers.splitlines() if line.startswith("|") and "pitch/amd/evidence/" in line
            and "| Slot |" not in line and line.count("|") == 6]
    for row in rows:
        pitched = row.split("|")[2]
        assert "AMD Token Factory" in pitched and "MI300X" not in pitched, row


# ---------------------------------------------------------------- docs

def test_docs_name_the_baseline_tag_and_the_amd_modes():
    originality = _read(ROOT / "docs" / "originality.md")
    for fact in ("amd-baseline", "1d500f6", "2026-10-07", "Built during the event"):
        assert fact in originality, fact
    editions = _read(ROOT / "docs" / "editions.md")
    for mode in ("Endpoint mode", "Notebook mode", "Droplet mode", "Token Factory", "optional managed-cloud adapters"):
        assert mode in editions, mode
    readme = _read(ROOT / "README.md")
    for word in ("Reinvent Commerce", "What runs on AMD", "Gemma 4", "Whisper", "GraphSAGE", "amd-baseline",
                 "WAPSI_LLM_BASE_URL", "WAPSI_LLM_API_KEY", "WAPSI_LLM_MODEL"):
        assert word in readme, word
    # the hero opens with the merchant's problem before anything about the platform
    assert readme.index("kirana shop") < readme.index("## What runs on AMD") < readme.index("## Quick start")


@pytest.mark.parametrize("pattern", ["snowflake", "cortex", "coco", "ai builder cup", "aibc"])
def test_dropped_events_and_platforms_are_not_mentioned_in_docs_or_pitch(pattern):
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in _doc_files()
            for n, line in enumerate(_read(p).splitlines(), start=1)
            if re.search(rf"\b{pattern}\b", line, re.I)]
    assert hits == []


def test_no_key_or_secret_looking_value_is_written_in_docs_or_pitch():
    key_like = re.compile(r"\b(?:rc|sk|hf)[-_][A-Za-z0-9_-]{16,}")
    files = [*_doc_files(), *sorted((AMD / "evidence").glob("*"))]
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in files if p.is_file()
            for n, line in enumerate(_read(p).splitlines(), start=1) if key_like.search(line)]
    assert hits == []
