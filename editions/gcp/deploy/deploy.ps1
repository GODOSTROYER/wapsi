<#
.SYNOPSIS
  Wapsi, Google Cloud edition: ONE command from a project id to a warm, public, seeded Cloud Run demo.

.DESCRIPTION
  Twin of deploy.sh; both print the same plan with -DryRun. Guide: editions/gcp/DEPLOY.md

    powershell -ExecutionPolicy Bypass -File editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID>
    powershell -ExecutionPolicy Bypass -File editions\gcp\deploy\deploy.ps1 -ProjectId <PROJECT_ID> -DryRun

  Steps (each idempotent, each skippable with -Skip / -Only): preflight project apis budget registry iam secrets
  firestore bigquery build deploy smoke.

  Secrets: GEMINI_API_KEY (or GOOGLE_API_KEY) is read from YOUR environment, written without a newline to a file in
  your user temp folder, handed to `gcloud secrets create --data-file` and deleted at once. It is never printed,
  logged or put on a command line. Cloud Run mounts it from Secret Manager by reference.
  Run the two sign-ins yourself first: gcloud auth login ; gcloud auth application-default login

.EXAMPLE
  $env:GEMINI_API_KEY = Read-Host 'Gemini API key'    # paste; nothing is stored in the repository
  .\editions\gcp\deploy\deploy.ps1 -ProjectId wapsi-aibc-1234
