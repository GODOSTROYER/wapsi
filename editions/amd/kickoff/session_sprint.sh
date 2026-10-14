#!/usr/bin/env bash
# One short AMD GPU notebook session, highest-value work first.
#
# AMD AI Notebook sessions are metered and can end without warning, so this runs the kickoff stages in blocks and
# packs the evidence after every block (small .json/.md/.csv files only, no logs or weights). A copy of each pack is
# what you keep if the session dies halfway.
#
#   bash editions/amd/kickoff/session_sprint.sh            # all blocks
#   SPRINT_BLOCKS="serve eval bench" bash .../session_sprint.sh
#
# Blocks, in order: env, serve (models + serve), eval, bench, ab, gnn, voice, evidence.
# AMD AI Notebook tutorial sessions on MI300X end after about 1 hour: split the work, e.g.
#   SPRINT_BLOCKS="env serve bench ab evidence"   then   SPRINT_BLOCKS="env gnn voice evidence"
# Output: $SPRINT_OUT/progress.log, $SPRINT_OUT/run.log, $SPRINT_OUT/pack_<block>.tgz
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${ROOT}"
OUT="${SPRINT_OUT:-/workspace/runlogs}"
mkdir -p "${OUT}"
BLOCKS="${SPRINT_BLOCKS:-env serve eval bench ab gnn voice evidence}"
export WAPSI_GPU_HOUR_USD="${WAPSI_GPU_HOUR_USD:-1.99}"
RUN=(bash editions/amd/kickoff/run_all.sh --mode notebook --skip-preflight --ignore-budget --keep-going)

# One GPU, one session: the optional second-opinion judge (Gemma 4 E4B on port 8001) collides with the main engine,
# which takes 8001 for its own distributed init when 8000 is the API port, and every later stage then waits 30 min for
# it. Unless SPRINT_JUDGE=1, blank the judge in the model plan so the notebook stages never start or wait for it.
no_judge() {
  [[ "${SPRINT_JUDGE:-0}" == "1" ]] && return 0
  local plan="${WAPSI_WORK:-/root/wapsi-scratch/wapsi-work}/model_plan.env"
  if [[ -f "${plan}" ]]; then printf '
# session_sprint: no judge model this session
PLAN_JUDGE_MODEL=
' >> "${plan}"; echo "judge removed from ${plan}"; fi
}

note() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "${OUT}/progress.log"; }

# vLLM 0.23.1.dev (AMD's rocm7.14 notebook image) was built against the transformers that shipped Gemma 4 (5.5.x).
# With the newer transformers the env stage installs (5.16+), vLLM either refuses Gemma 4's per-layer config
# ("head_dim is a per-layer attribute") or, if that guard is relaxed, builds the full-attention layers with the wrong
# head size and fails while loading weights (512 vs 256). Fix: give vLLM its own transformers 5.5 + tokenizers 0.22
# in an overlay directory on PYTHONPATH; everything else keeps the pinned versions. Verified on MI300X 2026-10-14:
# google/gemma-4-31B-it loads (58.9 GiB, 24 s) and answers.
vllm_overlay() {
  local dir="${WAPSI_VLLM_OVERLAY:-$HOME/tf55}"
  if [[ ! -d "${dir}/transformers" ]]; then
    python3 -m pip install -q --no-deps --target "${dir}" "transformers>=5.5,<5.6" "tokenizers>=0.22.0,<=0.23.0"
  fi
  export PYTHONPATH="${dir}${PYTHONPATH:+:${PYTHONPATH}}"
  echo "vLLM overlay on PYTHONPATH: ${dir} ($(python3 -c 'import transformers,tokenizers;print(transformers.__version__, tokenizers.__version__)'))"
}

# Older fallback, kept for reference: relaxing the guard is NOT enough for gemma-4-31B-it (wrong head size on load).
patch_transformers() {
  python3 - <<'PY'
import os, shutil
try:
    import transformers
except Exception as e:
    print("transformers not importable:", e); raise SystemExit(0)
p = os.path.join(os.path.dirname(transformers.__file__), "integrations", "heterogeneity", "configuration_utils.py")
if not os.path.exists(p):
    print("no heterogeneity guard in transformers", transformers.__version__); raise SystemExit(0)
s = open(p).read()
old = 'allow_global_per_layer_attribute_access", False)'
if old in s:
    shutil.copy(p, p + ".orig")
    open(p, "w").write(s.replace(old, 'allow_global_per_layer_attribute_access", True)  # wapsi sprint patch'))
    print("patched", p)
else:
    print("guard already relaxed or changed in", transformers.__version__)
PY
}

