"""Write ``wapsi_amd_kickoff.ipynb``: the kickoff pipeline for an AMD ROCm JupyterLab (standard library only).

    python3 editions/amd/kickoff/make_notebook.py            # (re)write the notebook next to this file
    python3 editions/amd/kickoff/make_notebook.py --check    # exit 1 if the committed notebook is out of date

The notebook holds no logic of its own: every cell calls ``preflight.sh`` / ``run_all.sh`` (the same scripts the
droplet and a laptop use), so what the notebook runs is what the tests cover. It is generated from the cell list
below (nbformat 4.5, no outputs, no execution counts) and a test keeps the committed file equal to this output.
Secrets are read with ``getpass`` into the kernel's environment only: nothing is printed and nothing is stored in
the notebook file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOTEBOOK = HERE / "wapsi_amd_kickoff.ipynb"

MD, CODE = "markdown", "code"
CELLS: list[tuple[str, str, str]] = [
    (MD, "title", """\
# Wapsi on AMD: event-week pipeline in a ROCm notebook

This notebook runs the Wapsi kickoff pipeline in an **AMD ROCm JupyterLab without Docker**. Every cell calls the
repository's own scripts (`editions/amd/kickoff/preflight.sh`, `run_all.sh`); nothing is reimplemented here.

| Mode | When | What runs |
|---|---|---|
| `notebook` | this kernel has an AMD GPU (ROCm build of torch) | GNN training and the LoRA fine-tune as plain python, serving with `python -m vllm.entrypoints.openai.api_server` (transformers if vLLM is absent), benchmarks with `amd-smi` / `rocm-smi` snapshots |
| `endpoint` | no GPU here, but an OpenAI-compatible endpoint (AMD Token Factory, or your own vLLM) | golden-set evaluation, raw-LLM vs Wapsi-bound A/B, latency and throughput, API-reported cost |

Both write the same evidence bundle (`evidence.json`, `evidence.md`). **Results are reported under the name the
hardware reports**: the GPU as `amd-smi` or torch names it in notebook mode, the endpoint host and model id in
endpoint mode. Nothing here claims an MI300X unless the notebook's GPU is one.

All data is synthetic. A running notebook instance spends credits: stop and delete it when the last cell is done.
"""),
    (CODE, "repo", """\
# 1. Find the repository (open this notebook from inside a clone, or set WAPSI_REPO to the clone's path).
#    New instance:  !git clone https://github.com/<you>/wapsi.git  and reopen the notebook from the clone.
import os
import pathlib
import subprocess
import sys

start = pathlib.Path(os.environ.get("WAPSI_REPO", ".")).resolve()
REPO = next((p for p in [start, *start.parents] if (p / "editions" / "amd" / "kickoff" / "run_all.sh").exists()), None)
assert REPO is not None, "not inside a Wapsi clone: git clone the repository, or set WAPSI_REPO"
os.chdir(REPO)
os.environ["WAPSI_PYTHON"] = sys.executable  # the kernel's python: the ROCm torch lives here
print("repository:", REPO)
print("python:", sys.executable)
"""),
    (CODE, "secrets", """\
# 2. Secrets: typed here, kept in this kernel's environment only. Never printed, never saved in the notebook.
import getpass


def secret(name: str, prompt: str) -> None:
    if not os.environ.get(name):
        value = getpass.getpass(prompt)
        if value:
            os.environ[name] = value
    print(f"{name}: {'set' if os.environ.get(name) else 'not set'}")


secret("HF_TOKEN", "Hugging Face read token (Gemma 4 is Apache-2.0 and not gated; a token avoids rate limits); empty to skip: ")
# Only for endpoint mode (no GPU in this kernel):
secret("WAPSI_LLM_API_KEY", "Endpoint API key (AMD Token Factory); empty to skip: ")
"""),
    (CODE, "settings", """\
# 3. Settings (all optional). SCRATCH holds the weights and work files: point it at the big volume.
os.environ.setdefault("SCRATCH", str(pathlib.Path.home() / "wapsi-scratch"))
# os.environ["WAPSI_INR_PER_USD"] = "88.5"       # the day's rate, for a rupee cost per case
# os.environ["WAPSI_GPU_HOUR_USD"] = "1.99"      # what an hour of this instance costs, if you know it
# os.environ["WAPSI_NB_SERVER"] = "transformers" # force the transformers server (default: vLLM when importable)
# os.environ["WAPSI_BASE_MODEL"] = "google/gemma-4-E4B-it"  # override the model plan (default: largest Gemma 4 that fits)
# Endpoint mode: os.environ["WAPSI_LLM_BASE_URL"] = "https://developer.amd.com.cn/radeon/api/v1"

