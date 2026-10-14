# Hosting the public demo (the "demo application URL")

The hackathon submission needs a public URL that stays up through judging. This page gets you one for free, with
no credit card, in three ways. All of them serve the same thing: the shared officer app (`ui/app.py`) started by
`deploy/demo/app.py`, with the two showcase cases and the synthetic demo world. All data is synthetic.

| | Option | Card needed | Cold start | Best for |
|---|---|---|---|---|
| a | **Hugging Face Spaces** (Docker) | no | wakes on a visit after a long idle time | the judged URL (recommended) |
| b | **Streamlit Community Cloud** | no | a visitor must click "wake up" after idle time | a second, independent URL |
| c | **Cloud Run** | yes (free trial) | none with one warm instance | if you already have the Google Cloud project |

> **What was verified, and what was not (2026-10-14).** The image was built locally both from the repository
> (`deploy/demo/Dockerfile`) and from the folder `scripts/make_space.py` assembles, which is what a Space builds.
> The build baked the demo world and the docs search index (561 chunks from the five public sources, 32 citation
> ids; raw files and full texts removed). The container ran as user 1000 in recorded mode (health check, banner,
> "Recorded run" chip, 16 of 16 showcase drafts verified) and in live mode against the AMD Token Factory (no
> banner, live chip naming the model and `developer.amd.com.cn`, 16 of 16 drafts verified). The Space builder,
> the launchers and the Community Cloud requirements are covered by tests. Nothing was pushed to Hugging Face,
> Streamlit or Google Cloud: those steps need your accounts, so the first real run there is yours.

## What a visitor sees

- **Live model.** With the three endpoint variables set, prose is written by that OpenAI-compatible endpoint and a
  chip on the Drafts page and in the sidebar names it: **"Gemma 4 on AMD via developer.amd.com.cn"** (the model
  family, where it runs, the endpoint host). The chip is read from the drafts themselves: each draft records who
  wrote its prose, and the audit chain seals that name with the draft.
- **Recorded run.** Without them every page carries the banner **"Recorded run — live AMD endpoint not
  configured"**, and the prose is replayed from `data/recordings/demo_prose.json`: answers an open model served on
  AMD really gave for these cases, recorded once. The chip then reads "Recorded run · <model> on AMD via <host>".
  Cases with no recorded answer use the offline stub, and the chip says so.
- **Either way** every amount, account number, UTR and date is a slot filled from the ledger, and the verifier
  recomputes them on the spot. The model only ever writes words around placeholders, so a replay cannot show a
  stale number: a recording is keyed by the case's own figures and is not used when they differ.
- **If the live endpoint fails** (rate limit, outage, or an HTTP 200 with garbage) the recorded run answers that
  draft, the chip shows the mix ("2 live, 1 recorded"), and the endpoint is left alone for a minute. A page never
  waits more than a few seconds for the rate limit.
- **Each visitor has their own copy.** Cases are prepared once per server start and every browser session works on
  its own copy, so one judge's approvals never reach the next. "Start over" in the sidebar resets a session.

### The endpoint variables

| Variable | Value | Where it goes |
|---|---|---|
| `WAPSI_LLM_BASE_URL` | `https://developer.amd.com.cn/radeon/api/v1` (AMD Token Factory) or your own vLLM URL | variable |
| `WAPSI_LLM_API_KEY` | your key | **secret**, never a file in the repository |
| `WAPSI_LLM_MODEL` | `Gemma-4-31B-IT` | variable |
| `WAPSI_LLM_JSON_MODE` | optional: `json_object` (default), `json_schema`, or `none` (send no `response_format`) | variable |
| `WAPSI_LLM_EXTRA_BODY` | optional: a JSON object merged into every request | variable |
| `WAPSI_LLM_MODELS` | recommended on a shared endpoint: `Gemma-4-31B-IT,DeepSeek-V4-Flash`, the models to fail over along, in order, while one is withdrawn or answers in garbage. Each draft's chip names the model that wrote it | variable |
| `WAPSI_LLM_PLATFORM` | optional: `AMD` for your own vLLM on AMD hardware. `*.amd.com(.cn)` hosts are recognised; any other host is shown without a platform unless you state it | variable |
| `WAPSI_LLM_RPM` | optional: requests a minute the app may send (default 12; the Token Factory allows 20 per account) | variable |
| `WAPSI_UI_LLM` | optional: `auto` (default in the demo), `recorded` (never call the endpoint), `live` (never replay) | variable |