#>
[CmdletBinding()]
param(
    [string]$ProjectId = $env:PROJECT_ID,
    [string]$Region = 'asia-south1',
    [ValidateSet('aistudio', 'vertex')][string]$GeminiBackend = 'aistudio',
    [string]$Model = 'gemini-3.8-flash',
    [string]$GeminiLocation = 'global',
    [ValidateSet('bigquery', 'parquet')][string]$Ledger = 'bigquery',
    [ValidateRange(0, 3)][int]$MinInstances = 1,
    [ValidateRange(0, 100000)][int]$BudgetUsd = 50,
    [AllowEmptyString()][string]$DemoNow = '2026-09-16T11:00:00+05:30',
    [string]$Tag = '',
    [int]$SmokeTimeoutSec = 300,
    [string[]]$Skip = @(),
    [string[]]$Only = @(),
    [switch]$DryRun,
    [switch]$PrivateApi,
    [switch]$UpdateKey,
    [switch]$Teardown
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 on older .NET defaults may not offer TLS 1.2, which Cloud Run requires (smoke test)
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
$Steps = @('preflight', 'project', 'apis', 'budget', 'registry', 'iam', 'secrets', 'firestore', 'bigquery', 'build',
    'deploy', 'smoke')

function Die([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}
function Say([string]$Text) { Write-Host $Text }
function IsDry { return [bool]$DryRun }

function Split-List([string[]]$Items) {
    $out = @()
    foreach ($i in $Items) { foreach ($p in ([string]$i).Split(',')) { if ($p.Trim()) { $out += $p.Trim() } } }
    return , $out
}
$SkipSet = Split-List $Skip
$OnlySet = Split-List $Only

if (-not $ProjectId) { Die '-ProjectId is required (the id of your Google Cloud project)' }
if ($ProjectId -cnotmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$') { Die "not a project id: $ProjectId" }
if ($Region -cnotmatch '^[a-z]+-[a-z]+[0-9]+$') { Die "not a region: $Region" }
foreach ($s in @($SkipSet) + @($OnlySet)) {
    if ($Steps -notcontains $s) { Die "unknown step $s (steps: $($Steps -join ' '))" }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Repo = 'wapsi'; $Dataset = 'wapsi'; $Api = 'wapsi-api'; $Ui = 'wapsi-ui'
$LastImageFile = 'editions/gcp/deploy/.last_image'; if ($env:WAPSI_LAST_IMAGE_FILE) { $LastImageFile = $env:WAPSI_LAST_IMAGE_FILE }
$ApiSa = "wapsi-api@$ProjectId.iam.gserviceaccount.com"
$UiSa = "wapsi-ui@$ProjectId.iam.gserviceaccount.com"
$BuildSa = "wapsi-build@$ProjectId.iam.gserviceaccount.com"
$GeminiSecret = 'wapsi-gemini-api-key'; $RoleSecret = 'wapsi-role-signing-key'
$ImageBase = "$Region-docker.pkg.dev/$ProjectId/$Repo/wapsi-gcp"
$WorldDir = 'data/generated/judging'
$Case = 'CASE-31009260000101'
$KeyShown = '<GEMINI_API_KEY from your environment, never printed>'
$G = @("--project=$ProjectId", '--quiet')
$script:ApiUrl = ''; $script:UiUrl = ''
$Inv = [Globalization.CultureInfo]::InvariantCulture
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

# ---------------------------------------------------------------- printing and running
function Fmt([object[]]$Items) {
    $parts = foreach ($x in $Items) {
        $s = [string]$x
        if ($s -ne '' -and $s -cmatch '^[A-Za-z0-9_./:=,@%+()<>~-]+$') { $s } else { "'" + $s + "'" }
    }
    return ($parts -join ' ')
}
function Show([string]$Mark, [object[]]$Items) { Say ("$Mark " + (Fmt $Items)) }

function Exec([object[]]$Items, [switch]$Discard) {
    $exe = [string]$Items[0]
    $rest = @()
    if ($Items.Count -gt 1) { $rest = @($Items[1..($Items.Count - 1)] | ForEach-Object { [string]$_ }) }
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    $global:LASTEXITCODE = 0
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Discard) { & $exe @rest *> $null } else { & $exe @rest | Out-Host }
    } catch {
        $global:LASTEXITCODE = 1
    } finally {
        $ErrorActionPreference = $prev
    }
    return ($global:LASTEXITCODE -eq 0)
}
function Run { $a = @($args); Show '+' $a; if (IsDry) { return }; if (-not (Exec $a)) { Die ('command failed: ' + (Fmt $a)) } }
function Try-Run { $a = @($args); Show '+' $a; if (IsDry) { return }; if (-not (Exec $a)) { Say '  WARNING: that command failed; continuing' } }
function Check { $a = @($args); Show '?' $a; if (IsDry) { return $false }; return (Exec $a -Discard) }
function Capture {
    $a = @($args); Show '?' $a
    if (IsDry) { return '' }
    $exe = [string]$a[0]; $rest = @($a[1..($a.Count - 1)] | ForEach-Object { [string]$_ })
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return '' }
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = & $exe @rest 2>$null } catch { $out = @() } finally { $ErrorActionPreference = $prev }
    return ((@($out) | ForEach-Object { [string]$_ }) -join "`n").Trim()
}
function Bind {
    # IAM bindings right after a service account is created can need a few seconds to propagate
    $a = @($args); Show '+' $a
    if (IsDry) { return }
    for ($i = 1; $i -le 5; $i++) {
        if (Exec $a -Discard) { return }
        Start-Sleep -Seconds ($i * 4)
    }
    Die ('command failed: ' + (Fmt $a))
}
function Run-Masked([string]$Real, [string]$Shown) {
    $a = @($args)
    $disp = @($a | ForEach-Object { ([string]$_).Replace($Real, $Shown) })
    Show '+' $disp
    if (IsDry) { return }
    if (-not (Exec $a)) { Die ('command failed: ' + (Fmt $disp)) }
}
function Want([string]$Name) {
    if ($OnlySet.Count -gt 0 -and $OnlySet -notcontains $Name) { return $false }
    if ($SkipSet -contains $Name) { return $false }
    return $true
}
function Write-Utf8([string]$Path, [string]$Text) { [IO.File]::WriteAllText($Path, $Text, $Utf8NoBom) }

function Cost-Lines {
    # Cloud Run Tier 1 (asia-south1) idle min-instance list prices: 0.0000025 USD per vCPU-second and per GiB-second
    $perDay = ((2 + 1) + (2 + 2)) * 0.0000025 * 86400 * $MinInstances
    $month = $perDay * 30
    Say 'Estimated cost (list prices; check https://cloud.google.com/run/pricing):'
    Say ("  Cloud Run warm instances (min-instances $MinInstances): " + $month.ToString('0.00', $Inv) +
        ' USD/month (' + $perDay.ToString('0.00', $Inv) + ' USD/day), from the free-trial credit')
    Say '  Gemini API free tier, BigQuery, Firestore, Secret Manager, Cloud Build, Logging at demo scale: 0.00 USD (free tiers)'
    Say '  Artifact Registry: under 0.10 USD/month (the cleanup policy keeps 2 images)'
    if ($MinInstances -ne 0) {
        Say '  After judging: re-run with min-instances 0 and only the deploy step (about 0 USD; cold starts of 10-20 s).'
    }
}

