# AMD edition runbook: 14 to 18 Oct 2026 (lablab.ai AMD Developer Hackathon ACT III, solo entrant)

One person, five days. This runbook puts the fastest path first: **endpoint mode in about 15 minutes** once you have
an AMD Token Factory key. Everything else (a ROCm notebook, an MI300X droplet) is an extra that adds results; nothing
waits for it. Commands run from the repository root, in Git Bash on Windows.

| Fact | Value |
|---|---|
| Challenge and track | "Solve a real Business Problem with AMD technology", **Track 3: Reinvent Commerce** |
| Build window | 12 Oct 15:00 UTC to 18 Oct 15:00 UTC. The pipeline accepts runs until **18 Oct 14:00 UTC**, one hour before the form closes |
| Submission deadline | **Sun 18 Oct, 20:30 IST** (15:00 UTC). **Our stop: 17:30 IST** (12:00 UTC) |
| AMD compute every team has | **AMD Token Factory**: an OpenAI-compatible API, models served on AMD GPUs. The trial key allows 20 requests a minute, 8 at once and a $1 a day relative-cost cap, for the whole team. No billing |
| Optional AMD compute | GPU notebooks and dedicated vLLM endpoints (need your portal login); an AMD Developer Cloud MI300X droplet ($1.99 per GPU-hour, if you have credits) |
| Google partner prize | Gemma 4 (Google's open model) is the drafting model, served on AMD; the agent layer is Google's open-source ADK |
| Baseline | git tag `amd-baseline` (the repository at kickoff). Everything committed after 12 Oct 15:00 UTC is built during the event |

## The three ways to run (`kickoff/run_all.sh`)

`run_all.sh` runs the same twelve stages in whichever way the machine allows and writes the same evidence bundle
(`evidence.json` + `evidence.md`). `preflight.sh` detects the mode; `--mode` chooses it.

| Mode | When | Stages that run | What the evidence names |
|---|---|---|---|
| `endpoint` | no GPU box: a Token Factory key (or anyone's vLLM URL) | `env`, `models`, `eval`, `bench`, `ab`, `evidence` (the GNN / LoRA / serve stages are recorded as skipped) | the endpoint host and the model id. The GPU is the provider's: no GPU name, memory or power figure |
| `notebook` | an AMD ROCm JupyterLab without Docker | all twelve, as plain python: ROCm torch for the GNN and the LoRA, `python -m vllm.entrypoints.openai.api_server` to serve (transformers if vLLM is absent) | the GPU as `amd-smi` / `rocm-smi` / torch names it |
| `droplet` | an MI300X droplet with Docker | all twelve, with docker compose and the vLLM-ROCm images | `amd-smi static`: AMD Instinct MI300X |

Stages: `env` → `models` → `gnn` → `lora-data` → `lora-train` → `merge` → `lora-eval` → `serve` → `eval` → `bench` →
`ab` → `evidence`. A stage that is done is skipped on a re-run; `--stage a,b` and `--from a` repeat stages; `--dry-run`
prints every command and runs nothing.

**What may be claimed.** `collect_evidence.py` marks a bundle valid only when its identity checks pass and every stage
started inside the event window, and `fill_pitch.py` writes a number only under words that fit the run: an endpoint
run is "measured on AMD Token Factory (<model> on AMD GPUs)", never "on an MI300X"; a notebook GPU is named as it
names itself; a slot whose text says "MI300X" is filled only from an MI300X run, and one that says "Gemma" only when
Gemma drafted. CPU and rehearsal numbers are refused everywhere.

## Fastest path: endpoint mode in 15 minutes

You need the Token Factory key and nothing else. The key lives in a file outside git (or in your shell); it is never
printed, logged, cached or written into a report.

```bash
uv sync --all-extras                                   # once

# 1. The endpoint (a private file outside the repository; see kickoff/endpoint.env.example for the names)
set -a; source /path/outside/git/amd_tokenfactory.env; set +a
#    WAPSI_LLM_BASE_URL=https://developer.amd.com.cn/radeon/api/v1   WAPSI_LLM_API_KEY=...   WAPSI_LLM_MODEL=Gemma-4-31B-IT
export WAPSI_INR_PER_USD=<the day's rate>              # optional: a rupee figure next to the dollar one

# 2. Ready? (two GETs, no model call)
bash editions/amd/kickoff/preflight.sh                 # detects endpoint mode; fix every FAIL

# 3. The run: about 100 calls, 12 to 15 minutes on the trial key
bash editions/amd/kickoff/run_all.sh --mode endpoint --dry-run     # read the plan once
bash editions/amd/kickoff/run_all.sh --mode endpoint

# 4. Read it, fill the pitch, check, commit
cat editions/amd/evidence/endpoint/$(date -u +%F)/evidence.md
uv run python editions/amd/kickoff/fill_pitch.py --evidence editions/amd/evidence/endpoint/$(date -u +%F)/evidence.json          # diff
uv run python editions/amd/kickoff/fill_pitch.py --evidence editions/amd/evidence/endpoint/$(date -u +%F)/evidence.json --write
uv run python pitch/check_numbers.py                   # must print PASS
git add editions/amd/evidence/endpoint pitch && git commit -m "AMD: endpoint run $(date -u +%F)"
```

What the six stages do:

| Stage | What it does | Calls |
|---|---|---|
| `env` | preflight, the demo world (seed 7), the docs index, `endpoint.py probe`: host, provider, the model's card and price list, the limits, one short completion | 1 |
| `models` | the model plan from `GET /models`: Gemma 4 first, a different small model as judge | 0 |
| `eval` | the golden-set evaluation with the real model drafting every prose field (10 cases; `WAPSI_EP_FULL=1`: all 39), plus the harness's LLM-only baseline | about 35 |
| `bench` | `llm_bench` at concurrency 1 / 4 / 8 (tokens/s, TTFT, p50 / p95), the real drafting prompts at 1 / 4, and 6 cases end to end: cases and drafts per hour, model seconds per case, API-reported cost per case | about 50 |
| `ab` | the same model writing a free-form STR vs writing inside Wapsi's slots and verifier, on the same cases; the bound arm's drafting requests come from the disk cache of `eval` | about 10 |
| `evidence` | `evidence.json` + `evidence.md`, and the `fill_pitch.py` diff | 0 |

Things that are true of a shared trial endpoint, and what the pipeline does about each:

- **20 requests a minute for the team.** The client keeps to `WAPSI_LLM_RPM` (default 16) and
  `WAPSI_LLM_MAX_CONCURRENCY` (default 4). An HTTP 429 is waited out (`Retry-After`) and retried; it is counted, never
  an error. A benchmark level starts only when the last 60 s leave room for its whole burst; a level that was
  rate-limited or paced is shown but is never a headline number.
- **Cases per hour is the request cap, not the model.** At a few calls per case and 16 calls a minute the cap sets
  the ceiling. The bundle says so next to the number and also reports what does not depend on the cap: latency per
  request, tokens per second, model seconds per case.
- **No strict JSON schema.** Requests use `response_format: {"type": "json_object"}`; the schema is enforced by Wapsi's
  own parser and checks, as everywhere else. (`WAPSI_LLM_JSON_MODE=json_schema` is for your own vLLM.)
- **No `seed`.** Temperature 0 plus a disk cache (`data/generated/llm-cache`, gitignored): a re-run of the same case
  costs nothing and returns the same text. Benchmarks never read the cache.
- **Cost.** There is no GPU to price, so the GPU-hour cost per case stays empty (a placeholder for a notebook or
  droplet run). The bundle reports `usage.cost` as the API states it, per case and for the whole run.
- **The catalogue changes while you work.** On 14 Oct `Gemma-4-31B-IT` was served, then for about ten minutes
  `GET /models` did not list it and calls to it answered HTTP 400, then it was listed again a little before it
  answered. A model you named is never swapped silently: preflight and the probe stop and tell you. Then either wait
  (`uv run python editions/amd/kickoff/endpoint.py wait-model --call`, GETs only until it is listed) or accept the
  fallback list for this run (`export WAPSI_ALLOW_MODEL_FALLBACK=1`; the evidence then flags that the drafting model
  is not Gemma, and the "Gemma" slots of the deck are left alone).
- **Terms.** Trial and development use only, no SLA, prompts logged for about two days: send synthetic data only
  (the repository has nothing else).

More cases, later: `WAPSI_EP_FULL=1 bash editions/amd/kickoff/run_all.sh --mode endpoint --new` (every golden case;
about an hour and a few cents of the daily cost cap). `--stage bench,ab,evidence` repeats measurements into the day's
folder; `fill_pitch.py --write` merges a later bundle into `pitch/NUMBERS.md` (it replaces the rows it re-measured
and keeps the rest, each row naming its own bundle).

## Model plan (`kickoff/model_plan.py`): everything chosen by environment

| Role | Endpoint mode | Notebook / droplet | Fallbacks, in order |
|---|---|---|---|
| Drafting (prose fields only) | `WAPSI_LLM_MODEL`, default `Gemma-4-31B-IT` | `WAPSI_BASE_MODEL`, default the largest Gemma 4 the GPU serves: `google/gemma-4-31B-it`, else `google/gemma-4-E4B-it` | endpoint: `Qwen3.8-27B`, `DeepSeek-V4-Flash`, `MiMo-V2.6-Flash`, `GLM-5.3-Flash`; self-hosted: `Qwen/Qwen3.6-35B-A3B`, only if Gemma 4 will not serve |
| Judge (screens untrusted complaint text next to the rules) | `WAPSI_JUDGE_MODEL`, default `MiniCPM5-2B` (`none` = rules only) | `WAPSI_VERIFIER_MODEL_ID`, default `google/gemma-4-E4B-it` when the memory allows a second model | endpoint: `Qwen3.8-27B`, `DeepSeek-V4-Flash`, always a model other than the drafting one |

- `WAPSI_LLM_FALLBACK_MODELS` / `WAPSI_JUDGE_FALLBACK_MODELS` replace a list (comma separated).
- Gemma is the Google technology of this entry. A run drafted by a fallback model says so in `evidence.md` and never
  fills a slot whose text says "Gemma".
- A fallback model that thinks before it answers (Qwen, DeepSeek, GLM, MiMo on the shared endpoint) gets
  `reasoning_effort: "low"` and 1,024 tokens of headroom, because thinking tokens count toward `max_tokens`
  (`WAPSI_LLM_EXTRA_BODY`, `WAPSI_LLM_MAX_TOKENS_HEADROOM` override). Gemma 4 does not think by default.
- On a GPU box the memory decides (estimates from the LoRA presets, not measurements): about 80 GB to serve the 31B
  model and about 150 GB to fine-tune and merge it; below that the LoRA stages are skipped and the base model is
  served (`WAPSI_LORA=1` forces them).
- Mule, innocent-holder and merchant decisions never come from a language model: the transparent scorer and the GNN
  make them, and the golden-set report says which scorer ran.

## Day by day

Times are IST. Each day ends with a commit; the evidence folder of the day is the proof of the day.

| Day | Goal | Do | Done when |
|---|---|---|---|
| **Wed 14** | First valid evidence | The 15-minute endpoint run above. Read `evidence.md`. `fill_pitch.py` diff, `--write`, `check_numbers.py`. In the evening: `WAPSI_EP_FULL=1 ... --new` for every golden case. Ask the portal for a GPU notebook or a dedicated endpoint if one is on offer | `editions/amd/evidence/endpoint/2026-10-14/evidence.json` has `results_valid: true` and is committed |
| **Thu 15** | The product on the endpoint | App and agent against the same endpoint (same three variables). Place the endpoint slots in the deck (`fill_pitch.py --slots` lists them). If a notebook came through: the notebook path below, `env` to `gnn` first | The demo flow runs end to end on Gemma 4 served on AMD; the deck shows dated numbers |
| **Fri 16** | Extras that add proof | Notebook: `lora-data` to `evidence` (or `serve` onward if the GPU is too small to fine-tune). Re-run `--stage bench,ab,evidence` on the endpoint once more for a second dated bundle. Record screen footage of a run | A second bundle is merged into `pitch/NUMBERS.md`; footage exists |
| **Sat 17** | Freeze | Last measurements by noon. Cut the video, export the slides and the cover image, write the short and long descriptions. Stop and delete any notebook instance; destroy any droplet. `uv run pytest -q` and `uv run python pitch/check_numbers.py` both green | Nothing left that needs a GPU or the key |
| **Sun 18** | Submit by **17:30 IST** | Public repository, README that runs, demo URL (demo mode: CPU, no key needed), video, slides, technology tags, Track 3. Read the form back once. The form closes at 20:30 IST | Submission confirmed, three hours early |

Submission checklist (lablab.ai form): title, short and long description, challenge and track, technology tags,
working prototype, cover image, demo video, pitch slides, public GitHub repository (MIT), demo platform and
application URL. Judging: application of technology, presentation, business value, originality.

## Notebook mode (an AMD ROCm JupyterLab, no Docker)

Open `editions/amd/kickoff/wapsi_amd_kickoff.ipynb` from a clone inside the notebook instance and run the cells top
to bottom. Each cell calls `preflight.sh` or `run_all.sh --mode notebook --stage ...`; the notebook has no logic of
its own. The same from a terminal:

```bash
git clone https://github.com/<you>/wapsi.git && cd wapsi
export WAPSI_PYTHON=$(which python3)          # the notebook's python: the ROCm torch lives there
export SCRATCH=$HOME/wapsi-scratch            # weights and work files: the big volume
read -rs HF_TOKEN && export HF_TOKEN          # Gemma 4 licence accepted on the model pages
bash editions/amd/kickoff/preflight.sh --mode notebook
bash editions/amd/kickoff/run_all.sh --mode notebook --dry-run
bash editions/amd/kickoff/run_all.sh --mode notebook
```

- **Packages.** The image's ROCm torch is never replaced: the `uv.lock` pins of everything else are installed with
  `pip --no-deps` (`WAPSI_NB_SKIP_PIP=1` keeps the notebook's packages as they are). `train_image_check.py` then runs
  the imports, the declared torch ranges and one LoRA step on the GPU.
- **Models.** `model_plan.py` reads the GPU memory torch reports and plans the largest Gemma 4 that fits; a GPU that
  serves the model but cannot fine-tune it skips the LoRA stages and serves the base model.
- **Serving.** `serving/start_llm.sh python <profile>`: the same flags as the container, through
  `python -m vllm.entrypoints.openai.api_server`, bound to 127.0.0.1. Without vLLM, a transformers server answers one
  request at a time (`WAPSI_NB_SERVER=transformers` forces it); the benchmark then runs at concurrency 1 only and the
  evidence records the serving stack.
- **Identity and telemetry.** `amd-smi` or `rocm-smi` when present, else the device a ROCm build of torch reports
  (`torch_info.json`). Results carry that name. No `amd-smi`: no VRAM telemetry, and the rows stay empty.
- **Cost.** Set `WAPSI_GPU_HOUR_USD` to what an hour of the instance costs to get a cost per case and the budget
  guard; without it both stay empty.
- **Servers stop when `run_all.sh` exits** (the GPU is freed); the next `--stage eval` starts them again.
  `WAPSI_KEEP_SERVING=1` leaves them up for a demo.
- If the shell also carries Token Factory variables, notebook mode sets them aside: here the model is served on this
  machine, and the provider's key is never sent to a local server.
- A running instance spends credits. When the last stage is done: commit and push the evidence, download the
  adapter, then **stop and delete the instance**.

## Droplet mode (AMD Developer Cloud MI300X, docker compose)

Only if you have credits and the time; the entry does not depend on it.

> **Billing rule:** a powered-off droplet **still bills**. Only *Destroy* stops the meter. Check the billing page
> every day.

```bash
ssh root@DROPLET_IP && tmux new -s wapsi
git clone https://github.com/<you>/wapsi.git && cd wapsi
export SCRATCH=<the 5 TB mount from lsblk/df>            # it must exist: preflight fails a missing path
read -rs HF_TOKEN && export HF_TOKEN
export WAPSI_DROPLET_STARTED_AT=<creation time, ISO UTC> WAPSI_INR_PER_USD=<the day's rate>
editions/amd/kickoff/preflight.sh --mode droplet        # fix every red row
editions/amd/kickoff/run_all.sh --mode droplet --dry-run
editions/amd/kickoff/run_all.sh --mode droplet          # env → evidence, resumable; unattended overnight is fine
```

| Stage | What it does on the droplet |
|---|---|
| `env` | preflight, `amd-smi static`, `uv sync`, ~120 synthetic worlds, the docs index, pinned image digests, the ROCm training image and its check |
| `models` | the model plan (recorded), Gemma 4 31B and Gemma 4 E4B into `$SCRATCH/hf` |
| `gnn` | GraphSAGE base and no-external-signals variants on ROCm, held-out and hard-world evaluation |
| `lora-data` … `lora-eval` | base model on vLLM-ROCm as teacher, verifier-filtered dataset, TRL + PEFT bf16 LoRA, fp32 merge, base vs tuned |
| `serve` | compose: merged model + verifier + GNN + app, smoke tests |
| `eval` | the golden-set evaluation with the served model |
| `bench` | amd-smi telemetry, `llm_bench` at 1 / 4 / 16 / 64, verifier bench, `e2e_bench` at 1 / 8 / 16, `AMD_RESULTS.md` |
| `ab`, `evidence` | the A/B; `amd-smi static` (if the folder has none), the bundle, the `fill_pitch.py` diff |

If a stage fails, `run_all.sh` stops and names the log; fix it and run again (done stages are skipped). A failed
`lora-eval` removes its two vLLM containers, and `serve` removes them again before it starts. Measurements do not
stop a run: a failed benchmark request, end-to-end case or A/B case is recorded (`measure_failures` in the stage
notes, "Warnings" in `evidence.md`) and never becomes a headline number.

**Create the droplet.** Console: **GPU Droplets → Create GPU Droplet**, the **MI300X (1 GPU)** plan (192 GB, a 5 TB
scratch disk), an image that already has Docker and ROCm, your SSH key, the tag `wapsi`. Billing starts at creation.

**Firewall (the Cloud Firewall, not UFW; Docker's published ports bypass UFW).** Inbound TCP 22 from `YOUR_IP/32`
only; everything else binds to 127.0.0.1. Reach the UI through a tunnel:
`ssh -L 8501:127.0.0.1:8501 -L 8080:127.0.0.1:8080 root@DROPLET_IP`.

**First login.** `amd-smi static --asic` (one "AMD Instinct MI300X"), `docker version && docker compose version`
(Compose ≥ 2.20.2), `docker ps` (stop any pre-installed container that holds the GPU), `lsblk && df -h` (find the
scratch mount).

**Manual equivalents** (what the stages run, for a step that needs a hand):
```bash
cd editions/amd && cp .env.example .env && chmod 600 .env && serving/pull_and_pin.sh       # pinned digests
serving/start_llm.sh print gemma4-base                                                    # the exact vLLM command
docker compose up -d llm && docker compose logs -f llm                                    # first start downloads ~62 GB
cd ../.. && uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8000/v1
uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8000/v1 --concurrency 1 4 16 64 --out <dir>
uv run python -m editions.amd.bench.e2e_bench --world demo --concurrency 8 --llm-url http://127.0.0.1:8000/v1 \
  --gpu-hour-usd 1.99 --inr-per-usd <RATE_ON_THE_DAY> --name e2e_c8 --out <dir>
python3 editions/amd/bench/gpu_telemetry.py --once                                        # VRAM per container, on camera
```
**Telemetry fixtures:** compare `bench/amd_smi_*_raw.json` in the evidence folder with `editions/amd/bench/fixtures/`.
If the shape differs, copy the raw captures over the fixtures, update `tests/amd/test_amd_bench.py`, and fix the
parser until `uv run pytest tests/amd -k telemetry` passes.

**Teardown.** `git push` the evidence; copy the adapter and checkpoints off the box; `docker compose down`;
**Destroy the droplet** in the console; check the billing page.

### Droplet budget (≤ $100 at $1.99 per GPU-hour)

| GPU work | Planned hours | Cost |
|---|---|---|
| `run_all.sh --mode droplet` end to end (env → evidence), unattended | 12.5 | $24.88 |
| Re-runs of failed stages, a second `--stage serve,eval,bench,ab,evidence` | 12 | $23.88 |
| Video footage, final benchmarks | 7 | $13.93 |
| **Total planned** | **31.5** | **$62.69** |
| Buffer: re-downloads, reruns, mistakes | 18.5 | $36.81 |
| **Cap** | **50** | **$99.50** |

`run_all.sh` prints the spend before every stage (`stagelog.py budget`: hours since `WAPSI_DROPLET_STARTED_AT` ×
`WAPSI_GPU_HOUR_USD` + `WAPSI_SPENT_USD` from earlier droplets), warns at 75 % of `WAPSI_BUDGET_USD` and refuses to
start a stage at 90 % unless you pass `--ignore-budget`. Endpoint mode has no GPU-hour bill; a notebook is metered
only when `WAPSI_GPU_HOUR_USD` is set.

## Voice intake on open models (any mode)

A voice note is read by an open speech recogniser (Whisper) and an open text model on AMD fills the fields; the
ledger reconciliation is the same code as everywhere else. See [`docs/voice-intake.md`](../../docs/voice-intake.md).

```bash
# the text model alone (the bundled samples' scripts stand in for the recogniser): 6 calls, 4 s apart
uv run python -m editions.amd.bench.voice_bench --asr scripts --out editions/amd/bench/results/voice-$(date -u +%Y%m%d)

# the whole open path with local Whisper (uv sync --extra asr; CPU works for 30 s clips, a ROCm GPU is faster)
WAPSI_ASR_DEVICE=cuda uv run python -m editions.amd.bench.voice_bench --asr local --repeat 3   --out editions/amd/bench/results/voice-$(date -u +%Y%m%d)

# Whisper as a service on a GPU box (OpenAI-compatible /v1/audio/transcriptions), then point the app at it
uv run uvicorn editions.amd.asr.server:app --host 127.0.0.1 --port 8002     # uv sync --extra asr --extra amd-app
export WAPSI_ASR_BASE_URL=http://127.0.0.1:8002/v1 WAPSI_ASR_ON_AMD=1 WAPSI_VOICE_BACKEND=open

uv run python -m editions.amd.bench.voice_bench --fake                 # offline harness check, measures no model
```

Every result records where the recogniser ran and which model answered. A number from the Token Factory is
AMD-served but is not an MI300X benchmark; a CPU number is a rehearsal number.

## Demo URL during judging

Deploy **demo mode** (`app-demo`: CPU, `FakeLLM`, no GPU, no key) on any small host, or run the app against the
endpoint with the same three variables while the key is valid. Say plainly in the submission which one the live URL
is, and that the video and the evidence bundles show the AMD-served runs.

```bash
docker compose -f editions/amd/docker-compose.yml --profile demo up --build      # http://127.0.0.1:8501
```

## Before any run on a laptop (no key, no GPU, no cost)

```bash
uv sync --all-extras
bash editions/amd/kickoff/run_all.sh --rehearsal          # whole pipeline on CPU: tiny worlds, tiny model, stub LLMs
uv run pytest tests/amd -q
```
On Windows, run `run_all.sh` from **Git Bash**. In PowerShell, `bash` can resolve to WSL's `bash.exe`, which sees
neither this checkout's uv venv nor its Windows paths. The rehearsal is labelled "CPU rehearsal" everywhere, lives in
a gitignored folder, and `fill_pitch.py` refuses it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| preflight: `model served FAIL … is not served right now` | The endpoint's catalogue changed. Wait: `uv run python editions/amd/kickoff/endpoint.py wait-model --call` (returns when the model answers). Or run with the fallback list: `export WAPSI_ALLOW_MODEL_FALLBACK=1` (the evidence flags a non-Gemma drafting model). |
| `endpoint.py probe`: `is listed by GET /models but refuses calls right now (HTTP 400)` | The model is coming back. `wait-model --call`, then `run_all.sh --mode endpoint` again (it resumes at `env`). |
| Many `HTTP 429` in a result file | Someone else is using the key, or `WAPSI_LLM_RPM` is too high. Lower it (`export WAPSI_LLM_RPM=10`) and re-run `--stage bench`: a rate-limited benchmark level is never a headline number. |
| `HTTP 400 … does not support JSON schema output mode` | `WAPSI_LLM_JSON_MODE=json_schema` is set against an endpoint with no strict schema. Unset it (endpoint mode defaults to `json_object`). |
| `model returned no content (finish_reason=length)` | A model that thinks before it answers used the whole token budget on reasoning. The fallback families get `reasoning_effort: "low"` and headroom on their own; for any other model: `export WAPSI_LLM_EXTRA_BODY='{"reasoning_effort": "low"}' WAPSI_LLM_MAX_TOKENS_HEADROOM=1024`. |
| `endpoint limits WARN: little left today` | The $1 a day relative-cost cap; it resets at 16:00 UTC. The default run uses a few cents. |
| A draft fails the verifier with `whole_account_freeze` | The model's prose spoke of freezing (seen with the real model on 14 Oct: "requests to other banks to freeze the outbound funds"). The verifier is right to stop it; the draft is not approvable until the wording check also runs before the fallback paragraph is chosen (open issue for the core drafter). Report the pass rate as measured. |
| Results valid: no (`endpoint.json is missing`, `not an AMD host`) | The `env` stage records who answered. Re-run `--stage env,evidence`. For your own vLLM on AMD hardware behind a public URL, declare it: `export WAPSI_ENDPOINT_HARDWARE="AMD Instinct MI300X, vLLM-ROCm"` (recorded as declared, not measured). An endpoint on this machine belongs to notebook or droplet mode. |
| `fill_pitch.py`: "measured but not filled (the page's words do not fit this run)" | By design: the slot's text says MI300X or Gemma and this run is neither. Reword the page, or place an endpoint slot (`--slots`). |
| Notebook: `torch (ROCm) sees the GPU FAIL` | `WAPSI_PYTHON` points at a python without the ROCm torch. `export WAPSI_PYTHON=$(which python3)` inside the notebook's terminal. |
| Notebook: out of memory when serving | The plan's thresholds are estimates. `export WAPSI_BASE_MODEL=google/gemma-4-E4B-it`, or lower `VLLM_GPU_UTIL` / `VLLM_MAX_MODEL_LEN`, then `--from models`. |
| Gemma 4 errors about attention, sliding window or head size on ROCm | Gemma 4 needs `--attention-backend TRITON_ATTN`, which `start_llm.sh` adds for Gemma profiles (check with `serving/start_llm.sh print`). No such flag in this vLLM build: `VLLM_ATTENTION_FLAG=0`. On the droplet, try the day-0 image: `VLLM_IMAGE=<VLLM_GEMMA4_IMAGE digest>`. |
| Gemma 4 will not serve on your own GPU at all | Use endpoint mode for the LLM side (Gemma 4 on Token Factory). Last resort on the box: `WAPSI_BASE_PROFILE=qwen-fallback WAPSI_BASE_MODEL=Qwen/Qwen3.6-35B-A3B WAPSI_LORA_CONFIG=qwen36-35b-a3b-mi300x WAPSI_MERGED_NAME=wapsi-qwen36-str-merged`, then `--from models`; say so in the results. |
| Out of memory at vLLM start-up, or "not enough KV cache" | Start `llm` first, then the others. Lower `VLLM_MAX_MODEL_LEN` (e.g. 8192) or `VLLM_MAX_NUM_SEQS`; `VLLM_KV_CACHE_DTYPE=fp8` and/or `VLLM_QUANTIZATION=fp8`; lower `VERIFIER_GPU_UTIL`. Keep the sum of utilizations ≤ ~0.85. |
| `401/403`, gated repo, while downloading | Accept the licence on the Hugging Face model page, then `export HF_TOKEN` in the shell that runs the stage. |
| Droplet: healthcheck never turns healthy | `docker compose logs -f llm`. The first start downloads and compiles; `start_period` is 30 min. |
| Droplet: `/dev/kfd` permission denied (non-root containers such as `gnn`) | Needs the `video` and `render` groups (compose sets both for `gnn`). Check `ls -l /dev/kfd /dev/dri/*`. |
| High prose fallback rate | Look at `failed_checks` and `llm_issues` in the case JSON. A fallback is safe (the fixed paragraph passes the verifier) but should be rare. |
| `amd-smi` JSON differs from the fixtures | Capture it, update the fixtures and tests, then fix `bench/gpu_telemetry.py`. The rocm-smi fallback keeps sampling in the meantime. |
| The cases run without docs search (`docs_index=missing`) | `data/docs/raw` is not in git and the public sites did not answer. Copy it from a machine that has it (`scp -r data/docs/raw …`), then `uv run python -m wapsi.search.build` and re-run the stage. |
| Training image (droplet) or `train_image_check.py` fails | The image keeps its ROCm torch and installs the `uv.lock` pins with `--no-deps`. Read `train_image_check.json` (which import or the LoRA step failed), then use a newer ROCm PyTorch image (`export ROCM_PYTORCH_IMAGE=…`, `--stage env`) or pin the failing library lower in `requirements-rocm.txt`. |
