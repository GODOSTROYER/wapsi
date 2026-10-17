"""Build the Wapsi AMD ACT III demo video (draft) from beats.py, slides/ and shots/.

    # once: slides and app screenshots (the app must be running for capture_ui.py)
    uv run --with playwright python pitch/amd/video/scripts/render_slides.py
    uv run --with playwright python pitch/amd/video/scripts/capture_ui.py
    # narration (Windows SAPI), clips, captions, final MP4, SCRIPT_FINAL.md
    uv run python pitch/amd/video/scripts/build_video.py [--voice "Microsoft Zira Desktop"] [--rate 0] [--retts]

Needs ffmpeg with libx264 and libass on PATH, and PowerShell (System.Speech) for the voice.
Output: pitch/amd/video/wapsi_amd_demo_draft.mp4, captions.srt, SCRIPT_FINAL.md (timings from the real audio).

Another video from the same pipeline: `--beats <beats .py file> --out-dir <dir>` reads SCENES / SOURCES (and the
optional OUT_NAME, SCRIPT_TITLE, SCRIPT_NOTES, MAX_SECONDS) from that file and writes audio/, build/, the MP4,
captions.srt and SCRIPT_FINAL.md under <dir>; app screenshots and slides are looked up in <dir>/shots and
<dir>/slides first, then in this video's shots/ and slides/ (e.g. pitch/amd/video_usecases).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from beats import SCENES, SOURCES  # noqa: E402

AUDIO, BUILD, SLIDES, SHOTS = ROOT / "audio", ROOT / "build", ROOT / "slides", ROOT / "shots"
SLIDE_DIRS, SHOT_DIRS = [SLIDES], [SHOTS]  # searched in order (--out-dir puts its own folders first)
FPS = 30
GAP = 0.25  # silence after each beat
SCENE_GAP = 0.45  # extra silence at the end of a scene
FRAME = (144, 22, 1632, 918)  # where an app screenshot sits on slides/uiframe.png
PAPER = "0xF2ECE1"
OUT_MP4 = ROOT / "wapsi_amd_demo_draft.mp4"
SCRIPT_TITLE = "# Wapsi · AMD ACT III demo video: final narration (draft cut)"
SCRIPT_NOTES = ("Every figure carries its place and date on screen. MI300X figures: measured 14 Oct 2026 on an AMD "
                "Instinct MI300X (AMD AI Notebook, ROCm 7.14, Gemma-4-31B-IT on vLLM 0.23). The app walkthrough is the "
                "local Streamlit app in recorded mode (prose recorded from Gemma-4-31B-IT on AMD Token Factory, "
                "14-10-2026); the agent trace was recorded on AMD Token Factory. Synthetic data only.")
MAX_SECONDS = 300
BEATS_NAME = "scripts/beats.py"


def configure(beats_file: str | None, out_dir: str | None) -> None:
    """Point the pipeline at another beats file and output folder (defaults: this video, unchanged)."""
    global ROOT, AUDIO, BUILD, OUT_MP4, SCENES, SOURCES, SCRIPT_TITLE, SCRIPT_NOTES, MAX_SECONDS, BEATS_NAME
    if beats_file:
        spec = importlib.util.spec_from_file_location("video_beats", Path(beats_file).resolve())
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        SCENES, SOURCES = mod.SCENES, mod.SOURCES
        SCRIPT_TITLE = getattr(mod, "SCRIPT_TITLE", SCRIPT_TITLE)
        SCRIPT_NOTES = getattr(mod, "SCRIPT_NOTES", SCRIPT_NOTES)
        MAX_SECONDS = getattr(mod, "MAX_SECONDS", MAX_SECONDS)
        out_name = getattr(mod, "OUT_NAME", None)
        bp = Path(beats_file).resolve()
        base = Path(out_dir).resolve() if out_dir else ROOT
        BEATS_NAME = bp.relative_to(base).as_posix() if bp.is_relative_to(base) else bp.name
    else:
        out_name = None
    if out_dir:
        ROOT = Path(out_dir).resolve()
        AUDIO, BUILD = ROOT / "audio", ROOT / "build"
        SLIDE_DIRS.insert(0, ROOT / "slides")
        SHOT_DIRS.insert(0, ROOT / "shots")
        OUT_MP4 = ROOT / (out_name or OUT_MP4.name)


def find(dirs: list[Path], name: str) -> Path:
    for d in dirs:
        if (d / name).exists():
            return d / name
    raise SystemExit(f"{name} not found in {', '.join(map(str, dirs))}")


def run(cmd: list[str], cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(" ".join(cmd))
        print(r.stderr[-3000:])
        raise SystemExit(f"command failed ({r.returncode})")


def beats() -> list[dict]:
    out = []
    for si, sc in enumerate(SCENES, 1):
        for bi, b in enumerate(sc["beats"], 1):
            out.append(dict(b, id=f"s{si}b{bi:02d}", scene=sc["title"], first=bi == 1, last=bi == len(sc["beats"])))
    return out


def tts(items: list[dict], voice: str, rate: int, force: bool, engine: str = "sapi", model: str = "") -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    man_p = AUDIO / "manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8")) if man_p.exists() and not force else {}
    todo, old = [], dict(man)
    for b in items:
        tag = f"gemini|{model}|{voice}|{GEMINI_STYLE}" if engine == "gemini" else f"{voice}|{rate}"
        key = hashlib.sha1(f"{tag}|{b['say']}".encode()).hexdigest()
        if man.get(b["id"]) != key or not (AUDIO / f"{b['id']}.wav").exists():
            todo.append({"id": b["id"], "say": b["say"]})
            man[b["id"]] = key
    if todo and engine == "gemini":
        done: list[str] = []
        try:
            gemini_tts(todo, model, voice, done)
        finally:  # keep what was synthesised; lines that failed stay due
            for b in todo:
                if b["id"] in done:
                    continue
                if b["id"] in old:
                    man[b["id"]] = old[b["id"]]
                else:
                    man.pop(b["id"], None)
            man_p.write_text(json.dumps(man, indent=1), encoding="utf-8")
    elif todo:
        lines = AUDIO / "lines.json"
        lines.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "tts.ps1"),
             "-Lines", str(lines), "-OutDir", str(AUDIO), "-Voice", voice, "-Rate", str(rate)])
    man_p.write_text(json.dumps(man, indent=1), encoding="utf-8")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_STYLE = ("Confident narrator of a fast-moving startup product demo, Indian English accent, brisk pace, "
                "speaking quickly but clearly, no long pauses, warm and serious, never theatrical.")


NO_METADATA: set[str] = set()  # models that reject the style annotation: the style goes into the text instead
EXHAUSTED: set[tuple[str, str]] = set()  # (model, key) pairs whose daily quota is used up in this run


def as_wav(audio: bytes, mime: str) -> bytes:
    """Some TTS models return a WAV file, others raw 16-bit mono PCM (audio/L16;rate=24000): wrap the latter."""
    import io
    import re

    if audio[:4] == b"RIFF":
        return audio
    m = re.search(r"rate=(\d+)", mime)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(m.group(1)) if m else 24000)
        w.writeframes(audio)
    return buf.getvalue()


def gemini_line(text: str, keys: list[str], start: int, model: str, voice: str) -> bytes:
    """One line through Gemini TTS; keys are tried round robin from `start`, rotating on 429 / 5xx. A key whose daily
    quota is used up is not tried again; when every key is, the run stops (no other voice is substituted)."""
    import base64
    import time
    import urllib.error
    import urllib.request

    def body() -> bytes:
        if model in NO_METADATA:
            content = {"type": "text", "text": f"Read this aloud as a {GEMINI_STYLE[0].lower()}{GEMINI_STYLE[1:]} "
                                               f"Say only the text after the colon: {text}"}
        else:
            content = {"type": "text", "text": text,
                       "annotations": [{"type": "speech_metadata", "style": GEMINI_STYLE}]}
        return json.dumps({"model": model, "response_format": {"type": "audio"},
                           "generation_config": {"speech_config": [{"voice": voice}]},
                           "input": [{"type": "user_input", "content": [content]}]}).encode()

    last = ""
    for attempt in range(len(keys) * 4):
        key = keys[(start + attempt) % len(keys)]
        if (model, key) in EXHAUSTED:
            if all((model, k) in EXHAUSTED for k in keys):
                raise SystemExit(f"Gemini TTS: the daily quota of {model} is used up on every key")
            continue
        req = urllib.request.Request(GEMINI_URL, data=body(), method="POST",
                                     headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read())
            for step in out.get("steps", []):
                for c in step.get("content", []):
                    if c.get("type") == "audio" and c.get("data"):
                        return as_wav(base64.b64decode(c["data"]), c.get("mime_type") or c.get("mimeType") or "")
            last = "no audio in the response"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            msg = e.read()[:400].decode(errors="replace")
            if e.code == 400 and "metadata is not supported" in msg:
                NO_METADATA.add(model)
                continue
            if e.code == 429 and "per day" in msg:
                EXHAUSTED.add((model, key))
                continue
            if e.code not in (429, 500, 502, 503, 504):
                raise SystemExit(f"Gemini TTS failed: {last} {msg[:200]!r}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
        time.sleep(min(2 * (attempt // len(keys) + 1), 20))
    raise SystemExit(f"Gemini TTS failed after retries: {last}")


def gemini_tts(todo: list[dict], model: str, voice: str, done: list[str] | None = None) -> None:
    """Keys come from GEMINI_API_KEYS (comma-separated) in the environment; never printed."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")).split(",")
            if k.strip()]
    if not keys:
        raise SystemExit("set GEMINI_API_KEYS (comma-separated) for --engine gemini")

    def one(i_b: tuple[int, dict]) -> str:
        i, b = i_b
        wav = gemini_line(b["say"], keys, i, model, voice)
        (AUDIO / f"{b['id']}.wav").write_bytes(wav)
        return b["id"]

    with ThreadPoolExecutor(max_workers=len(keys)) as ex:
        futures = [ex.submit(one, ib) for ib in enumerate(todo)]
        errors = []
        for f in futures:
            try:
                bid = f.result()
                print(f"tts {bid}")
                if done is not None:
                    done.append(bid)
            except SystemExit as e:
                errors.append(str(e))
        if errors:
            raise SystemExit(errors[0])