$script:N = 0
function Step([string]$Name, [string]$Title) {
    $script:N++
    Say ''
    if (Want $Name) { Say "== [$($script:N)/$($Steps.Count)] ${Name}: $Title"; return $true }
    Say "== [$($script:N)/$($Steps.Count)] ${Name}: skipped"
    return $false
}
function Gemini-Key {
    if ($env:GEMINI_API_KEY) { return $env:GEMINI_API_KEY }
    return $env:GOOGLE_API_KEY
}
function Key-Help {
    return @'
GEMINI_API_KEY is not set and the secret wapsi-gemini-api-key does not exist yet.
  1. Create a key at https://aistudio.google.com/api-keys (pick this project; stay on the Free tier).
  2. Put it in this PowerShell session without echoing it:
       $s = Read-Host 'Gemini API key' -AsSecureString
       $env:GEMINI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
  3. Re-run this command (finished steps are skipped automatically).
'@
}

# ---------------------------------------------------------------- steps
function Step-Preflight {
    if (-not (Check gcloud --version) -and -not (IsDry)) {
        Die 'gcloud CLI not found: install it (winget install -e --id Google.CloudSDK), open a new terminal, re-run'
    }
    $account = Capture gcloud auth list --filter=status:ACTIVE '--format=value(account)'
    if (-not $account -and -not (IsDry)) { Die 'not signed in to gcloud: run   gcloud auth login' }
    if (-not (Check uv --version) -and -not (IsDry)) {
        Die 'uv not found: https://docs.astral.sh/uv/ (winget install -e --id astral-sh.uv)'
    }
    if ($Ledger -eq 'bigquery' -and (Want 'bigquery')) {
        if (-not (Check gcloud auth application-default print-access-token) -and -not (IsDry)) {
            Die 'no Application Default Credentials (the BigQuery loader needs them): run   gcloud auth application-default login'
        }
    }
    if ($GeminiBackend -eq 'aistudio') {
        if (Gemini-Key) { Say '  GEMINI_API_KEY is set in this shell (value not shown)' }
        else { Say '  GEMINI_API_KEY is not set in this shell (fine when the secret already exists)' }
    }
}

function Step-Project {
    Run gcloud config set project $ProjectId
    $billing = Capture gcloud billing projects describe $ProjectId '--format=value(billingEnabled)' --quiet
    if (-not (IsDry)) {
        switch (([string]$billing).ToLowerInvariant()) {
            'true' { Say '  billing is enabled' }
            'false' { Die "billing is not enabled on ${ProjectId}: gcloud billing accounts list ; gcloud billing projects link $ProjectId --billing-account=<ACCOUNT_ID>" }
            default { Say '  WARNING: could not check billing; Cloud Run needs a linked (free-trial) billing account' }
        }
    }
}

function Step-Apis {
    $apis = @('run.googleapis.com', 'cloudbuild.googleapis.com', 'artifactregistry.googleapis.com',
        'secretmanager.googleapis.com', 'firestore.googleapis.com', 'bigquery.googleapis.com', 'logging.googleapis.com',
        'iam.googleapis.com')
    if ($BudgetUsd -ne 0) { $apis += 'billingbudgets.googleapis.com' }
    if ($GeminiBackend -eq 'vertex') { $apis += 'aiplatform.googleapis.com' }
    Run gcloud services enable @apis @G
}

