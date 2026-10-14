# Wapsi GNN: edge-aware GraphSAGE mule scorer (AMD edition)

A graph neural network that plugs into `wapsi.classify` as a `Scorer`, written in **pure PyTorch** (no
torch_geometric or DGL, because PyG has no supported ROCm path). Message passing only uses `index_add` and
`scatter_reduce(amax)`, both standard ATen ops with HIP kernels, so the same code runs on CPU, CUDA and AMD Instinct
MI300X through the `torch.cuda` API (`--device auto|cpu|cuda`). There are no custom kernels and no CUDA-only ops.

> **Originality note (lablab.ai AMD ACT III).** This code was **prepared before the event**, on 2026-09-23. It
> exists at kickoff, will be covered by the `amd-baseline` tag, and must be disclosed as pre-existing in
> `docs/originality.md`, which currently dates all AMD-edition work to 12–18 Oct. The **MI300X training runs, the
> checkpoints they produce, and every reported GPU number (time, throughput, memory, metrics) are produced during
> 12–18 Oct 2026.** All results in `results/cpu-dev-*.json` are CPU development numbers from a laptop. They are not
> MI300X results. AMLworld pre-training (`amlworld.py`) is event-time only: nothing was downloaded or run on real
> AMLworld data before the event.

## Files

| File | What |
|---|---|
| `graph.py` | Case subgraphs (`build_local_graph`, `graph_for_trace`), a vectorised whole-window graph (`build_world_graph`), features, cache |
| `model.py` | `EdgeSageLayer` and `WapsiGnn` (with an optional temporal variant), `EdgeHead`, device helpers, safetensors checkpoints |
| `train.py` | Training CLI: train / val / test worlds, early stopping, `metrics.json` |
| `scorer.py` | `GnnScorer`, which implements the `wapsi.classify` `Scorer` protocol, with Integrated-Gradients contributions |
| `service.py` | FastAPI app: `POST /score`, `GET /healthz` (compose service `gnn`, port 8081) |
| `evaluate.py` | heuristic-v2 vs XGBoost vs GNN through `process_case`, written to `results/cpu-dev-<date>.json` |
| `amlworld.py` | **Event-time**: IBM AMLworld HI-Small loader and edge-level pre-training, then transfer |
| `Dockerfile` | ROCm PyTorch base (`rocm/pytorch:<pinned tag>`), CPU fallback build arg, non-root |

## Design

**Graph.** Each graph is a case subgraph. Its nodes are the accounts of one money trail (the victim accounts plus
every account that received tainted money: the "seeds", exactly `trace.accounts`), plus up to 10 context
counterparties per seed (the top 10 by rupees exchanged in the window) and up to 5 device-sharing peers. The edges
are every visible transaction between two nodes in `[as_of − 30 d, window_end]`. It is a directed multigraph with one
edge per transaction, plus device-link edges.

- **Why case subgraphs and not neighbour sampling on the 2M-edge world graph.** The product only ever labels the
  accounts of one trail. The classify features are anchored per case: `as_of` is the first reported victim transfer,
  and the burst window starts at each account's first tainted receipt. Case subgraphs are small (tens to a few
  hundred nodes), so a training batch is a disjoint union of case subgraphs, trained full-batch. Neighbour sampling
  is still applied where it matters: a hub (merchant, pool) keeps only its top-k counterparties. Cost is linear in
  the number of cases, not in world size. `build_world_graph` also builds the whole 2M-transaction `full` world in
  13.6 s (vectorised Arrow → pandas → torch) for global use and pre-training.
- **Visibility.** Home-bank transactions come from `transactions_touching`. Other-bank activity comes only from
  `consortium_transactions`, and only for trail accounts, which are the accounts the bank really asks CFCFRMS
  about. Other-bank context accounts are never queried: their features stay zero, with `g_features_observed = 0`.
- **Node features.** The 33 `wapsi.classify.features.account_features` columns, reused from the core and anchored
  exactly like `classify_trace` (tests assert that the scorer's graph is identical to the training graph). On top
  of those come 11 degree statistics (`g_*`). Heavy-tailed columns get `log1p`. Standardisation statistics are
  stored in the checkpoint.
- **Edge features.** log amount, channel one-hot, Δt since the sender's previous credit (same-second credits count
  first), IST hour sin/cos, cross-bank, round amount, device link, signed log time relative to the anchor, and an
  after-anchor flag.
- **Labels.** From `labels_accounts.role_truth`: MULE = 1; INNOCENT_HOLDER, LEGIT, MERCHANT and INTERMEDIARY = 0;
  VICTIM and UNKNOWN are masked. The loss is BCE on trail accounts, class-balanced, plus context accounts at weight
  0.3.