def wav_seconds(p: Path) -> float:
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


def ease(y0: int, y1: int, d: float) -> str:
    return f"{y0}+({y1 - y0})*(0.5-0.5*cos(PI*min(t/{d:.3f}\\,1)))"


def clip(b: dict, d: float) -> Path:
    vis = b["vis"]
    key = hashlib.sha1(json.dumps([vis, round(d, 3), b["first"], 3]).encode()).hexdigest()[:12]
    out = BUILD / "clips" / f"{b['id']}_{key}.mp4"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    fade = f",fade=in:st=0:d=0.3:color={PAPER}" if b["first"] else ""
    enc = ["-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-t", f"{d:.3f}"]
    if vis[0] == "slide":
        run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}",
             "-i", str(find(SLIDE_DIRS, f"{vis[1]}.png")), "-vf", f"format=yuv420p{fade}", *enc, str(out)])
    else:
        _, shot, x, w, y0, y1 = vis
        from PIL import Image  # pillow ships with the project's deps

        shot_p = find(SHOT_DIRS, f"{shot}.png")
        img_h = Image.open(shot_p).size[1]
        h = round(w * 9 / 16)
        y0, y1 = (max(0, min(v, img_h - h)) for v in (y0, y1))
        fx, fy, fw, fh = FRAME
        flt = (f"[0]crop=w={w}:h={h}:x={x}:y='{ease(y0, y1, d)}',scale={fw}:{fh}:flags=lanczos[s];"
               f"[1][s]overlay={fx}:{fy},format=yuv420p{fade}")
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(shot_p),
             "-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(find(SLIDE_DIRS, "uiframe.png")),
             "-filter_complex", flt, *enc, str(out)])
    return out


