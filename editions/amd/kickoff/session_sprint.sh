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
# Output: $SPRINT_OUT/progress.log, $SPRINT_OUT/run.log, $SPRINT_OUT/pack_<block>.tgz
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${ROOT}"
OUT="${SPRINT_OUT:-/workspace/runlogs}"
mkdir -p "${OUT}"
BLOCKS="${SPRINT_BLOCKS:-env serve eval bench ab gnn voice evidence}"
export WAPSI_GPU_HOUR_USD="${WAPSI_GPU_HOUR_USD:-1.99}"
RUN=(bash editions/amd/kickoff/run_all.sh --mode notebook --skip-preflight --ignore-budget --keep-going)

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
  python3 - <<'PY' >> "${OUT}/run.log" 2>&1
import glob, json, time, os, datetime
import torch
from transformers import pipeline
dev = 0 if torch.cuda.is_available() else -1
t0 = time.time()
asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
               torch_dtype=torch.float16 if dev == 0 else torch.float32, device=dev)
load_s = time.time() - t0
rows = []
for wav in sorted(glob.glob("data/samples/voice/*.wav")):
    import wave
    with wave.open(wav) as w:
        dur = w.getnframes() / w.getframerate()
    t = time.time()
    out = asr(wav, generate_kwargs={"task": "transcribe"}, return_timestamps=True)
    el = time.time() - t
    rows.append({"file": os.path.basename(wav), "audio_s": round(dur, 2), "asr_s": round(el, 3),
                 "rtf": round(el / dur, 4), "chars": len(out.get("text", ""))})
res = {"measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "model": "openai/whisper-large-v3-turbo",
       "device": torch.cuda.get_device_name(0) if dev == 0 else "cpu", "torch": torch.__version__, "load_s": round(load_s, 2),
       "samples": rows, "mean_rtf": round(sum(r["rtf"] for r in rows) / max(len(rows), 1), 4)}
os.makedirs("editions/amd/bench/results/voice-notebook", exist_ok=True)
json.dump(res, open("editions/amd/bench/results/voice-notebook/whisper_rtf.json", "w"), indent=1)
print("voice bench", res["device"], "mean RTF", res["mean_rtf"])
PY
}

note "SPRINT START blocks=[${BLOCKS}] commit=$(git rev-parse --short HEAD)"
for b in ${BLOCKS}; do
  note "BLOCK ${b} start"
  case "${b}" in
    env)      "${RUN[@]}" --stage env >> "${OUT}/run.log" 2>&1 ;;
    serve)    vllm_overlay >> "${OUT}/run.log" 2>&1; "${RUN[@]}" --stage models,serve >> "${OUT}/run.log" 2>&1 ;;
    eval)     "${RUN[@]}" --stage eval >> "${OUT}/run.log" 2>&1 ;;
    bench)    "${RUN[@]}" --stage bench >> "${OUT}/run.log" 2>&1 ;;
    ab)       "${RUN[@]}" --stage ab >> "${OUT}/run.log" 2>&1 ;;
    gnn)      "${RUN[@]}" --stage gnn >> "${OUT}/run.log" 2>&1 ;;
    voice)    voice_bench ;;
    evidence) "${RUN[@]}" --stage evidence >> "${OUT}/run.log" 2>&1 ;;
    *)        note "unknown block ${b}" ;;
  esac
  note "BLOCK ${b} end rc=$?"
  pack "${b}"
done
note "SPRINT DONE"