**Model.** Two GraphSAGE layers with edge features in the messages:
`m_{u→v} = ReLU(W_in [h_u ‖ e_uv])` and `m_{v→w} = ReLU(W_out [h_w ‖ e_vw])`. Each is aggregated with **mean and max,
separately over in- and out-neighbours**. The update is `h' = LayerNorm(h + Dropout(ReLU(W [h ‖ 4 aggregates])))`
(residual). The head is an MLP on `[h_L ‖ h_0]`, giving one logit. Hidden size is 64 (78k parameters). `--temporal`
adds a learnable Time2Vec encoding of each edge's time to the edge features.

**How graph context enters the Scorer protocol.** `Scorer.score(features_df)` only receives the per-account feature
frame, so `GnnScorer` holds a ledger reference and builds the case subgraph itself. The frame's rows become the
seeds (their features are used exactly as given), and context comes from the ledger. The time anchor is resolved in
this order:

1. An explicit `as_of` (service).
2. A bound trace (`GnnScorer.for_trace(path, ledger, trace)`).
3. The complaints whose victim accounts are among the rows. This reproduces `trace.window_start` and `window_end`
   exactly, and is what makes plain `classify_trace(ledger, trace, cfg, model=GnnScorer.load(path, ledger=ledger))`
   work.

Without a ledger, each row is scored as an isolated node and `warnings` says so.

**Contributions** (these keep `wapsi.classify.explain` reasons working). Integrated Gradients (32 steps) over each
account's own standardised features, with the rest of the graph held fixed. The values are log-odds and map 1:1 to
the `FEATURE_COLUMNS` names. Three extra columns make the decomposition exactly additive:

- `bias`: an isolated account with mean features.
- `graph_neighbourhood`: what the account's position in the transaction graph adds.
- `ig_residual`: the integration error. Tests check it is ≤ 5% of the feature attributions.

`explain` ignores columns it does not know.

## Trained on an AMD Instinct MI300X (AMD AI Notebook, 2026-10-14)