def chunks(text: str, limit: int = 100) -> list[str]:
    """Caption pieces: whole sentences merged up to `limit` characters (two lines at most); a longer sentence is
    split at the punctuation (else the space) nearest its middle."""
    import re

    def split(piece: str) -> list[str]:
        if len(piece) <= limit:
            return [piece]
        mid = len(piece) // 2
        cuts = [m.end() for m in re.finditer(r"[,:;·]\s", piece)] or [m.start() + 1 for m in re.finditer(" ", piece)]
        cut = min(cuts, key=lambda c: abs(c - mid))
        return split(piece[:cut].strip()) + split(piece[cut:].strip())

    out: list[str] = []
    for sent in re.split(r"(?<=[.?!])\s+", text.strip()):
        if out and len(out[-1]) + 1 + len(sent) <= limit:
            out[-1] = f"{out[-1]} {sent}"
        else:
            out.extend(split(sent))
    return out


def ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Segoe UI Semibold,42,&H00E1ECF2,&H00E1ECF2,&H002B2015,&H002B2015,0,0,0,0,100,100,0,0,3,14,0,2,160,160,44,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="Microsoft Zira Desktop")
    ap.add_argument("--rate", type=int, default=1)
    ap.add_argument("--retts", action="store_true")
    ap.add_argument("--engine", choices=["sapi", "gemini"], default="sapi",
                    help="gemini: Gemini TTS, keys from GEMINI_API_KEYS, used round robin")
    ap.add_argument("--model", default="gemini-3.8-flash-tts")
    ap.add_argument("--only", help="comma-separated beat ids: synthesise just these lines and stop (voice test)")
    ap.add_argument("--beats", help="another beats .py file (SCENES, SOURCES); default: scripts/beats.py")
    ap.add_argument("--out-dir", help="write audio/, build/, the MP4, captions and SCRIPT_FINAL.md here")
    a = ap.parse_args()
    configure(a.beats, a.out_dir)

    items = beats()
    if a.only:
        tts([b for b in items if b["id"] in a.only.split(",")], a.voice, a.rate, True, a.engine, a.model)
        return 0
    tts(items, a.voice, a.rate, a.retts, a.engine, a.model)
    a.voice = f"Gemini TTS {a.model}, voice {a.voice}" if a.engine == "gemini" else f"Windows SAPI {a.voice}"
    BUILD.mkdir(parents=True, exist_ok=True)

    t = 0.0
    timeline, clips, ass, srt = [], [], [ASS_HEAD], []
    pcm = bytearray()
    rate = None
    for b in items:
        wp = AUDIO / f"{b['id']}.wav"
        speech = wav_seconds(wp)
        d = math.ceil((speech + GAP + (SCENE_GAP if b["last"] else 0)) * FPS) / FPS
        with wave.open(str(wp)) as w:
            rate = rate or w.getframerate()
            data = w.readframes(w.getnframes())
        pcm += data + b"\x00\x00" * (int(round(d * rate)) - len(data) // 2)
        clips.append(clip(b, d))
        parts = chunks(b["cap"])
        total = sum(len(p) for p in parts)
        c0 = t
        for p in parts:
            c1 = c0 + speech * len(p) / total
            ass.append(f"Dialogue: 0,{ts_ass(c0)},{ts_ass(c1 + 0.05)},Cap,,0,0,0,,{p}")
            srt.append(f"{len(srt) + 1}\n{ts_srt(c0)} --> {ts_srt(c1)}\n{p}\n")
            c0 = c1
        timeline.append((b, t, d, speech))
        t += d
        print(f"{b['id']}  {t:7.2f}s  {b['vis'][0]}:{b['vis'][1]}")

    with wave.open(str(BUILD / "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    (BUILD / "captions.ass").write_text("\n".join(ass) + "\n", encoding="utf-8")
    (ROOT / "captions.srt").write_text("\n".join(srt), encoding="utf-8")
    (BUILD / "concat.txt").write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")

    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy",
         "video_noaudio.mp4"], cwd=BUILD)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", "video_noaudio.mp4", "-i", "narration.wav",
         "-vf", "subtitles=captions.ass", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
         "-r", str(FPS), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", "-shortest",
         str(OUT_MP4)], cwd=BUILD)

    write_script(timeline, t, a.voice)
    print(f"\n{OUT_MP4}  {t:.1f} s ({int(t // 60)}:{int(t % 60):02d})")
    if t > MAX_SECONDS:
        print(f"WARNING: longer than {mmss(MAX_SECONDS)}")
    return 0