pack() {  # small evidence pack: results only
  local name="$1"
  find editions/amd/evidence editions/amd/gnn/results editions/amd/bench/results evals/reports \
       -type f \( -name '*.json' -o -name '*.md' -o -name '*.csv' -o -name '*.txt' \) -size -2M 2>/dev/null \
       | tar czf "${OUT}/pack_${name}.tgz" -T - 2>/dev/null
  note "PACKED ${name} $(du -h "${OUT}/pack_${name}.tgz" | cut -f1)"
}

voice_bench() {  # Whisper on the GPU over the committed voice samples (real-time factor)
  # The notebook image has no ffmpeg, so the pipeline cannot open a .wav by path: decode each file with the wave
  # module and pass {"raw", "sampling_rate"}. This is how Whisper ran on the MI300X by hand on 2026-10-14
  # (editions/amd/bench/results/voice-mi300x-notebook-20261014/).
  python3 - <<'PY' >> "${OUT}/run.log" 2>&1
import datetime, glob, json, os, time, wave
import numpy as np
import torch
from transformers import pipeline


def load_wav(path):
    """16-bit PCM WAV -> (mono float32 in [-1, 1], sample rate), standard library only (no ffmpeg)."""
    with wave.open(path) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: {8 * width}-bit PCM; the bundled samples are 16-bit")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


dev = 0 if torch.cuda.is_available() else -1
t0 = time.time()
asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
               torch_dtype=torch.float16 if dev == 0 else torch.float32, device=dev)
load_s = time.time() - t0
rows = []
for wav in sorted(glob.glob("data/samples/voice/*.wav")):
    audio, rate = load_wav(wav)
    dur = len(audio) / rate
    t = time.time()
    out = asr({"raw": audio, "sampling_rate": rate}, generate_kwargs={"task": "transcribe"}, return_timestamps=True)
    el = time.time() - t
    rows.append({"file": os.path.basename(wav), "audio_s": round(dur, 2), "asr_s": round(el, 3),
                 "rtf": round(el / dur, 4), "chars": len(out.get("text", ""))})
rest = rows[1:] or rows  # the first sample includes kernel warm-up
now = datetime.datetime.now(datetime.timezone.utc)
res = {"measured_at": now.isoformat(), "model": "openai/whisper-large-v3-turbo",
       "device": torch.cuda.get_device_name(0) if dev == 0 else "cpu", "torch": torch.__version__,
       "load_s": round(load_s, 2),
       "note": "session_sprint.sh voice block; audio decoded with the wave module (no ffmpeg), "
               + ("fp16" if dev == 0 else "fp32") + ", return_timestamps=True; the first sample includes kernel warm-up",
       "samples": rows, "mean_rtf": round(sum(r["rtf"] for r in rows) / max(len(rows), 1), 4),
       "steady_state_rtf_excluding_first": round(sum(r["rtf"] for r in rest) / max(len(rest), 1), 4)}
out_dir = os.environ.get("SPRINT_VOICE_OUT") or f"editions/amd/bench/results/voice-notebook-{now:%Y%m%d}"
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, "whisper_rtf.json"), "w", encoding="utf-8") as fh:
    json.dump(res, fh, indent=1)
print("voice bench", res["device"], "steady-state RTF", res["steady_state_rtf_excluding_first"], "->", out_dir)
PY
}

# A block that fails is noted and packed, and the sprint goes on to the next one (run_all.sh --keep-going does the
# same inside a block): run_block runs as the left side of ||, so errexit does not stop it halfway either.
run_block() {
  case "$1" in
    env)      "${RUN[@]}" --stage env >> "${OUT}/run.log" 2>&1 ;;
    serve)    vllm_overlay >> "${OUT}/run.log" 2>&1
              "${RUN[@]}" --stage models >> "${OUT}/run.log" 2>&1
              no_judge >> "${OUT}/run.log" 2>&1
              "${RUN[@]}" --stage serve >> "${OUT}/run.log" 2>&1 ;;
    eval)     "${RUN[@]}" --stage eval >> "${OUT}/run.log" 2>&1 ;;
    bench)    "${RUN[@]}" --stage bench >> "${OUT}/run.log" 2>&1 ;;
    ab)       "${RUN[@]}" --stage ab >> "${OUT}/run.log" 2>&1 ;;
    gnn)      "${RUN[@]}" --stage gnn >> "${OUT}/run.log" 2>&1 ;;
    voice)    voice_bench ;;
    evidence) "${RUN[@]}" --stage evidence >> "${OUT}/run.log" 2>&1 ;;
    *)        note "unknown block $1"; return 2 ;;
  esac
}

note "SPRINT START blocks=[${BLOCKS}] commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
for b in ${BLOCKS}; do
  note "BLOCK ${b} start"
  rc=0
  run_block "${b}" || rc=$?
  note "BLOCK ${b} end rc=${rc}"
  pack "${b}" || note "PACK ${b} failed"
done
note "SPRINT DONE"
