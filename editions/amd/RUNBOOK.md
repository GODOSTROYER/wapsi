# AMD edition runbook: event week (lablab.ai AMD Developer Hackathon ACT III, 12–18 Oct 2026)

This is the step-by-step plan for one AMD Instinct MI300X droplet on AMD Developer Cloud. The droplet costs
$1.99 per GPU-hour and the budget is ≤ $100. All GPU work, and every number we publish, happens between
**12 Oct 15:00 UTC and 18 Oct 14:00 UTC**. Commands are run from the repository root unless stated otherwise.

> **Billing rule:** a powered-off droplet **still bills**. Only *Destroy* stops the meter. Check the billing page
> every day.

## Key dates

| When | What |
|---|---|
| by **Oct 8** | Join the AMD AI Developer Program and request the **$100 credit**. It expires 30 days after deposit, so requesting around Oct 8 covers event week and the start of judging. |
| before **Oct 12 15:00 UTC** (20:30 IST) | **Register on lablab.ai and create the team.** Registration closes at kickoff; pods and credits are per team. |
| Oct 12 15:00 UTC | Kickoff. Tag `amd-baseline` (step 1). The GPU clock starts after this point. Then one command: `editions/amd/kickoff/run_all.sh` (see "Kickoff day" below). |
| **Oct 13 12:00 IST** | Go/no-go on Gemma 4 on vLLM-ROCm. If it will not serve by then, switch to Qwen3.6-35B-A3B (Troubleshooting). |
| Oct 17 | Freeze: final numbers, video footage, `AMD_RESULTS.md`. **Destroy the droplet.** |
| **Oct 18 14:00 UTC** (19:30 IST) | Submission deadline. The dashboard may say 15:00 UTC; use the earlier time. Aim to submit by 17:00 IST. |

## Kickoff day (Mon 12 Oct): one push-button run

Every MI300X hour should produce results, not setup. `editions/amd/kickoff/` turns steps 4 to 11 below into one
resumable command. The numbered steps stay as the manual fallback and explain what each stage does.