function Step-Budget {
    if ($BudgetUsd -eq 0) { Say '  no budget alert (--budget-usd 0)'; return }
    $acct = Capture gcloud billing projects describe $ProjectId '--format=value(billingAccountName)' --quiet
    $acct = ([string]$acct) -replace '^billingAccounts/', ''
    if (IsDry) { $acct = '<billing-account>' }
    if (-not $acct) {
        Say '  WARNING: no billing account found; create the budget in the console (Billing > Budgets & alerts)'
        return
    }
    $names = Capture gcloud billing budgets list "--billing-account=$acct" '--format=value(displayName)' "--billing-project=$ProjectId" --quiet
    if ((([string]$names) -split "`n") -contains "wapsi-$ProjectId") { Say "  budget wapsi-$ProjectId exists"; return }
    # a budget alerts by e-mail; it never stops spending (the free trial itself never charges your card)
    Try-Run gcloud billing budgets create "--billing-account=$acct" "--display-name=wapsi-$ProjectId" `
        "--budget-amount=$($BudgetUsd)USD" "--filter-projects=projects/$ProjectId" '--threshold-rule=percent=0.5' `
        '--threshold-rule=percent=0.9' '--threshold-rule=percent=1.0' '--threshold-rule=percent=1.0,basis=forecasted-spend' `
        "--billing-project=$ProjectId" --quiet
}

function Step-Registry {
    if (-not (Check gcloud artifacts repositories describe $Repo "--location=$Region" @G)) {
        Run gcloud artifacts repositories create $Repo --repository-format=docker "--location=$Region" `
            --description=Wapsi @G
    }
    $f = [IO.Path]::GetTempFileName()
    try {
        Write-Utf8 $f '[{"name":"keep-2","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":2}},{"name":"delete-rest","action":{"type":"Delete"},"condition":{"tagState":"any","olderThan":"1d"}}]'
        Show '+' (@('gcloud', 'artifacts', 'repositories', 'set-cleanup-policies', $Repo, "--location=$Region",
                '--policy=<cleanup-policy.json>', '--no-dry-run') + $G)
        if (-not (IsDry)) {
            $a = @('gcloud', 'artifacts', 'repositories', 'set-cleanup-policies', $Repo, "--location=$Region",
                "--policy=$f", '--no-dry-run') + $G
            if (-not (Exec $a -Discard)) { Say '  WARNING: set a cleanup policy in the console (0.5 GB is free)' }
        }
    } finally {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
}