def mmss(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def write_script(timeline: list, total: float, voice: str) -> None:
    L = [SCRIPT_TITLE, "",
         f"Generated by `scripts/build_video.py` from `{BEATS_NAME}`. Total **{mmss(total)}** "
         f"({total:.1f} s; limit {mmss(MAX_SECONDS).replace('.0', '')}). Voice: {voice} (synthesised). "
         "Timings are measured from the synthesised audio.", "",
         SCRIPT_NOTES, ""]
    scene = None
    for b, t0, d, _speech in timeline:
        if b["scene"] != scene:
            scene = b["scene"]
            L += ["", f"## {scene}", "", "| Time | Screen | Narration | Caption | Source |", "|---|---|---|---|---|"]
        vis = b["vis"]
        screen = f"slide `{vis[1]}`" if vis[0] == "slide" else f"app `{vis[1]}` (pan y {vis[4]}→{vis[5]})"
        src = SOURCES.get(b["src"], "") if b.get("src") else "words only / on-screen app"
        L.append(f"| {mmss(t0)}–{mmss(t0 + d)} | {screen} | {b['say']} | {b['cap']} | {src} |")
    L += ["", "## Sources", ""] + [f"- **{k}**: {v}" for k, v in SOURCES.items()]
    (ROOT / "SCRIPT_FINAL.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