| Script | What it does |
|---|---|
| `kickoff/preflight.sh` | Green/amber/red table: amd-smi / rocm-smi and the GPU name, ROCm version, VRAM total and in use, docker + compose ≥ 2.20.2, free disk on the scratch and Docker volumes, `HF_TOKEN` present (value never printed, never on a command line), network to Hugging Face, access to each gated model, uv, the `amd-baseline` tag, the event window, pinned digests, optional inputs (AMLworld CSV, the day's INR rate, droplet start time). Exit 1 if anything is red. |
| `kickoff/run_all.sh` | The pipeline, 11 stages: `env` → `models` → `gnn` → `lora-data` → `lora-train` → `merge` → `lora-eval` → `serve` → `bench` → `ab` → `evidence`. `--dry-run` prints every command; `--stage NAME[,NAME]` re-runs stages; `--from NAME` re-runs from there; `--list`. Each stage logs with UTC timestamps, takes an `amd-smi` snapshot before and after, and writes `stages/NN-<stage>.json` + a done marker into `editions/amd/evidence/<UTC date>/`; a re-run skips what is done. |
| `kickoff/stagelog.py` | The guards and records behind it: the event-window check, the budget guard (prints spend before every stage, warns at 75 %, refuses new stages at 90 % of `WAPSI_BUDGET_USD`, reminds you to destroy the droplet on exit), amd-smi snapshots. |
| `kickoff/collect_evidence.py` | One `evidence.json` + `evidence.md` (dated, with `amd-smi static`) from the stage outputs: tokens/s, TTFT, p95 latency at N concurrent, cases/hour, drafts per GPU-hour, $ and ₹ per case at $1.99 per GPU-hour, VRAM per container, GNN, LoRA, the A/B. `gpu_results_valid` is true only for an MI300X run inside the event window. |
| `kickoff/fill_pitch.py` | Fills the TBD benchmark slots in `pitch/amd/DECK_OUTLINE.md` and `pitch/amd/VIDEO_SCRIPT.md` and adds the matching rows to `pitch/NUMBERS.md` (each re-reads its `evidence.json`; a later bundle replaces only the rows it re-measured). Dry-run diff by default; `--write` applies. Refuses rehearsal or invalid evidence. |
| `kickoff/ab_hallucination.py` | The A/B for deck slide 8: the same model writing a free-form STR vs writing inside Wapsi's slots and verifier, scored by one auditor (wrong amounts, invented accounts, unknown or repealed citations) on the cases both arms completed; the deck compares like with like, raw STRs vs the bound arm's STRs, with all bound drafts alongside. |

**Originality rule.** GPU runs, and every GPU number we report, happen only from **12 Oct 15:00 UTC** (20:30 IST) to
18 Oct 14:00 UTC. `run_all.sh` refuses to start in GPU mode outside that window or without the `amd-baseline` tag, and
`collect_evidence.py` marks a bundle valid only if every stage started inside it. The tag is created **at kickoff, not
before** (step 1 has the command). The CPU rehearsal below is labelled "CPU rehearsal" everywhere, lives in a
gitignored folder, and `fill_pitch.py` refuses it, so no laptop number can reach the pitch.

**Before kickoff (laptop, no GPU, no cost):**
```bash
uv sync --extra amd --extra amd-app --extra ml
editions/amd/kickoff/run_all.sh --rehearsal          # whole pipeline on CPU: tiny worlds, tiny model, stub LLMs
editions/amd/kickoff/run_all.sh --dry-run            # read the exact GPU-day commands once
uv run pytest tests/amd -q
```
On Windows, run the two `run_all.sh` lines from **Git Bash**: `bash editions/amd/kickoff/run_all.sh --rehearsal`. In
PowerShell, `bash` can resolve to WSL's `bash.exe`, which sees neither this checkout's uv venv nor its Windows paths.
The droplet itself is Linux, where the scripts run as they are.

**Hour by hour on 12 Oct** (UTC, IST in brackets; stage times are estimates until measured):

| UTC (IST) | Do this | Notes |
|---|---|---|
| 15:00 (20:30) | `git tag -a amd-baseline -m "State of the repo at AMD ACT III kickoff (2026-10-12 15:00 UTC)" && git push origin amd-baseline`; add the tag's commit and time to `docs/originality.md` | Step 1. Not before 15:00 UTC. |
| 15:05 (20:35) | Create the droplet and firewall (steps 2 and 3). `export WAPSI_DROPLET_STARTED_AT=<creation time, ISO UTC>`, and `export WAPSI_SPENT_USD=<earlier droplets>` if any | Billing starts at creation. |
| 15:15 (20:45) | SSH in, `tmux new -s wapsi`, clone, `export SCRATCH=<5 TB mount>` (it must exist: preflight fails a missing path rather than report the boot disk), `read -rs HF_TOKEN && export HF_TOKEN`, `export WAPSI_INR_PER_USD=<the day's rate>`, optional `export AMLWORLD_CSV=<HI-Small_Trans.csv>`; from the laptop, `scp -r data/docs/raw root@DROPLET_IP:<clone>/data/docs/` (the legal corpus is not in git; otherwise `env` fetches it from the public sites); then `editions/amd/kickoff/preflight.sh` | Fix every red row. Amber rows are allowed. |
| 15:25 (20:55) | `editions/amd/kickoff/run_all.sh` | Starts `env` (~20 min: uv sync, ~120 synthetic worlds, docs index, pinned images, training image and its check: imports, torch ranges and one LoRA step on the GPU, versions into `train_image_check.json`). |
| 15:45 (21:15) | `models` | ~80 GB of weights into `$SCRATCH/hf` (~15–30 min). |
| 16:15 (21:45) | `gnn` | GraphSAGE base + no-external-signals variants on ROCm, held-out and hard-world evaluation (~20–40 min). First GPU numbers: s/epoch, edges/s. |
| 16:45 (22:15) | `lora-data` | Base Gemma 4 31B on vLLM-ROCm. **Go/no-go signal:** if it does not serve, see Troubleshooting (Qwen fallback: `WAPSI_BASE_PROFILE=qwen-fallback WAPSI_BASE_MODEL=Qwen/Qwen3.6-35B-A3B WAPSI_LORA_CONFIG=qwen36-35b-a3b-mi300x WAPSI_MERGED_NAME=wapsi-qwen36-str-merged`, then `--from models`). Base serving bench, then the verifier-filtered dataset (~1–2 h). |
| ~18:30 (00:00) | `lora-train` | 2 epochs, ~1.5–4 h. Unattended overnight is fine; the budget line is printed before every stage. Back up the adapter when it ends. |
| ~22:00 (03:30) | `merge`, `lora-eval` | fp32 merge, bf16 save; base vs tuned on held-out cases (~1–1.5 h). |
| ~23:30 (05:00) | `serve`, `bench`, `ab`, `evidence` | Merged model + verifier + GNN + app via compose; telemetry, serving and end-to-end benchmarks, A/B; `evidence.json` + `evidence.md` (~2 h). |
| Tue 03:30 (09:00) | Read `editions/amd/evidence/<date>/evidence.md`, review `fill_pitch.py` diff, `--write`, `uv run python pitch/check_numbers.py`, commit + push the evidence | Go/no-go on Gemma due 12:00 IST. Destroy the droplet if nothing needs re-running. |

If a stage fails, `run_all.sh` stops and says which log to read; fix it and run `run_all.sh` again (done stages are
skipped) or `run_all.sh --stage <name>`. A failed `lora-eval` removes its two vLLM containers, and `serve` removes them
again before it starts, so port 8000 is free. Measurements do not stop the run: when a benchmark request, an
end-to-end case or an A/B case fails, the result file still records it, the stage notes count it
(`measure_failures`), the stage goes on, and `evidence.md` lists it under "Warnings". A concurrency level or e2e run
with failures never becomes a headline number (re-run `--stage bench`), and the A/B compares both arms only on the
cases both completed. `run_all.sh --stage bench,ab,evidence` repeats the measurements later in the week; each run
writes into that day's evidence folder, and its `evidence` stage captures `amd-smi static` and the commit again, so
the bundle is valid without `env`. `fill_pitch.py --write` merges a later bundle into `pitch/NUMBERS.md`: it replaces
the rows of the values it re-measured and keeps the rest, each row naming its own bundle.

## 0. Before the event (no GPU, no cost)

1. Get a Hugging Face account and **accept the Gemma 4 licence** on the model pages you will use:
   `google/gemma-4-31B-it` (base and LoRA) and `google/gemma-4-E4B-it` (verifier). Create a **read-only token**.
   It stays on your machine and in the droplet's shell only. Never put it in git, `.env`, a screenshot or the video.
2. Upload an SSH key to AMD Developer Cloud. Note your public IP, which the firewall needs.
3. Check the demo on your laptop:
   ```bash
   uv run pytest tests/amd -q
   docker compose -f editions/amd/docker-compose.yml --profile demo up --build   # http://127.0.0.1:8501
   ```
4. Read the other streams' READMEs: `editions/amd/gnn/README.md` (GraphSAGE training, the `gnn` service) and
   `editions/amd/lora/README.md` (Gemma 4 LoRA fine-tune and merge). Prepare their training data locally.
5. Optional: install `doctl` and log in with an API token from the AMD Developer Cloud account, if the console
   issues one, to script the firewall. The console works too.

## 1. Kickoff: record the originality baseline

Run this at 15:00 UTC on 12 Oct, **not before** (the tag marks the state of the repository at kickoff):
```bash
git tag -a amd-baseline -m "State of the repo at AMD ACT III kickoff (2026-10-12 15:00 UTC)"
git push origin amd-baseline
```
Add a line to `docs/originality.md` with the tag's commit and time. Everything after this point in
`editions/amd/gnn`, `editions/amd/lora`, `editions/amd/bench/results` and `AMD_RESULTS.md` is event work.

## 2. Create the droplet

In the AMD Developer Cloud console:

1. Go to **GPU Droplets → Create GPU Droplet**.
2. Choose the **MI300X (1 GPU)** plan. The published spec is 1× MI300X with 192 GB, 20 vCPU, 240 GB RAM, a 720 GB
   NVMe boot disk and a 5 TB scratch disk. Pick the region the console offers.
3. For the image, pick one that **already has Docker and ROCm**, such as a Quick Start image like "vLLM". If you
   pick plain ROCm, install Docker in step 4. Confirm the image names in the console on the day.
4. Add your SSH key, give the droplet the **tag `wapsi`**, and name it `wapsi-mi300x`.
5. Start the budget log with the creation time. Billing starts now.

## 3. Firewall (DigitalOcean Cloud Firewall, not UFW)

Docker's published ports bypass UFW. Protection comes from the **Cloud Firewall** plus our 127.0.0.1 binds.

Console path: **Networking → Firewalls → Create**.
- **Inbound:** TCP 22 from `YOUR_IP/32`. Add TCP 8501 from `YOUR_IP/32` only if you will not use an SSH tunnel.
- **Outbound:** allow all (Hugging Face and registry downloads).
- **Apply to:** tag `wapsi`.

The same with doctl:
```bash
doctl compute firewall create --name wapsi-fw --tag-names wapsi \
  --inbound-rules "protocol:tcp,ports:22,address:YOUR_IP/32 protocol:tcp,ports:8501,address:YOUR_IP/32" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0 protocol:icmp,address:0.0.0.0/0,address:::/0"
```
The preferred way to reach the UI is an SSH tunnel, which needs no open port:
`ssh -L 8501:127.0.0.1:8501 -L 8080:127.0.0.1:8080 root@DROPLET_IP`, then open http://127.0.0.1:8501.

## 4. First login: check the machine

```bash
ssh root@DROPLET_IP
amd-smi static --asic            # one "AMD Instinct MI300X"
amd-smi metric --json | head -50 # CONFIRM the JSON shape (see step 11: telemetry fixtures)
rocm-smi --showmeminfo vram
docker version && docker compose version    # Compose must be >= 2.20.2 ("required: false" in depends_on)
docker ps                                   # stop any pre-installed container that holds the GPU: docker stop <name>
lsblk && df -h                              # find the 5 TB scratch mount; put models and the HF cache there
```
If Docker or Compose is missing or too old: `apt-get update && apt-get install -y docker.io docker-compose-v2`.
Confirm the package names on the image.

Use the scratch disk for weights. The two Gemma 4 31B checkpoints (base and merged) take about 62 GB each in bf16:
```bash
export SCRATCH=/mnt/scratch          # <- the mount point lsblk/df showed
mkdir -p $SCRATCH/hf $SCRATCH/models
```

## 5. Repository, host tools, synthetic world

```bash
git clone https://github.com/<you>/wapsi.git && cd wapsi
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
uv sync --no-dev                     # host-side benches: CPU-only core deps (no torch on the host)
uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
uv run python -m wapsi.search.fetch && uv run python -m wapsi.search.build   # optional: legal corpus index
```

## 6. Secrets and `.env`

```bash
read -rs HF_TOKEN && export HF_TOKEN            # paste the token; not echoed, not in history
export VLLM_API_KEY="$(openssl rand -hex 24)"   # optional: bearer token between app and vLLM
cd editions/amd
cp .env.example .env && chmod 600 .env
sed -i "s#^HF_CACHE_DIR=.*#HF_CACHE_DIR=$SCRATCH/hf#; s#^WAPSI_MODELS_DIR=.*#WAPSI_MODELS_DIR=$SCRATCH/models#" .env
serving/pull_and_pin.sh                          # pulls the images, writes VLLM_IMAGE=...@sha256:... into .env
grep _IMAGE= .env                                # record these digests in AMD_RESULTS.md / the submission
install -d -o 10001 -g 10001 bench/results       # the app container (uid 10001) writes results here
```
To use AMD's day-0 Gemma 4 image instead of the validated one, set `VLLM_IMAGE` to the `VLLM_GEMMA4_IMAGE` value.

## 7. Day-0 smoke test: base Gemma 4 31B

The merged checkpoint does not exist yet, so serve the base model:
```bash
sed -i 's#^WAPSI_LLM_PROFILE=.*#WAPSI_LLM_PROFILE=gemma4-base#; s#^WAPSI_LLM_MODEL=.*#WAPSI_LLM_MODEL=google/gemma-4-31B-it#' .env
serving/start_llm.sh print gemma4-base           # review the exact vllm command line
docker compose up -d llm && docker compose logs -f llm     # first start downloads ~62 GB, then compiles
# wait for "Application startup complete", then from the repo root:
cd ../.. && uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8000/v1
```
The smoke test prints the served model, one completion, and a showcase case through the whole pipeline: drafts,
verification passes and prose fallbacks. Exit codes: 0 ok, 1 endpoint problem, 2 a draft failed verification.
**If Gemma 4 fails, go to Troubleshooting now. The Qwen decision is due Oct 13 12:00 IST.**

Record the base-model serving baseline, which gives the base-vs-tuned comparison and the first numbers:
```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ); OUT=editions/amd/bench/results/$STAMP; mkdir -p $OUT
uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8000/v1 --concurrency 1 4 16 64 \
  --name llm_bench_base --out $OUT
```

## 8. GNN training on ROCm (`editions/amd/gnn/README.md`)

The GNN is small and trains while vLLM runs. Follow the GNN README; the command has this shape:
```bash
uv run python -m editions.amd.gnn.train --worlds ... --val-worlds ... --test-worlds ... \
  --device auto --epochs 50 --out editions/amd/gnn/checkpoints/<name>
```
Run it inside a ROCm PyTorch container, as that README describes; the host has no torch. Copy its `metrics.json`
(epoch time, edges/s, peak memory, val/test metrics) into `$OUT/gnn_metrics.json` so `report.py` picks it up.

## 9. LoRA fine-tune and merge (`editions/amd/lora/README.md`)

A 31B bf16 LoRA run needs most of the GPU, so **stop vLLM first**: `cd editions/amd && docker compose stop llm`.
Then follow the LoRA README:
```bash
python -m editions.amd.lora.train --config gemma4-31b-mi300x --data data/generated/lora-<teacher> --out models/lora/gemma4-31b
python -m editions.amd.lora.merge --run models/lora/gemma4-31b --out $SCRATCH/models/wapsi-gemma-4-31b-str-merged
```
The merge `--out` must be `$WAPSI_MODELS_DIR/wapsi-gemma-4-31b-str-merged`. The llm container sees it as
`/models/wapsi-gemma-4-31b-str-merged`, which is the default `WAPSI_LLM_MODEL`. Copy the LoRA evaluation JSON
(base vs tuned) into `$OUT/lora_eval.json`.

**Back up the adapter now.** It is small; the merged model can always be rebuilt from base + adapter. Push it to a
**private** Hugging Face repo, or `scp -r root@DROPLET_IP:~/wapsi/models/lora/gemma4-31b ./` from your laptop.

## 10. Serve the merged model with the verifier and the GNN

```bash
cd editions/amd
sed -i 's#^WAPSI_LLM_PROFILE=.*#WAPSI_LLM_PROFILE=gemma4-merged#; s#^WAPSI_LLM_MODEL=.*#WAPSI_LLM_MODEL=/models/wapsi-gemma-4-31b-str-merged#' .env
sed -i 's#^COMPOSE_PROFILES=.*#COMPOSE_PROFILES=gpu,verifier,gnn#' .env
sed -i 's#^WAPSI_VERIFIER_BASE_URL=.*#WAPSI_VERIFIER_BASE_URL=http://verifier-llm:8000/v1#; s#^WAPSI_GNN_URL=.*#WAPSI_GNN_URL=http://gnn:8081#' .env
docker compose up -d llm                # vLLM first (0.55 of VRAM) ...
docker compose up -d                    # ... then verifier-llm (0.15), gnn, app (depends_on + healthchecks)
docker compose ps                       # all "healthy"
cd ../.. && uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8000/v1
uv run python -m editions.amd.serving.smoke_test --base-url http://127.0.0.1:8001/v1 --skip-pipeline
curl -s http://127.0.0.1:8080/healthz | python3 -m json.tool      # llm, guard "rules+judge:...", scorer "gnn:..."
curl -s -X POST http://127.0.0.1:8080/cases/31009260000101/process | python3 -m json.tool
```
Open the UI through the SSH tunnel. With an empty store the UI processes the first `WAPSI_UI_SEED_LIMIT` cases of
the world using the real model. The e2e bench (`--store-dir`) or the API fills in more.

## 11. Benchmarks (the numbers for the slides and the video)

Telemetry runs on the **host**; `amd-smi` is not inside the containers. The sampler uses only the standard library.
```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ); OUT=editions/amd/bench/results/$STAMP; mkdir -p $OUT
amd-smi metric --json > $OUT/amd_smi_metric_raw.json; amd-smi process --json > $OUT/amd_smi_process_raw.json
python3 editions/amd/bench/gpu_telemetry.py --once                       # sanity: VRAM per container
python3 editions/amd/bench/gpu_telemetry.py --interval 2 --out $OUT & TPID=$!

# serving: drafting model, then the verifier
uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8000/v1 --concurrency 1 4 16 64 \
  --input-len 1024 --output-len 256 --out $OUT
uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8000/v1 --prompts wapsi \
  --concurrency 1 8 32 --name llm_bench_wapsi_prompts --out $OUT
uv run python -m editions.amd.bench.llm_bench --base-url http://127.0.0.1:8001/v1 --concurrency 1 4 16 \
  --name llm_bench_verifier --out $OUT

# end to end: golden cases through the real pipeline (sweep concurrency; pass the day's INR rate if you quote ₹)
for C in 1 8 16; do
  uv run python -m editions.amd.bench.e2e_bench --world demo --concurrency $C \
    --llm-url http://127.0.0.1:8000/v1 --verifier-url http://127.0.0.1:8001/v1 --gnn-url http://127.0.0.1:8081 \
    --gpu-hour-usd 1.99 --inr-per-usd <RATE_ON_THE_DAY> --name e2e_c$C --out $OUT
done

kill -INT $TPID; wait $TPID                                              # writes telemetry_summary.json
uv run python -m editions.amd.bench.report --results-dir $OUT --out editions/amd/AMD_RESULTS.md
git add editions/amd/bench/results/$STAMP editions/amd/AMD_RESULTS.md && git commit -m "AMD: MI300X benchmark run $STAMP" && git push
```
**Telemetry fixtures:** compare `$OUT/amd_smi_*_raw.json` with `editions/amd/bench/fixtures/*.json`. If the shape
differs, copy the raw captures over the fixtures, update the expected values in `tests/amd/test_amd_bench.py`, and
fix the parser until `uv run pytest tests/amd -k telemetry` passes.

## 12. Video capture (`amd-smi` on camera, about 90 s of the AMD deep-dive)

Use two terminals side by side; keep the HF token and IP addresses off screen.
```bash
watch -n 2 'amd-smi metric --usage --mem-usage --power; python3 editions/amd/bench/gpu_telemetry.py --once | tail -5'
cd editions/amd && docker compose ps     # llm + verifier-llm + gnn + app on one MI300X
```
Then:
1. Run a concurrency-16 `llm_bench` or `e2e_bench` while recording, so GFX activity and power move.
2. Show `AMD_RESULTS.md`: the serving table, cases/hour, $ per case and VRAM per container.
3. Show the GNN training curve and the LoRA base-vs-tuned table from their results files.
4. Optional bonus slide: try `rocm examine` from the ROCm CLI once. It is a tech preview; keep it out of the
   critical path.

## 13. Teardown (every time the droplet will sit idle for more than ~3 h, and at the end)

1. `git push` the results, and copy anything else off the box: adapter, GNN checkpoint, raw captures.
2. Optional, for the demo URL: `docker compose cp app:/state/cases ./mi300x-cases` keeps the real-model case
   bundles. The data is synthetic, so these are safe to publish.
3. `cd editions/amd && docker compose down`.
4. **Console: Destroy the droplet.** Powering off is not enough.
5. Check the billing page: no active GPU droplet. Update the budget log.

**Demo URL during judging.** An MI300X costs about $48 a day and cannot stay up through judging. Deploy the
**demo mode** (`app-demo`: CPU, `FakeLLM`, no GPU) on any small host, optionally preloaded with the MI300X case
bundles from step 13.2. Say plainly in the submission that the live URL is demo mode and that the video and
`AMD_RESULTS.md` show the MI300X run.

## Budget (≤ $100 at $1.99 per GPU-hour)

| Day (IST) | GPU work | Planned hours | Cost |
|---|---|---|---|
| Mon Oct 12 20:30 to Tue 09:00 | `kickoff/run_all.sh` end to end (env → evidence), unattended overnight | 12.5 | $24.88 |
| Tue Oct 13 | Review `evidence.md`; Gemma/Qwen go/no-go by 12:00; re-run failed stages (`--stage`); destroy by evening | 6 | $11.94 |
| Wed Oct 14 | Fixes and a second `--stage serve,bench,ab,evidence` run (new droplet only if needed) | 6 | $11.94 |
| Thu Oct 15 | Video footage (step 12), final benchmarks; destroy | 5 | $9.95 |
| Fri Oct 16 | Buffer re-runs and extra footage; destroy by evening | 4 | $7.96 |
| Sat Oct 17 (freeze) | Final capture only if needed; **destroy** | 2 | $3.98 |
| Sun Oct 18 | No GPU; submit by 17:00 IST | 0 | $0.00 |
| **Total planned** | | **35.5** | **$70.65** |
| Buffer | re-downloads, reruns, mistakes | 14.5 | $28.86 |
| **Cap** | | **50** | **$99.50** |

`run_all.sh` prints this arithmetic before every stage (`stagelog.py budget`: hours since `WAPSI_DROPLET_STARTED_AT`
× `WAPSI_GPU_HOUR_USD` + `WAPSI_SPENT_USD` from earlier droplets), warns at 75 % of `WAPSI_BUDGET_USD` and refuses to
start a stage at 90 % ($90 of $100) unless you pass `--ignore-budget`.

- Each re-creation of the droplet costs about 30–45 min of billed setup (pull images, download ~62 GB). Destroy it
  when it will sit idle for more than about 3 h; keep it up for long unattended runs, such as LoRA overnight.
- Keep a running log of creation and destroy times. Stop new GPU work when the log reaches $90.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Gemma 4 errors about attention, sliding window or head size on ROCm | Gemma 4 needs `--attention-backend TRITON_ATTN`, which `start_llm.sh` adds for Gemma profiles. Check with `serving/start_llm.sh print`. If this vLLM build has no such flag, set `VLLM_ATTENTION_FLAG=0` (the backend then comes from the `VLLM_ATTENTION_BACKEND` env var). Try the day-0 image: `VLLM_IMAGE=<VLLM_GEMMA4_IMAGE digest>`. |
| Gemma 4 still will not serve by **Oct 13 12:00 IST** | Switch to the fallback: `WAPSI_LLM_PROFILE=qwen-fallback`, `WAPSI_LLM_MODEL=Qwen/Qwen3.6-35B-A3B` (day-0 ROCm; thinking off by default). Tell the LoRA stream to retarget Qwen, or serve base Qwen and say so. The verifier can stay Gemma 4 E4B. |
| Out of memory at startup, or "not enough KV cache" | Start `llm` first, then the others. Lower `VLLM_MAX_MODEL_LEN` (e.g. 8192) or `VLLM_MAX_NUM_SEQS`. Use `VLLM_KV_CACHE_DTYPE=fp8` and/or `VLLM_QUANTIZATION=fp8`. Lower `VERIFIER_GPU_UTIL`. Keep the sum of utilizations ≤ ~0.85 so the GNN has room. |
| Second vLLM refuses to start: free memory below requested | The first instance took its share. Start order must be llm → verifier; lower `VERIFIER_GPU_UTIL`. |
| `401/403`, gated repo, while downloading | Accept the licence on the Hugging Face model page, then `export HF_TOKEN` in the shell that runs `docker compose`. |
| Healthcheck never turns healthy | `docker compose logs -f llm`. The first start downloads and compiles; `start_period` is 30 min. |
| `/dev/kfd` permission denied (non-root containers such as `gnn`) | Needs the `video` and `render` groups (compose sets both for `gnn`). Check `ls -l /dev/kfd /dev/dri/*`. |
| Merged checkpoint reserves image/audio encoder memory | Set `VLLM_LIMIT_MM='{"image": 0, "audio": 0}'`. |
| High prose fallback rate in the smoke test or e2e | Look at `failed_checks` and `llm_issues` in the case JSON. Try `WAPSI_LLM_JSON_MODE=json_object` (for guided-decoding problems). A fallback is safe (the fixed paragraph passes the verifier) but should be rare with the tuned model. |
| Compose rejects `required:` in `depends_on` | Docker Compose older than 2.20.2; upgrade the compose plugin. |
| App cannot write `bench/results` | `install -d -o 10001 -g 10001 editions/amd/bench/results` (the container runs as uid 10001). |
| `amd-smi` JSON differs from the fixtures | Capture it (step 11), update the fixtures and tests, then fix `bench/gpu_telemetry.py`. The rocm-smi fallback keeps sampling in the meantime. |
| A port answers from the internet | Docker bypasses UFW. Keep every `*_BIND=127.0.0.1` and rely on the Cloud Firewall; use the SSH tunnel. |
| Disk full | Point `HF_CACHE_DIR` and `WAPSI_MODELS_DIR` at the scratch disk; `docker system prune` removes old images. |
| Training image: the build's import line or `train_image_check.py` fails (env stage) | The image keeps its ROCm torch (2.9.1) and installs the uv.lock pins of transformers / peft / trl / accelerate / datasets with `--no-deps`; their declared torch floors accept 2.9.1 (tested offline), so a failure is an API break. Read `train_image_check.json` (which import or the LoRA step failed). Then either use a newer ROCm PyTorch image (`export ROCM_PYTORCH_IMAGE=rocm/pytorch:<newer tag>` and `run_all.sh --stage env`), or pin the failing library to an older release in `requirements-rocm.txt` under `$SCRATCH/wapsi-work/train-image/` and re-run the `docker build` line from the env log. |
| `e2e_bench` runs without docs search (`docs_index=missing` in the env stage notes) | `data/docs/raw` is not in git and the public sites did not answer. From the laptop: `scp -r data/docs/raw root@DROPLET_IP:<clone>/data/docs/`, then on the droplet `uv run python -m wapsi.search.build` and `run_all.sh --stage bench`. |