function Step-Iam {
    foreach ($sa in @('wapsi-api', 'wapsi-ui', 'wapsi-build')) {
        if (-not (Check gcloud iam service-accounts describe "$sa@$ProjectId.iam.gserviceaccount.com" @G)) {
            Run gcloud iam service-accounts create $sa "--display-name=$sa" @G
        }
    }
    $roles = @('roles/bigquery.jobUser', 'roles/datastore.user', 'roles/logging.logWriter')
    if ($GeminiBackend -eq 'vertex') { $roles += 'roles/aiplatform.user' }
    foreach ($sa in @($ApiSa, $UiSa)) {
        foreach ($role in $roles) {
            Bind gcloud projects add-iam-policy-binding $ProjectId "--member=serviceAccount:$sa" "--role=$role" `
                --condition=None --quiet
        }
    }
    foreach ($role in @('roles/cloudbuild.builds.builder', 'roles/artifactregistry.writer', 'roles/logging.logWriter')) {
        Bind gcloud projects add-iam-policy-binding $ProjectId "--member=serviceAccount:$BuildSa" "--role=$role" `
            --condition=None --quiet
    }
}

function Exec-WithSecretFile([string]$Value, [object[]]$Before) {
    # the value goes to a user-temp file (no newline, no BOM) that gcloud reads and that is deleted right after
    $f = [IO.Path]::GetTempFileName()
    try {
        Write-Utf8 $f $Value
        return (Exec (@($Before) + @("--data-file=$f") + $G) -Discard)
    } finally {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
}

function Step-Secrets {
    if ($GeminiBackend -eq 'aistudio') {
        $key = Gemini-Key
        if (Check gcloud secrets describe $GeminiSecret @G) {
            if ($UpdateKey) {
                if (-not $key) { Die '-UpdateKey needs GEMINI_API_KEY in this shell' }
                Show '+' (@('gcloud', 'secrets', 'versions', 'add', $GeminiSecret, "--data-file=$KeyShown") + $G)
                if (-not (Exec-WithSecretFile $key @('gcloud', 'secrets', 'versions', 'add', $GeminiSecret))) {
                    Die 'could not add a secret version'
                }
            } else {
                Say "  secret $GeminiSecret exists; kept (--update-key adds a new version from GEMINI_API_KEY)"
            }
        } else {
            if (-not $key -and -not (IsDry)) { Die (Key-Help) }
            Show '+' (@('gcloud', 'secrets', 'create', $GeminiSecret, '--replication-policy=user-managed',
                    "--locations=$Region", '--labels=app=wapsi', "--data-file=$KeyShown") + $G)
            if (-not (IsDry)) {
                $ok = Exec-WithSecretFile $key @('gcloud', 'secrets', 'create', $GeminiSecret,
                    '--replication-policy=user-managed', "--locations=$Region", '--labels=app=wapsi')
                if (-not $ok) { Die 'could not create the secret' }
            }
        }
        foreach ($sa in @($ApiSa, $UiSa)) {
            Bind gcloud secrets add-iam-policy-binding $GeminiSecret "--member=serviceAccount:$sa" `
                --role=roles/secretmanager.secretAccessor @G
        }
    }
    if (-not (Check gcloud secrets describe $RoleSecret @G)) {
        Show '+' (@('gcloud', 'secrets', 'create', $RoleSecret, '--replication-policy=user-managed', "--locations=$Region",
                '--labels=app=wapsi', '--data-file=<48 random bytes made here, never printed>') + $G)
        if (-not (IsDry)) {
            $bytes = New-Object byte[] 48
            $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
            $rng.GetBytes($bytes)
            $ok = Exec-WithSecretFile ([Convert]::ToBase64String($bytes)) @('gcloud', 'secrets', 'create', $RoleSecret,
                '--replication-policy=user-managed', "--locations=$Region", '--labels=app=wapsi')
            if (-not $ok) { Die 'could not create the role-signing secret' }
        }
    }
    foreach ($sa in @($ApiSa, $UiSa)) {
        Bind gcloud secrets add-iam-policy-binding $RoleSecret "--member=serviceAccount:$sa" `
            --role=roles/secretmanager.secretAccessor @G
    }
}

function Step-Firestore {
    if (-not (Check gcloud firestore databases describe @G)) {
        Run gcloud firestore databases create "--location=$Region" --type=firestore-native @G
    }
}

function Step-BigQuery {
    if ($Ledger -ne 'bigquery') { Say '  ledger parquet: the image carries the judging world; nothing to load'; return }
    if (-not (IsDry) -and (Test-Path -LiteralPath (Join-Path $WorldDir 'transactions.parquet'))) {
        Say "  judging world already built: $WorldDir"
    } else {
        Run uv run --extra gcp python -m editions.gcp.app.seed world --out $WorldDir
    }
    Run uv run --extra gcp python -m wapsi.adapters.bigquery_ledger load --world $WorldDir --project $ProjectId `
        --dataset $Dataset --location $Region --reader "serviceAccount:$ApiSa" --reader "serviceAccount:$UiSa"
}

function Step-Build {
    Run gcloud builds submit . "--region=$Region" --config=editions/gcp/deploy/cloudbuild.yaml `
        "--substitutions=_IMAGE=$Image,_BAKE_WORLD=1" "--service-account=projects/$ProjectId/serviceAccounts/$BuildSa" @G
    if (-not (IsDry)) { Write-Utf8 $LastImageFile "$Image`n" }
}

function Env-Yaml {
    # no secrets in here: those are mounted from Secret Manager by reference
    $world = ''; if ($Ledger -eq 'parquet') { $world = '/app/data/world' }
    $web = '1'; if ($PrivateApi) { $web = '0' }
    $lines = @(
        'WAPSI_DEMO_MODE: "0"',
        "WAPSI_PROJECT: `"$ProjectId`"",
        "WAPSI_REGION: `"$Region`"",
        "WAPSI_GEMINI_BACKEND: `"$GeminiBackend`"",
        "WAPSI_LOCATION: `"$GeminiLocation`"",
        "WAPSI_MODEL: `"$Model`"",
        "WAPSI_LEDGER: `"$Ledger`"",
        "WAPSI_WORLD: `"$world`"",
        "WAPSI_BQ_DATASET: `"$Dataset`"",
        'WAPSI_ADK_STORE: "memory"',
        'WAPSI_SEED_CASES: "showcase"',
        'WAPSI_TRUST_ROLE_HEADER: "1"',
        "WAPSI_ADK_WEB: `"$web`"",
        'WAPSI_OTEL_TO_CLOUD: "0"',
        "WAPSI_NOW: `"$DemoNow`"",
        "GOOGLE_CLOUD_PROJECT: `"$ProjectId`""
    )
    if ($GeminiBackend -eq 'vertex') {
        $lines += "GOOGLE_CLOUD_LOCATION: `"$GeminiLocation`""
        $lines += 'GOOGLE_GENAI_USE_VERTEXAI: "TRUE"'
    }
    return , $lines
}

function Service-Urls {
    $script:ApiUrl = Capture gcloud run services describe $Api "--region=$Region" '--format=value(status.url)' @G
    $script:UiUrl = Capture gcloud run services describe $Ui "--region=$Region" '--format=value(status.url)' @G
    if (IsDry) { $script:ApiUrl = '<wapsi-api-url>'; $script:UiUrl = '<wapsi-ui-url>' }
    if (-not $script:ApiUrl -or -not $script:UiUrl) { Die 'could not read the service URLs (deploy first)' }
}

function Step-Deploy {
    $lines = Env-Yaml
    $f = [IO.Path]::GetTempFileName()
    try {
        Write-Utf8 $f (($lines -join "`n") + "`n")
        Say '  env (no secrets):'
        foreach ($l in $lines) { Say "    $l" }
        $secrets = "WAPSI_ROLE_SIGNING_KEY=${RoleSecret}:latest"
        if ($GeminiBackend -eq 'aistudio') { $secrets += ",GEMINI_API_KEY=${GeminiSecret}:latest" }
        $authApi = '--allow-unauthenticated'; if ($PrivateApi) { $authApi = '--no-allow-unauthenticated' }
        # API: ADK agents + /healthz + /demo; one instance because ADK sessions are in memory
        Run-Masked $f '<env.yaml>' gcloud run deploy $Api "--image=$Image" "--region=$Region" `
            "--service-account=$ApiSa" "--env-vars-file=$f" "--set-secrets=$secrets" `
            "--min-instances=$MinInstances" --max-instances=1 --cpu=2 --memory=2Gi --concurrency=20 --timeout=300 `
            --cpu-boost --execution-environment=gen2 '--labels=app=wapsi,component=api,event=aibc' $authApi @G
        # UI: the case room judges open (Streamlit on the same Firestore case store)
        Run-Masked $f '<env.yaml>' gcloud run deploy $Ui "--image=$Image" "--region=$Region" `
            "--service-account=$UiSa" "--env-vars-file=$f" "--set-secrets=$secrets" --command=streamlit `
            '--args=run,editions/gcp/ui_app.py,--server.port=8080,--server.address=0.0.0.0,--server.headless=true' `
            "--min-instances=$MinInstances" --max-instances=2 --cpu=1 --memory=2Gi --concurrency=40 --timeout=3600 `
            --session-affinity --cpu-boost '--labels=app=wapsi,component=ui,event=aibc' --allow-unauthenticated @G
    } finally {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
    Service-Urls
}

function Step-Smoke {
    if (-not $script:ApiUrl) { Service-Urls }
    $api = $script:ApiUrl; $ui = $script:UiUrl
    Say "+ GET $api/healthz"
    if (-not (IsDry)) {
        try { $h = Invoke-RestMethod -Method Get -Uri "$api/healthz" -TimeoutSec 60 }
        catch { Die "health check failed: $api/healthz (logs: gcloud run services logs read $Api --region $Region)" }
        if ($h.status -ne 'ok') { Die 'unexpected /healthz answer' }
        if ($GeminiBackend -eq 'aistudio' -and -not $h.settings.gemini_api_key_set) {
            Say '  WARNING: the API does not see GEMINI_API_KEY (check the secret wapsi-gemini-api-key)'
        }
        Say '  healthy'
    }
    Say "+ POST $api/demo/seed?wait_s=240&redo_fallback=1"
    if (-not (IsDry)) {
        try { $null = Invoke-RestMethod -Method Post -Uri "$api/demo/seed?wait_s=240&redo_fallback=1" -TimeoutSec $SmokeTimeoutSec }
        catch { Say '  WARNING: seeding did not answer in time; it continues in the background' }
    }
    Say "+ GET $api/demo/cases/$Case"
    if (-not (IsDry)) {
        try { $c = Invoke-RestMethod -Method Get -Uri "$api/demo/cases/$Case" -TimeoutSec 60 }
        catch { Die 'the showcase case is not in the case store yet; re-run with -Only smoke in a minute' }
        $n = [int]$c.drafts_verified
        if ($n -le 0) { Die 'the showcase case has no verified drafts' }
        Say "  ${Case}: $n verified drafts"
        $all = @($c.drafts | Where-Object { $null -ne $_ })
        $fb = @($all | Where-Object { $_.prose_fallback -eq $true }).Count
        if ($fb -gt 0) {
            Say "  WARNING: $fb of $($all.Count) drafts use the deterministic fallback paragraph (Gemini was not reached): check /healthz and the API logs, fix the key or the model, redeploy, then run the smoke step again (it re-processes such cases)"
        }
    }
    Say "+ GET $ui/_stcore/health"
    if (-not (IsDry)) {
        try { $u = Invoke-WebRequest -Uri "$ui/_stcore/health" -UseBasicParsing -TimeoutSec 120 }
        catch { Die "the UI is not healthy: $ui" }
        if ([string]$u.Content -notmatch 'ok') { Die 'unexpected UI health answer' }
        Say '  UI healthy'
    }
}

function Do-Teardown {
    Say ''
    Say '== teardown: delete the two Cloud Run services (stops the warm-instance cost)'
    Try-Run gcloud run services delete $Api "--region=$Region" @G
    Try-Run gcloud run services delete $Ui "--region=$Region" @G
    Say 'Kept (inside free tiers): the BigQuery dataset, Firestore, secrets, images. To remove everything:'
    Say "  gcloud projects delete $ProjectId"
}

# ---------------------------------------------------------------- main
Push-Location $Root
try {
    if ($Tag) { $Image = "${ImageBase}:$Tag" }
    elseif (IsDry) { $Image = "${ImageBase}:dryrun" }
    elseif ((Want 'build') -or $Teardown) { $Image = "${ImageBase}:" + (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss', $Inv) }
    elseif ((Test-Path -LiteralPath $LastImageFile) -and ((Get-Content -LiteralPath $LastImageFile -TotalCount 1) -like "${ImageBase}:*")) {
        $Image = [string](Get-Content -LiteralPath $LastImageFile -TotalCount 1)
    } else {
        Die 'the build step is skipped and no image was built from here before: pass -Tag <tag of an existing image>'
    }

    Say 'Wapsi one-command deploy (Google Cloud, AI Builder Cup)'
    Say "  project   $ProjectId"
    Say "  region    $Region"
    Say "  gemini    $GeminiBackend, model $Model"
    Say "  ledger    $Ledger (judging world: demo seed 7 + showcase)"
    Say "  services  $Api (2 vCPU, 2 GiB) + $Ui (1 vCPU, 2 GiB), min-instances $MinInstances, public, no login"
    Say "  image     $Image"
    if (IsDry) { Say '  mode      DRY RUN: every command is printed, nothing is executed' } else { Say '  mode      live' }
    if ($Teardown) { Do-Teardown; exit 0 }
    Say ''
    Cost-Lines

    if (Step 'preflight' 'tools and sign-in') { Step-Preflight }
    if (Step 'project' 'project and billing') { Step-Project }
    if (Step 'apis' 'enable the APIs') { Step-Apis }
    if (Step 'budget' "budget alert ($BudgetUsd USD)") { Step-Budget }
    if (Step 'registry' "Artifact Registry repository $Repo ($Region)") { Step-Registry }
    if (Step 'iam' 'least-privilege service accounts') { Step-Iam }
    if (Step 'secrets' 'Secret Manager (Gemini API key, role-signing key)') { Step-Secrets }
    if (Step 'firestore' 'Firestore (Native mode) case store') { Step-Firestore }
    if (Step 'bigquery' "BigQuery dataset $Dataset + the synthetic judging world") { Step-BigQuery }
    if (Step 'build' 'Cloud Build image') { Step-Build }
    if (Step 'deploy' 'Cloud Run services') { Step-Deploy }
    if (Step 'smoke' 'smoke test') { Step-Smoke }

    Say ''
    if (IsDry) { Say 'Dry run complete: nothing was executed.' } else { Say 'Deployed.' }
    $apiShown = $script:ApiUrl; if (-not $apiShown) { $apiShown = '<wapsi-api-url>' }
    $uiShown = $script:UiUrl; if (-not $uiShown) { $uiShown = '<wapsi-ui-url>' }
    Say "  Judged URL (case room, no login): $uiShown/?page=case-room&case=$Case"
    if (-not $PrivateApi) { Say "  Agent (ADK dev UI):               $apiShown/dev-ui/" }
    Say "  Health:                           $apiShown/healthz"
    Say "  Showcase case (JSON):             $apiShown/demo/cases/$Case"
    Cost-Lines
    Say "Tear down: .\editions\gcp\deploy\deploy.ps1 -ProjectId $ProjectId -Teardown"
} finally {
    Pop-Location
}