> **Endpoint status on 2026-10-14 (measured, AMD Token Factory).** `Gemma-4-31B-IT` answered every request, even
> "say hello", with HTTP 200 and degenerate output (a run of `/` up to `max_tokens`), with and without
> `response_format`, at temperature 0 and at the default. Wapsi now treats such an answer as a failure: it is never
> cached, recorded or shown. `Qwen3.8-27B` on the same endpoint worked with
> `WAPSI_LLM_JSON_MODE=none` and `WAPSI_LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}`
> (5 showcase prose fields, 7 calls, median 9.8 s per call, 34,422 prompt and 1,215 completion tokens, endpoint-reported
> cost $0.000954), so the bundled recording is from that model and is labelled with it. Before you publish the URL,
> check the model you configure (see "Check the live endpoint") and record again with Gemma 4 once it answers.
> Set `WAPSI_LLM_MODELS` too: while the first model answers in garbage, the next one writes the prose live, and the
> recorded run is the last resort.

## a. Hugging Face Spaces (Docker Space)

A Docker Space builds the `Dockerfile` at the root of its own repository, runs it as user 1000 and routes to the
port named in the README's YAML block. `scripts/make_space.py` assembles that repository from this one.

1. **Build the folder** (from the repository root):
   ```bash
   uv run python scripts/make_space.py --out ../wapsi-space --repo https://github.com/<you>/wapsi
   ```
   It copies exactly what `deploy/demo/Dockerfile` reads (its `COPY` lines are the list; only files tracked by git),
   and writes `Dockerfile`, `README.md` (the Space card with `sdk: docker` and `app_port: 7860`) and
   `.gitattributes` (Git LFS rules for the voice samples). It refuses to finish if a file looks like it holds a key.
   `--check` verifies without writing; `--force` rebuilds an existing folder and keeps its `.git`.
