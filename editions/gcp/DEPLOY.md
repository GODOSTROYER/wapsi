# Deploy Wapsi to Cloud Run with one command

From a Google Cloud project id to a warm, public, seeded demo in `asia-south1`, with one command. The judged URL
opens on the showcase case with no login. Gemini writes the prose through the Gemini API free tier, and the
numbers still come from data. Everything runs on a free trial. All data is synthetic.

> Status: the scripts are tested offline. They parse in Windows PowerShell 5.1, PowerShell 7 and bash, every
> step is dry-run, and a full run goes against fake `gcloud` / `curl`. They have not yet been run against a real
> Google Cloud project. The first real run is yours: start with `-DryRun`.

## 5-minute checklist (Windows PowerShell)

1. **Install the tools** (once), then open a **new** terminal so `PATH` picks them up:
   ```powershell
   winget install -e --id Google.CloudSDK
   winget install -e --id astral-sh.uv
   ```
2. **Free trial, project and billing** (console, once):
   - Start the [free trial](https://console.cloud.google.com/freetrial) ($300 credit).
   - Create a project, for example `wapsi-demo-1234`.
   - Link the free-trial billing account to the project. You can do this in the console, or with
     `gcloud billing accounts list` and then
     `gcloud billing projects link <PROJECT_ID> --billing-account=<ACCOUNT_ID>`.
3. **Sign in twice.** You run these yourself; each one opens a browser.
   ```powershell
   gcloud auth login
   gcloud auth application-default login
   ```
4. **Gemini API key.** Open [aistudio.google.com/api-keys](https://aistudio.google.com/api-keys), click **Create API
   key** and pick the same project. Stay on the **Free** tier and do not click "Set up billing". Then put the key in
   this PowerShell session without it appearing on screen:
   ```powershell
   $s = Read-Host 'Gemini API key' -AsSecureString
   $env:GEMINI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
   ```
5. **Run the one command** from the repository root:
   ```powershell
   uv sync --extra gcp
   powershell -ExecutionPolicy Bypass -File editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID> -DryRun   # look first
   powershell -ExecutionPolicy Bypass -File editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID>
   ```
   A first run takes about 10 to 15 minutes; Cloud Build takes most of it. It ends with:
   ```
   Deployed.
     Judged URL (case room, no login): https://wapsi-ui-....run.app/?page=case-room&case=CASE-31009260000101
     Agent (ADK dev UI):               https://wapsi-api-....run.app/dev-ui/
     Health:                           https://wapsi-api-....run.app/healthz
     Showcase case (JSON):             https://wapsi-api-....run.app/demo/cases/CASE-31009260000101
   ```

Git Bash, macOS, Linux or Cloud Shell can run the twin script instead; it prints the same plan:
`bash editions/gcp/deploy/deploy.sh --project-id <PROJECT_ID>` (`read -rs GEMINI_API_KEY && export GEMINI_API_KEY`
sets the key).

Re-running is safe. Every step checks before it creates, so a second run skips what already exists. That makes it
the fix for most interrupted runs.

## What the command does

| # | Step | What happens (all idempotent) |
|---|---|---|
| 1 | `preflight` | `gcloud`, `uv`, the signed-in account and Application Default Credentials are present; says whether `GEMINI_API_KEY` is set (never its value) |
| 2 | `project` | `gcloud config set project`; checks that billing is linked |
| 3 | `apis` | enables `run`, `cloudbuild`, `artifactregistry`, `secretmanager`, `firestore`, `bigquery`, `logging`, `iam` (plus `billingbudgets` for the budget; `aiplatform` only with `-GeminiBackend vertex`) |
| 4 | `budget` | a 50 USD budget alert (e-mails at 50 / 90 / 100 % and at 100 % of forecast); `-BudgetUsd 0` skips it |
| 5 | `registry` | Artifact Registry repo `wapsi` with a keep-2-images cleanup policy (the free tier is 0.5 GB) |
| 6 | `iam` | service accounts `wapsi-api`, `wapsi-ui` (runtime: `bigquery.jobUser`, `datastore.user`, `logging.logWriter`) and `wapsi-build` (Cloud Build); no Editor or Owner |
| 7 | `secrets` | `wapsi-gemini-api-key` from your `GEMINI_API_KEY` (kept if it exists; `-UpdateKey` adds a version) and a random role-signing key; read access for the two runtime accounts only |
| 8 | `firestore` | the `(default)` Firestore database in Native mode (the case store; free tier) |
| 9 | `bigquery` | builds the judging world (demo seed 7 + the showcase cases, about 2 MB) and loads it into dataset `wapsi`, with dataset-level read access for the runtime accounts |
| 10 | `build` | makes sure the docs search index is built here (`python -m wapsi.search.bake --fetch`, skipped when `data/docs/index` exists), then `gcloud builds submit` on the free-tier build machine to Artifact Registry; the image bakes the judging world and the docs index (so `search_sop` and the citation corpus work on the judged URL) |
| 11 | `deploy` | `wapsi-api` (ADK agents, 2 vCPU / 2 GiB) and `wapsi-ui` (Streamlit case room, 1 vCPU / 2 GiB), `--min-instances 1`, public, secrets mounted by reference, demo env (below) |
| 12 | `smoke` | `GET /healthz` (warns if the image serves the citations-only search corpus), `POST /demo/seed?reset=1` (processes the showcase cases with Gemini, or resets them to their freshly seeded state so every judge starts on a fresh case; re-processes a stored case whose prose fell back), `GET /demo/cases/CASE-31009260000101` (must have verified drafts; warns if any draft used the fallback paragraph), `GET` the UI health; prints the URLs |

Run part of it with `-Only deploy,smoke` or `-Skip budget`. Without the `build` step, the last image built from
this checkout is reused, or the one you name with `-Tag`.

The deployed environment has no secrets in it:
- `WAPSI_SEED_CASES=showcase`: the API processes the two showcase cases once and stores them in Firestore,
  with a snapshot of each (collection `demo_snapshots`). Start-up seeding never overwrites a stored case. The
  smoke step re-processes a case whose prose fell back to the deterministic paragraphs (Gemini was unreachable,
  for example with a wrong key or model) and resets both cases to their snapshot.
- **One fresh case per judge.** Every visitor works on the same stored cases, so the UI's sidebar says
  "Shared demo" and offers **Reset demo**: the showcase cases go back to their freshly seeded state (plan and
  drafts as computed, nothing approved). A new session also resets them by itself when someone worked on them
  and nobody has touched them for `WAPSI_DEMO_IDLE_RESET_MIN` minutes (default 30; `0` turns it off). The audit
  log stays append-only: it keeps every earlier event, decisions included, and records each reset (`demo_reset`).
- **One seeder at a time.** During a redeploy the old and the new revision both seed, and the UI resets from its
  own service. A lease document in Firestore (`demo_locks/seed-showcase`, taken and released in transactions)
  lets exactly one of them work; the others wait and then find the cases in place. A lease left by a stopped
  instance expires after three minutes.
- `WAPSI_TRUST_ROLE_HEADER=1` and `WAPSI_ADK_WEB=1`: there is no login, and the ADK dev UI is public. This is
  acceptable only because every record is synthetic. `-PrivateApi` keeps the agent API private.
- `WAPSI_NOW=2026-09-16T11:00:00+05:30`: the SLA clocks read as in the showcase story. Use `-DemoNow ''` for the
  wall clock.

## Options

| PowerShell | bash | Default | Meaning |
|---|---|---|---|
| `-ProjectId` | `--project-id` | `$env:PROJECT_ID` | your project id (required) |
| `-Region` | `--region` | `asia-south1` | Cloud Run, Firestore, BigQuery, Artifact Registry, Secret Manager |
| `-GeminiBackend` | `--gemini-backend` | `aistudio` | `aistudio` = Gemini API key (free tier); `vertex` = Agent Platform (billed per token, no key) |
| `-Model` | `--model` | `gemini-3.8-flash` | agent and prose model |
| `-Ledger` | `--ledger` | `bigquery` | `parquet` reads the world baked into the image (no BigQuery) |
| `-MinInstances` | `--min-instances` | `1` | warm instances per service; `0` scales to zero |
| `-BudgetUsd` | `--budget-usd` | `50` | budget alert amount; `0` = none |
| `-PrivateApi` | `--private-api` | off | keep `wapsi-api` (ADK dev UI) private |
| `-UpdateKey` | `--update-key` | off | add a new version of the Gemini key secret from `GEMINI_API_KEY` |
| `-DemoNow` | `--demo-now` | `2026-09-16T11:00:00+05:30` | frozen demo clock (`''` = wall clock) |
| `-Skip` / `-Only` | `--skip` / `--only` | | comma-separated step names |
| `-DryRun` | `--dry-run` | off | print every command and run nothing |
| `-Teardown` | `--teardown` | off | delete the two Cloud Run services |

## Cost

The script prints an estimate before it starts and again at the end:

- **Warm instances.** `min-instances 1` on both services is about **45 USD a month (1.51 USD a day)** at Cloud Run's
  idle-instance list price in `asia-south1`. It is paid from the $300 trial credit, so your card is not charged
  while the account is a free trial.
- **Everything else is free at demo scale.** That covers the Gemini API free tier, BigQuery (about 2 MB), Firestore,
  Secret Manager, Cloud Build, Logging and Artifact Registry. Over the Gemini limit you get HTTP 429, never a bill.
- **After judging**, scale to zero (about 0 USD; the first request after idle waits 10 to 20 s for a cold start):
  ```powershell
  .\editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID> -MinInstances 0 -Only deploy
  ```

## Real-Gemini evaluation (one command)

With `GEMINI_API_KEY` set:

```powershell
uv run python -m evals.harness --world showcase --llm gemini      # 2 cases, about 7 requests
uv run python -m wapsi.synth --size demo --seed 7 --out data/generated/demo
uv run python -m evals.harness --world demo --llm gemini          # 39 cases, about 120 requests
```

- **Report.** It goes to `evals/reports/<world>-gemini.json` and `.md`, and never replaces `latest.json`. It records:
  - the model id, requests, API calls, cache hits, retries and 429s;
  - tokens and a cost estimate (0 USD on the free tier, plus the paid-tier equivalent);
  - `llm_prose_fallback_rate` and `draft_verification_pass_rate`;
  - an **A/B**: the same model writing a raw case summary (LLM-only number hallucination rate) against Wapsi's
    slot-bound drafts.
- **Free-tier aware.** It allows 10 requests a minute and 250 a day by default (`--rpm`, `--rpd`, or `WAPSI_LLM_RPM`
  / `WAPSI_LLM_RPD`; the real limits for your project are at
  [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit)).
  - 429 and 503 answers are retried with backoff, honouring the server's retry delay.
  - A daily-quota 429 is not retried. The run finishes with deterministic fallback paragraphs, and the report says so.
- **Reruns are free.** Answers are cached by prompt hash in `.cache/llm` (gitignored). A second run makes no API calls.
  The cache is for replaying evaluations only and is never used as training data. Use `--no-cache` to force fresh
  calls.
- **Other models.** `--llm gemini:gemini-3.5-flash-lite` picks another free-tier model.
  `WAPSI_GEMINI_BACKEND=vertex` uses Agent Platform with your ADC instead of a key.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | run it as shown: `powershell -ExecutionPolicy Bypass -File ...`; this changes nothing permanently |
| `gcloud` / `uv` not found right after `winget` | open a new terminal (the installer updates `PATH` for new shells only) |
| `not signed in` / `no Application Default Credentials` | `gcloud auth login` / `gcloud auth application-default login` |
| `billing is not enabled` | link the free-trial billing account (checklist step 2), then re-run |
| an IAM binding fails right after a service account was created | the script already retries for about a minute; re-run, since finished steps are skipped |
| Cloud Build: permission denied reading the source or pushing the image | re-run with `-Only iam,build` (role grants can take a minute to propagate) |
| `allUsers` / `--allow-unauthenticated` refused | your organisation enforces domain-restricted sharing: use a project under a personal account, or `-PrivateApi` and share the UI another way |
| BigQuery load: `quota project` warning or 403 | `gcloud auth application-default set-quota-project <PROJECT_ID>`, then `-Only bigquery` |
| Firestore: database exists in Datastore mode | use a fresh project (a project's `(default)` database mode cannot be changed) |
| smoke: `drafts use the deterministic fallback paragraph` | Gemini was not reached when the case was processed. Check `/healthz` (`gemini_api_key_set`) and the logs; fix the key (`-Only secrets,deploy,smoke -UpdateKey`) or the model (`-Model ... -Only deploy,smoke`); the smoke step then re-processes the case |
| smoke: `the image serves the citations-only search corpus` | the docs index was neither uploaded nor downloadable at build time. Build it here (`uv run --extra docs python -m wapsi.search.bake --fetch`; it reuses `data/docs/raw` if present), then `-Only build,deploy,smoke` |
| a judge sees decisions from an earlier visit | **Reset demo** in the UI's sidebar, or `-Only smoke` (it resets the showcase cases) |
| smoke: `showcase case is not in the case store yet` | Gemini or BigQuery was slow: `-Only smoke` again in a minute; logs: `gcloud run services logs read wapsi-api --region asia-south1` |
| `/healthz` says `gemini_api_key_set: false` | the secret is empty or missing: set `GEMINI_API_KEY`, then `-Only secrets,deploy -UpdateKey` |
| Gemini 429 in the app or the eval | per-minute limit: wait a minute. Daily limit: resets at midnight Pacific, or use `-Model gemini-3.5-flash-lite` |
| eval: `ab_test` is `null` or `llm_baseline` failures say `finish_reason=MAX_TOKENS` | the model spent its output budget thinking: add `--thinking-budget 128` (or `0` where the model allows it) |
| budget creation failed | create it in the console (Billing > Budgets & alerts); it only alerts, it never stops spending |

## Teardown

```powershell
.\editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID> -Teardown    # deletes wapsi-api and wapsi-ui (stops the cost)
gcloud projects delete <PROJECT_ID>                                    # removes everything else too
```

`-Teardown` keeps these, all inside their free tiers: the BigQuery dataset, Firestore, the secrets and the images.
Deleting the project removes them after Google's 30-day recovery window.

## How the key is handled

- **Read once, never shown.** The deploy reads `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) from your shell. It is never
  printed, logged or put on a command line.
- **PowerShell:** the key is written without a newline to a file in your user temp folder, handed to
  `gcloud secrets create --data-file`, and deleted at once.
- **bash:** the key is piped on stdin.
- **Cloud Run** mounts it as `GEMINI_API_KEY` from Secret Manager by reference (`--set-secrets`). Only the two
  runtime service accounts can read it.
- **Tests** run the whole script with a sentinel key and fail if it appears in any output.

The step-by-step script for advanced and paid setups is still [`deploy/cloudrun.sh`](deploy/cloudrun.sh), covering
Pub/Sub, Scheduler, Model Armor and Gemma; see [README.md](README.md).