MODE = "notebook"  # "endpoint" when this kernel has no AMD GPU


def sh(*args: str) -> int:
    \"\"\"Run one of the repository's scripts and stream its output.\"\"\"
    proc = subprocess.Popen(["bash", *args], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(line, end="")
    return proc.wait()


def stage(*names: str) -> int:
    \"\"\"run_all.sh for the given stages (resumable: a stage that is done is skipped unless named here).\"\"\"
    return sh("editions/amd/kickoff/run_all.sh", "--mode", MODE, "--stage", ",".join(names))
"""),
    (CODE, "preflight", """\
# 4. Preflight: a green / amber / red table. Fix every FAIL before going on.
#    No AMD GPU in this kernel?  Set MODE = "endpoint" above and export WAPSI_LLM_BASE_URL.
sh("editions/amd/kickoff/preflight.sh", "--mode", MODE)
"""),
    (CODE, "plan", """\
# 5. The plan: every command the run will execute, nothing executed.
sh("editions/amd/kickoff/run_all.sh", "--mode", MODE, "--dry-run")
"""),
    (CODE, "env-models", """\
# 6. Environment and models: packages on the ROCm torch (the image's torch is kept), the GPU's identity, synthetic
#    worlds, the model plan by GPU memory (largest Gemma 4 that fits), the weights.
stage("env", "models")
"""),
    (CODE, "gnn", """\
# 7. GNN: GraphSAGE mule scorer trained on this GPU (notebook mode only; endpoint mode records it as skipped).
stage("gnn")
"""),
    (CODE, "lora", """\
# 8. LoRA: verifier-filtered dataset from the base model, fine-tune, merge, base vs tuned (notebook mode only).
#    Skipped by the model plan when the GPU has too little memory to fine-tune the planned model.
stage("lora-data", "lora-train", "merge", "lora-eval")
"""),
    (CODE, "measure", """\
# 9. Serve, then measure: golden-set evaluation with the model, benchmarks, raw-LLM vs Wapsi-bound A/B.
#    (Endpoint mode skips "serve": the provider serves the model.)
stage("serve", "eval", "bench", "ab")
"""),
    (CODE, "evidence", """\
# 10. The evidence bundle: evidence.json + evidence.md, and a preview of what fill_pitch.py would write.
stage("evidence")
"""),
    (CODE, "show", """\
# 11. Read the bundle.
from IPython.display import Markdown, display

root = REPO / "editions" / "amd" / "evidence" / MODE
latest = max((p for p in root.glob("*/evidence.md")), key=lambda p: p.parent.name, default=None)
display(Markdown(latest.read_text(encoding="utf-8")) if latest else "no evidence.md yet: run the cells above")
"""),
    (MD, "teardown", """\
## When the last cell is done

1. `git add editions/amd/evidence && git commit` and push: the bundle is the proof of the run.
2. Download what cannot be rebuilt cheaply (the LoRA adapter under `$SCRATCH/wapsi-work/lora/run/adapter`).
3. **Stop and delete the notebook instance** in the portal: a running instance spends credits.

To fill the pitch from the bundle (on your laptop, after pulling):
`uv run python editions/amd/kickoff/fill_pitch.py --evidence editions/amd/evidence/<mode>/<date>/evidence.json`
"""),
]


def build() -> dict:
    cells = []
    for kind, cid, text in CELLS:
        cell = {"cell_type": kind, "id": f"wapsi-{cid}", "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == CODE:
            cell.update(execution_count=None, outputs=[])
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def render() -> str:
    return json.dumps(build(), indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed notebook differs")
    ap.add_argument("--out", default=str(NOTEBOOK))
    args = ap.parse_args(argv)
    out = Path(args.out)
    text = render()
    if args.check:
        same = out.exists() and out.read_text(encoding="utf-8") == text
        print(f"{out.name}: {'up to date' if same else 'OUT OF DATE (run make_notebook.py)'}")
        return 0 if same else 1
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(CELLS)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