2. **Create the Space** at [huggingface.co/new-space](https://huggingface.co/new-space): SDK **Docker**, template
   **Blank**, hardware **CPU basic** (free), visibility **Public**.
3. **Upload the folder.** Either with the Hub CLI, which handles the binary files for you:
   ```bash
   uv tool run --from huggingface_hub hf auth login          # you paste your own write token
   uv tool run --from huggingface_hub hf upload <user>/<space> ../wapsi-space . --repo-type space
   ```
   (older releases of the Hub library name the same commands `huggingface-cli login` and
   `huggingface-cli upload`), or with git: `git clone https://huggingface.co/spaces/<user>/<space>`, then
   `uv run python scripts/make_space.py --out <that clone> --force`, then
   `git lfs install && git add -A && git commit -m "Wapsi demo" && git push`.
4. **Configure the endpoint** in the Space: Settings → Variables and secrets. Add `WAPSI_LLM_API_KEY` as a
   **secret** and `WAPSI_LLM_BASE_URL`, `WAPSI_LLM_MODEL` as variables. Skip this step to run the recorded run.
5. **Open it.** The build takes a few minutes the first time. The judged URL is
   `https://<user>-<space>.hf.space/?page=case-room&case=CASE-31009260000101` (the direct URL, not the
   `huggingface.co/spaces/...` page that frames it).

Notes:

- The image builds the docs search index itself from the public legal sources (the documents carry no open
  licence, so their text is never committed and never pushed to a public Space). If those sites are unreachable at
  build time the build still succeeds and the app serves the citations-only corpus. `--with-docs-index` copies a
  locally built index into the folder: use it for a **private** Space only.
- A Space on free hardware goes to sleep after a long idle period and restarts on the next visit (about a minute;
  the disk is reset, the image is not rebuilt). Open the URL shortly before each judging session.
- The container turns Streamlit's XSRF and CORS checks off, because Spaces show the app in a frame on another
  origin, where uploads (the Voice Intake page) are otherwise refused. The demo holds synthetic data and has no
  login.

## b. Streamlit Community Cloud

1. Push this repository to a public GitHub repository (the submission needs one anyway).
2. At [share.streamlit.io](https://share.streamlit.io): **Create app** → your repository and branch →
   **Main file path** `deploy/streamlit_cloud/streamlit_app.py` → Advanced settings → Python 3.12.
3. In the same dialog (or later under Settings → Secrets) paste, to call the model live:
   ```toml
   WAPSI_LLM_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
   WAPSI_LLM_API_KEY = "<your key>"
   WAPSI_LLM_MODEL = "Gemma-4-31B-IT"
   WAPSI_LLM_MODELS = "Gemma-4-31B-IT,DeepSeek-V4-Flash"
   ```
   Top-level secrets are also set as environment variables, which is where the app reads them.
4. Deploy. Judged URL: `https://<app>.streamlit.app/?page=case-room&case=CASE-31009260000101`.

Why the entry point is not at the repository root: Community Cloud installs the first dependency file it finds,
looking in the entry point's folder first and preferring `uv.lock` over `requirements.txt`. From the root it would
take `uv.lock` and install the project without the `ui` extra (no plotly). The folder
`deploy/streamlit_cloud/` holds a `requirements.txt` and no lock file. Regenerate it after any dependency change
(a test compares it with `uv.lock`):

```bash
uv export --frozen --no-dev --extra ui --extra docs --no-hashes --no-emit-project --format requirements-txt -o deploy/streamlit_cloud/requirements.txt
```

Limits to plan for (check the current figures in Streamlit's documentation):

- **Memory.** Apps share a small host; one that exceeds its memory limit is restarted. The requirements leave out
  PyTorch and every cloud SDK, and the entry point processes 8 demo-world cases at start-up instead of 12
  (`WAPSI_UI_MAX_CASES`; set `WAPSI_DEMO_BUILD_WORLD=0` in the secrets to serve the showcase cases only).
- **Sleep.** An app without visitors is put to sleep, and the next visitor has to click a button to wake it. That
  is why option a is the URL to submit.
- **Docs index.** There is no build step here, so the app serves the citations-only corpus: every citation id and
  quote is there, full-text clause search is weaker.
- The synthetic demo world is generated at the first start (a few seconds) into the app's working copy.

## c. Cloud Run

Cloud Run needs a billing account (the free trial asks for a card). Two routes:

- **The Google Cloud edition, one command.** `editions/gcp/DEPLOY.md`: `deploy.ps1 -ProjectId <PROJECT_ID>` /
  `deploy.sh --project-id <PROJECT_ID>` builds and deploys that edition (ADK agents, Firestore, BigQuery, Gemini)
  with one warm instance. Its image carries the docs search index, the showcase cases are seeded once under a
  Firestore lease (a redeploy never seeds twice), and "Reset demo" in the sidebar gives each judge a fresh case.
- **The edition-neutral demo image** on Cloud Run (same container as option a; not yet run against a live project):
  ```bash
  gcloud builds submit . --region=asia-south1 --config=deploy/demo/cloudbuild.yaml \
    --substitutions=_IMAGE=asia-south1-docker.pkg.dev/<PROJECT_ID>/wapsi/wapsi-demo:latest
  gcloud run deploy wapsi-demo --image=asia-south1-docker.pkg.dev/<PROJECT_ID>/wapsi/wapsi-demo:latest \
    --region=asia-south1 --port=7860 --cpu=1 --memory=1Gi --min-instances=1 --max-instances=1 \
    --session-affinity --allow-unauthenticated \
    --set-env-vars=WAPSI_LLM_BASE_URL=https://developer.amd.com.cn/radeon/api/v1,WAPSI_LLM_MODEL=Gemma-4-31B-IT \
    --set-secrets=WAPSI_LLM_API_KEY=<secret-name>:latest
  ```
  `--max-instances=1` and `--session-affinity` keep a visitor on the instance that holds their session.

## Run the same container on your machine

```bash
DOCKER_BUILDKIT=1 docker build -f deploy/demo/Dockerfile -t wapsi-demo .
docker run --rm -p 7860:7860 wapsi-demo                         # recorded run, no key
docker run --rm -p 7860:7860 -e WAPSI_LLM_BASE_URL -e WAPSI_LLM_API_KEY -e WAPSI_LLM_MODEL wapsi-demo   # live
```

Open `http://localhost:7860/?page=case-room&case=CASE-31009260000101`. `-e NAME` without a value passes the
variable from your shell, so the key never appears on the command line. `--build-arg BAKE_DOCS_INDEX=0` skips the
docs-index download; `--build-arg BAKE_WORLD=0` skips the demo world. Without Docker:
`uv run streamlit run deploy/demo/app.py`.

## Check the live endpoint, and record again

```bash
# the endpoint variables are in your shell (never in a file in the repository)
uv run python scripts/record_demo_prose.py --dry-run          # what is recorded, what is missing; no call
uv run python scripts/record_demo_prose.py --refresh          # the two showcase cases: 5 prose fields
uv run python scripts/record_demo_prose.py --source demo      # the demo world's first 12 cases: 26 more
uv run python -m evals.harness --world showcase --llm openai-compat --min-interval 4   # 2 cases end to end
```

The recorder is polite on a shared key (sequential, 4 s apart, at most 12 a minute and 40 calls a run, HTTP 429
retried after `Retry-After`, every answer cached under `.cache/llm`). It refuses degenerate answers, so a broken
model records nothing, and it prints what it measured. Commit the updated `data/recordings/demo_prose.json` and
rebuild the Space folder. The harness writes `evals/reports/showcase-openai-compat.json` with the model id, the
endpoint host, calls, cache hits, retries, tokens and the cost the endpoint reported; a rerun is served from the
cache (measured on 2026-10-14 against the Token Factory with Qwen3.8-27B: 9 requests, 0 API calls, 9 cache hits).

## Keep it up through judging

1. Submit the Space's direct URL with the case-room deep link.
2. The day before, and an hour before judging: open the URL, look at the chip on the Drafts page. Green and
   "... on AMD via ..." means live. "Recorded run" with no banner means the endpoint is configured but did not
   answer: check the key and the daily cap (`GET <base url>/usage`).
3. If the endpoint misbehaves, set `WAPSI_UI_LLM=recorded` in the host's settings. The demo then never calls it and
   says "Recorded run — live model calls are switched off".
4. Keep option b deployed as a second URL.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Banner "Recorded run — live AMD endpoint not configured" although you set the variables | The banner lists the variables it could not see. On Spaces a new variable needs a restart (Settings → Restart). On Community Cloud the secrets must be top-level TOML keys |
| Chip "Recorded run · ..." without the banner | The endpoint is configured but the call failed (rate limit, wrong model name, key rejected, degenerate output). "Regenerate drafts" on the Drafts page tries a fresh live call after a minute |
| Chip "Fixed paragraphs · model not reached" | `WAPSI_UI_LLM=live` and the endpoint failed, or the model's prose broke the placeholder rules twice. The numbers are still right; switch to `auto` to use the recording |
| Chip "Offline stub · no model call" | No recorded answer exists for that case's figures. Record it (`scripts/record_demo_prose.py --source demo`) |
| Space build fails at `COPY` | The folder was assembled before a file was committed. Commit, rerun `make_space.py --force`, upload again |
| Space build is slow or the index step warns | A public legal site was unreachable; the image still works on the citations-only corpus. Rebuild later (Settings → Factory rebuild) |
| Upload on the Voice Intake page fails with 403 behind another proxy | Keep `--server.enableXsrfProtection false --server.enableCORS false` in the start command |
| Community Cloud: `ModuleNotFoundError: plotly` | The app was created with a main file at the repository root. Recreate it with `deploy/streamlit_cloud/streamlit_app.py` |