`run_all.sh --mode notebook --stage gnn` on one AMD Instinct MI300X (an SR-IOV virtual function in an AMD AI
Notebook, torch 2.11.0+rocm7.14.1; not the droplet): hidden 128, 3 layers, 410,017 parameters, 13 epochs in 8.6 s
(0.055 s per epoch after the first), peak memory 0.39 GB. On the hard world (64 cases) it gets dispositions 0.9198
(the heuristic's too), innocent-holder recall 0.875 (heuristic 0.6875) and mule ROC-AUC 0.9202 (heuristic 0.9411),
and sends 0.185 of accounts to the officer as UNKNOWN (heuristic 0.035); holds stay exact and false freezes 0, so the
heuristic stays the default. Files: `editions/amd/evidence/notebook/2026-10-14/gnn_metrics.json`, `gnn_eval.json`,
`gnn_hard_eval.json` (and `SESSIONS.md` there).

## CPU dev results (2026-09-23, NOT MI300X)

These come from `results/cpu-dev-2026-09-23.json`: Windows laptop, CPU only, 4 threads, torch 2.14 CPU. The GNN
was trained on `demo` plus `gnn-1001…1010` (447 cases), early-stopped on `gnn-2001/2002`, and scored once on the
test worlds. `xgb-same-train` is XGBoost retrained on exactly the same worlds. `xgb-demo` is the core's artefact,
trained on `demo` only. The metrics come from `process_case` over the golden cases (harness definitions).

Test worlds `heldout-404` and `heldout-505` (81 cases, 213 mule and 14 innocent-holder trail accounts, 248
dispositions):

| scorer | mule P | mule R | mule F1 | innocent P | innocent R | disposition acc | sent to officer |
|---|---|---|---|---|---|---|---|
| heuristic-v2 | 0.995 | 1.000 | 0.998 | 1.000 | 0.857 | 0.992 | 1 |
| xgb-demo | 1.000 | 1.000 | 1.000 | 1.000 | 0.929 | 0.996 | 1 |
| xgb-same-train | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0 |
| **gnn** | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0 |
| heuristic-v2, external signals zeroed | 1.000 | 0.995 | 0.998 | 1.000 | 0.857 | 0.992 | 3 |
| xgb-same-train, no external signals | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0 |
| gnn, no external signals | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0 |

The secondary held-out worlds `heldout-101/202/303` (121 cases) show the same picture. There, the GNN and
xgb-same-train are at 1.000 everywhere; heuristic-v2 has innocent-holder recall 0.875 and disposition accuracy
0.992.

**Honest reading.**

- The GNN beats heuristic-v2 on innocent-holder recall and disposition accuracy, but it only **ties** XGBoost trained
  on the same worlds. Every trained model is at the ceiling.
- The current synthetic worlds are nearly separable from node features alone:
  - single features reach 0.986 AUC on test trail accounts;
  - only 35 of 248 labelled test trail accounts are negatives;
  - a structure-only GNN (all 33 account features zeroed, degree statistics and edges only) also reaches test AUC 1.0.
- So this benchmark cannot show a graph advantage. This is **not** evidence that graph context adds nothing. It is
  evidence that the benchmark is saturated. Harder worlds are requested from the synth owner.
- No leakage was found. Labels are only read for training and the metrics, and the trail comes from `trace_case`
  as in production.
- **Cost:** GNN classify time is ~0.35 s per case on CPU (subgraph build and IG), against ~0.15 s for heuristic-v2.
- **Training:** 10 s on CPU (early stop at epoch 4 of 14), 45–80k edges/s, 0.46 GB peak RSS.

## Train on CPU now

```bash
# worlds (1.2 s each); held-out seeds 101..505 already exist
for s in $(seq 1001 1010) 2001 2002; do
  uv run python -m wapsi.synth --size demo --seed $s --name gnn-$s --out data/generated/gnn-$s --golden-dir ""
done
uv run python -m editions.amd.gnn.train \
  --worlds data/generated/demo $(printf 'data/generated/gnn-%s ' $(seq 1001 1010)) \
  --val-worlds data/generated/gnn-2001 data/generated/gnn-2002 \
  --test-worlds data/generated/heldout-404 data/generated/heldout-505 \
  --device auto --epochs 50 --workers 6 --out editions/amd/gnn/checkpoints/cpu-dev-base
#   graph build ≈ 20 s/world/process (cached in data/generated/gnn-cache), training ≈ 10 s
uv run python -m editions.amd.gnn.train ... --drop-features fri_risk mulehunter_score ncrp_prior_reports \
  --out editions/amd/gnn/checkpoints/cpu-dev-noext
uv run python -m editions.amd.gnn.evaluate --gnn editions/amd/gnn/checkpoints/cpu-dev-base \
  --gnn-ablated editions/amd/gnn/checkpoints/cpu-dev-noext --ablate-external \
  --secondary-worlds data/generated/heldout-101 data/generated/heldout-202 data/generated/heldout-303   # ≈ 6 min
uv run pytest tests/amd -q -k gnn                                                                         # < 60 s
```

On CPU, torch is capped at 4 threads. On a busy 22-core Windows machine, 16 threads made steps on these tiny graphs
about 1000× slower.

## Train on the MI300X at the event (12–18 Oct)

Inside the ROCm PyTorch container, do not `uv sync`: it would replace the HIP build of torch with a PyPI wheel. Use
the image's torch instead:

```bash
docker run -it --device /dev/kfd --device /dev/dri --group-add video --group-add render --ipc=host \
  -v $PWD:/work -w /work rocm/pytorch:<pinned tag> bash
pip install pydantic pandas pyarrow scikit-learn xgboost fastapi uvicorn safetensors psutil httpx
export PYTHONPATH=src:.
python -c "import torch; print(torch.cuda.is_available(), torch.version.hip, torch.cuda.get_device_name(0))"

# 1. more training worlds (CPU, parallel): ~2 min for 100 worlds
seq 3001 3100 | xargs -P 32 -I{} python -m wapsi.synth --size demo --seed {} --name gnn-{} --out data/generated/gnn-{} --golden-dir ""

# 2. case-level GNN on full (2M txns, 268 cases) + 111 demo-size worlds (~4.8k cases)
python -m editions.amd.gnn.train \
  --worlds data/generated/full data/generated/demo $(printf 'data/generated/gnn-%s ' $(seq 1001 1010) $(seq 3001 3100)) \
  --val-worlds data/generated/gnn-2001 data/generated/gnn-2002 \
  --test-worlds data/generated/heldout-404 data/generated/heldout-505 \
  --device cuda --epochs 100 --batch-size 256 --hidden 128 --layers 3 --workers 32 \
  --out editions/amd/gnn/checkpoints/mi300x-base
#   variants: --temporal ; --drop-features fri_risk mulehunter_score ncrp_prior_reports (-> mi300x-noext)

# 3. AMLworld HI-Small (downloaded at the event, Kaggle ealtman2019, CDLA-Sharing-1.0): edge pre-training + fine-tune
python -m editions.amd.gnn.train <same worlds as step 2> --device cuda --hidden 128 --layers 3 \
  --amlworld /data/amlworld/HI-Small_Trans.csv --amlworld-epochs 50 --out editions/amd/gnn/checkpoints/mi300x-aml

# 4. comparison on held-out worlds, labelled as an MI300X run
python -m editions.amd.gnn.evaluate --gnn editions/amd/gnn/checkpoints/mi300x-base \
  --gnn-ablated editions/amd/gnn/checkpoints/mi300x-noext --ablate-external --device cuda \
  --secondary-worlds data/generated/heldout-101 data/generated/heldout-202 data/generated/heldout-303 \
  --label "MI300X run (AMD Developer Cloud), $(date -I)" --out editions/amd/gnn/results/mi300x-$(date -I).json
```

**Expected time and memory.** These are estimates, to be replaced by the measured `metrics.json` values.

- **Graph build (CPU-bound, step 2):** 0.67 s per case measured on `full`, and 0.45 s on demo worlds. With 32
  workers this takes about 3–4 min. The long pole is `full`, which is built in one worker (≈ 3 min).
- **Case-level training:** ~85k nodes and ~250k edges per epoch. Measured throughput is ≈ 60k edges/s on 4 CPU
  threads. On the MI300X an epoch should take ≈ 1–2 s, dominated by Python and collation, so 100 epochs is under
  5 min, with peak HBM well under 5 GB. The GPU is not the bottleneck for this model.
- **AMLworld HI-Small (the GPU-heavy part):**
  - Size: ~5M edges and ~0.5M accounts.
  - Memory: a full-batch step on the 1.7M-edge `full` graph took 11.8 s on 4 CPU threads. Activations kept for
    backward are about E × (2·hidden + edge_hidden + hidden) × 4 bytes per layer and direction. That gives roughly:
    - HI-Small at hidden 64 and 2 layers: ≈ 20 GB;
    - HI-Small at hidden 128 and 3 layers: ≈ 50 GB;
    - HI-Medium (~32M edges) at hidden 64 and 2 layers: ≈ 115 GB. This fits the MI300X's 192 GB, but not full-batch
      on an 80 GB GPU.
  - Time: expect a few seconds per epoch, so 50 epochs is under 10 min.
  - Record `peak_mem_mb` (from `torch.cuda.max_memory_allocated`) and `edges_per_s`, which are logged every epoch.

## Plug into the pipeline

```python
from editions.amd.gnn.scorer import GnnScorer

scorer = GnnScorer.load("editions/amd/gnn/checkpoints/<name>", ledger=ledger, device="auto")
classify_trace(ledger, trace, ClassifyConfig(), model=scorer)
process_case(ledger, ncrp_ids, llm=llm, scorer=scorer, draft=True)
```

Over HTTP (the app's `editions/amd/app/gnn_client.py` → `http://gnn:8081`), `POST /score` accepts three shapes:

- the `classify_trace` feature frame `{"account_ids", "features": [{"account_id", ...}], "feature_columns"}`;
- `{"ncrp_ids": [...]}`: the service traces the case and scores it;
- `{"account_ids": [...], "as_of": "..."}`.

It answers:

```json
{"model": "gnn:<name>", "scores": {"<id>": p}, "contributions": {"<id>": {"<feature>": v, "bias": b}},
 "graph": {"<id>": {"graph_neighbourhood": …, "degree_stats": …, "ig_residual": …, "isolated_bias": …}},
 "anchor": {"as_of": …, "end": …}, "warnings": []}
```

`contributions` over `FEATURE_COLUMNS` plus `bias` sums to the logit, because the graph terms are folded into
`bias`. Environment variables:

- `WAPSI_GNN_CHECKPOINT` or `WAPSI_GNN_ARTIFACTS`: a checkpoint directory, or a directory of checkpoints (the newest
  is used, or the one named by `WAPSI_GNN_MODEL`).
- `WAPSI_GNN_WORLD` or `WAPSI_WORLD`.
- `WAPSI_GNN_DEVICE`, `WAPSI_GNN_THREADS`, `PORT`.

```bash
docker build -f editions/amd/gnn/Dockerfile -t wapsi-amd-gnn:rocm .                                  # ROCm
docker build -f editions/amd/gnn/Dockerfile --build-arg BASE_IMAGE=python:3.12-slim --build-arg TORCH_CPU=1 -t wapsi-amd-gnn:cpu .
```

The container runs as uid 10001. On ROCm hosts, add the `video` and `render` groups (compose: `group_add`) and the
`/dev/kfd` and `/dev/dri` devices. Re-pin `BASE_IMAGE` (tag and digest) at kickoff.

## Checkpoints

A checkpoint is a directory with `model.safetensors` (weights plus normalisation buffers), `config.json` (model,
feature columns, graph and classify config, worlds, device) and `metrics.json`. There is no pickle, and loads are
weights-only safe. `checkpoints/` is git-ignored: checkpoints are produced by runs, and the reportable ones are
produced on the MI300X during the event.
