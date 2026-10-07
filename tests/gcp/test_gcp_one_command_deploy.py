"""The one-command Cloud Run deploy (editions/gcp/deploy/deploy.ps1 + deploy.sh), fully offline.

- both scripts parse (Windows PowerShell 5.1 / PowerShell 7 parser, ``bash -n``);
- ``--dry-run`` / ``-DryRun`` prints every step and command, deterministically, the same in both shells, and runs
  nothing;
- a live run against fake ``gcloud`` / ``uv`` / ``curl`` (or ``Invoke-RestMethod``) goes through every step, hands
  the Gemini key to Secret Manager byte-exact and never prints it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "editions" / "gcp" / "deploy"
PS1 = DEPLOY / "deploy.ps1"
SH = DEPLOY / "deploy.sh"
SENTINEL = "AIzaSy-SENTINEL-one-command-deploy-0123"
STEPS = ["preflight", "project", "apis", "budget", "registry", "iam", "secrets", "firestore", "bigquery", "build",
         "deploy", "smoke"]


def _works(cmd: list[str]) -> bool:
    if shutil.which(cmd[0]) is None:
        return False
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# the bash on PATH (Git Bash on Windows): a bare "bash" in CreateProcess would find System32's WSL launcher first
BASH_EXE = shutil.which("bash") or "bash"
BASH = _works([BASH_EXE, "-c", "true"])
SHELLS = [s for s in ("pwsh", "powershell") if _works([s, "-NoProfile", "-NonInteractive", "-Command", "exit 0"])]
LIVE_SHELL = "powershell" if "powershell" in SHELLS else (SHELLS[0] if SHELLS else "pwsh")  # 5.1 first: the user's
needs_bash = pytest.mark.skipif(not BASH, reason="a working bash is not available")
needs_ps = pytest.mark.skipif(not SHELLS, reason="PowerShell is not available")


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "PROJECT_ID", "REGION", "MODEL", "LEDGER", "TAG", "MIN_INSTANCES",
        "BUDGET_USD", "DEMO_NOW", "GEMINI_BACKEND"}}
    env.update(extra)
    return env


def _norm(text: str) -> list[str]:
    """Lines without CR and without the shell-specific teardown hint."""
    return [ln for ln in text.replace("\r\n", "\n").split("\n") if not ln.startswith("Tear down:")]


# --------------------------------------------------------------------------- parsing and hygiene


@pytest.mark.parametrize("shell", SHELLS or ["pwsh"])
@needs_ps
def test_ps1_parses(shell):
    cmd = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{PS1}', [ref]$null, [ref]$e); if ($e.Count) {{ $e | ForEach-Object {{ $_.ToString() }}; exit 1 }}")
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr


def test_ps1_is_ascii_and_sh_is_lf():
    PS1.read_bytes().decode("ascii")  # Windows PowerShell 5.1 reads BOM-less scripts as ANSI: keep it ASCII
    assert b"\r\n" not in SH.read_bytes()


@needs_bash
def test_sh_parses():
    out = subprocess.run([BASH_EXE, "-n"], input=SH.read_bytes(), capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode(errors="replace")


# --------------------------------------------------------------------------- fakes


BASH_FAKES = r"""
log() { printf '%s\n' "$*" >> "$CALL_LOG"; }
gcloud() {
  log "CALL gcloud $*"
  case "$*" in
    *"secrets create wapsi-gemini-api-key"*"--data-file=-"*|*"secrets versions add wapsi-gemini-api-key"*"--data-file=-"*)
      local got; got="$(cat)"
      if [[ "$got" == "$EXPECT_KEY" ]]; then log "SECRET_OK"; else log "SECRET_MISMATCH ${#got}"; fi ;;
    *"--data-file=-"*) cat >/dev/null ;;
  esac
  case "$*" in
    *"--env-vars-file="*) local a="$*"; local f="${a##*--env-vars-file=}"; f="${f%% *}"; while IFS= read -r l; do log "ENVFILE $l"; done < "$f" ;;
  esac
  case "$*" in
    "--version") return 0 ;;
    *"auth list"*) echo "tester@example.com"; return 0 ;;
    *"print-access-token"*) echo "ya29.fake"; return 0 ;;
    *"billingEnabled"*) echo "True"; return 0 ;;
    *"billingAccountName"*) echo "billingAccounts/0000AA-BBBBBB-CCCCCC"; return 0 ;;
    *"budgets list"*) return 0 ;;
    *"secrets describe wapsi-gemini-api-key"*) [[ "${SECRET_EXISTS:-0}" == 1 ]]; return ;;
    *"run services describe wapsi-api"*) echo "https://wapsi-api-test.a.run.app"; return 0 ;;
    *"run services describe wapsi-ui"*) echo "https://wapsi-ui-test.a.run.app"; return 0 ;;
    *" describe"*) return 1 ;;
  esac
  return 0
}
uv() { log "CALL uv $*"; }
curl() {
  log "CALL curl $*"
  case "$*" in
    *healthz*) echo "{\"status\":\"ok\",\"settings\":{\"gemini_api_key_set\":true},\"docs_fallback\":${DOCS_FALLBACK:-false}}" ;;
    *demo/seed*) echo '{"seed":{"state":"done"}}' ;;
    *demo/cases*) echo "{\"case_id\":\"CASE-31009260000101\",\"drafts_verified\":${VERIFIED:-7},\"drafts\":[{\"prose_fallback\":${FALLBACK:-false}},{\"prose_fallback\":false}]}" ;;
    *_stcore*) echo ok ;;
  esac
}
sleep() { :; }
"""

PS_FAKES = r"""
function global:Log([string]$s) { Add-Content -LiteralPath $env:CALL_LOG -Value $s -Encoding UTF8 }
function global:gcloud {
    $line = (@($args) | ForEach-Object { [string]$_ }) -join ' '
    Log "CALL gcloud $line"
    $df = @($args | Where-Object { ([string]$_).StartsWith('--data-file=') }) | Select-Object -First 1
    if ($df -and ($line -like '*secrets create wapsi-gemini-api-key*' -or $line -like '*secrets versions add wapsi-gemini-api-key*')) {
        $path = ([string]$df).Substring(12)
        $got = [IO.File]::ReadAllText($path)
        if ($got -ceq $env:EXPECT_KEY) { Log 'SECRET_OK' } else { Log ("SECRET_MISMATCH " + $got.Length) }
        Log "SECRET_FILE $path"
    }
    $ev = @($args | Where-Object { ([string]$_).StartsWith('--env-vars-file=') }) | Select-Object -First 1
    if ($ev) { Get-Content -LiteralPath ([string]$ev).Substring(16) | ForEach-Object { Log "ENVFILE $_" } }
    $global:LASTEXITCODE = 0
    if ($line -eq '--version') { return }
    if ($line -like '*auth list*') { 'tester@example.com'; return }
    if ($line -like '*print-access-token*') { 'ya29.fake'; return }
    if ($line -like '*billingEnabled*') { 'True'; return }
    if ($line -like '*billingAccountName*') { 'billingAccounts/0000AA-BBBBBB-CCCCCC'; return }
    if ($line -like '*budgets list*') { return }
    if ($line -like '*secrets describe wapsi-gemini-api-key*') { if ($env:SECRET_EXISTS -ne '1') { $global:LASTEXITCODE = 1 }; return }
    if ($line -like '*run services describe wapsi-api*') { 'https://wapsi-api-test.a.run.app'; return }
    if ($line -like '*run services describe wapsi-ui*') { 'https://wapsi-ui-test.a.run.app'; return }
    if ($line -like '* describe*') { $global:LASTEXITCODE = 1; return }
}
function global:uv { Log ("CALL uv " + ((@($args) | ForEach-Object { [string]$_ }) -join ' ')); $global:LASTEXITCODE = 0 }
function global:Invoke-RestMethod {
    param($Method, $Uri, $TimeoutSec)
    Log "CALL irm $Method $Uri"
    if ($Uri -like '*healthz') { return [pscustomobject]@{ status = 'ok'; settings = [pscustomobject]@{ gemini_api_key_set = $true }; docs_fallback = ($env:DOCS_FALLBACK -eq 'true') } }
    if ($Uri -like '*demo/seed*') { return [pscustomobject]@{ seed = [pscustomobject]@{ state = 'done' } } }
    if ($Uri -like '*demo/cases*') { return [pscustomobject]@{ case_id = 'CASE-31009260000101'; drafts_verified = [int]$env:VERIFIED; drafts = @([pscustomobject]@{ prose_fallback = ($env:FALLBACK -eq 'true') }, [pscustomobject]@{ prose_fallback = $false }) } }
}
function global:Invoke-WebRequest { param($Uri, [switch]$UseBasicParsing, $TimeoutSec) Log "CALL iwr $Uri"; return [pscustomobject]@{ Content = 'ok' } }
function global:Start-Sleep { param($Seconds) }
"""


def run_sh(args: list[str], tmp_path: Path, *, key: str | None = SENTINEL, **env: str):
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    e = _env(CALL_LOG=log.as_posix(), EXPECT_KEY=SENTINEL, WAPSI_LAST_IMAGE_FILE=(tmp_path / "last_image").as_posix(),
             **{"VERIFIED": "7", **env})
    if key is not None:
        e["GEMINI_API_KEY"] = key
    body = (BASH_FAKES + SH.read_text(encoding="utf-8")).encode("utf-8")
    # stdin: no Windows/MSYS path translation of the script; cwd = deploy/ so ROOT resolves to the repository
    out = subprocess.run([BASH_EXE, "-s", "--", *args], input=body, capture_output=True, timeout=300, cwd=DEPLOY,
                         env=e)
    stdout, stderr = out.stdout.decode(errors="replace"), out.stderr.decode(errors="replace")
    calls = log.read_text(encoding="utf-8")
    for text in (stdout, stderr, calls):
        assert SENTINEL not in text, "the Gemini API key leaked"
    return out.returncode, stdout, stderr, calls


def run_ps(shell: str, args: list[str], tmp_path: Path, *, key: str | None = SENTINEL, fakes: bool = True, **env):
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    e = _env(CALL_LOG=str(log), EXPECT_KEY=SENTINEL, WAPSI_LAST_IMAGE_FILE=str(tmp_path / "last_image"),
             **{"VERIFIED": "7", **env})
    if key is not None:
        e["GEMINI_API_KEY"] = key
    quoted = " ".join("'" + a.replace("'", "''") + "'" if not a.startswith("-") else a for a in args)
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text((PS_FAKES if fakes else "") + f"\n& '{PS1}' {quoted}\nexit $LASTEXITCODE\n", encoding="utf-8")
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                         capture_output=True, timeout=300, env=e)
    stdout, stderr = out.stdout.decode(errors="replace"), out.stderr.decode(errors="replace")
    calls = log.read_text(encoding="utf-8-sig")
    for text in (stdout, stderr, calls):
        assert SENTINEL not in text, "the Gemini API key leaked"
    return out.returncode, stdout, stderr, calls


# --------------------------------------------------------------------------- dry run


@needs_bash
def test_dry_run_prints_every_step_runs_nothing_and_is_deterministic(tmp_path):
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--dry-run"], tmp_path)
    assert rc == 0, err
    assert calls == ""  # nothing was executed, not even the checks
    rc2, out2, _, _ = run_sh(["--project-id", "wapsi-test", "--dry-run"], tmp_path)
    assert out == out2
    for i, step in enumerate(STEPS, 1):
        assert f"== [{i}/12] {step}: " in out and f"== [{i}/12] {step}: skipped" not in out, step
    apis = next(ln for ln in out.splitlines() if "services enable" in ln)
    assert set(re.findall(r"\b[a-z]+\.googleapis\.com", apis)) == {
        "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
        "secretmanager.googleapis.com", "firestore.googleapis.com", "bigquery.googleapis.com",
        "logging.googleapis.com", "iam.googleapis.com", "billingbudgets.googleapis.com"}
    for needle in (
        "+ gcloud config set project wapsi-test",
        "+ gcloud artifacts repositories create wapsi --repository-format=docker --location=asia-south1",
        "--budget-amount=50USD",
        "+ gcloud iam service-accounts create wapsi-api",
        "--role=roles/bigquery.jobUser", "--role=roles/datastore.user", "--role=roles/secretmanager.secretAccessor",
        "+ gcloud secrets create wapsi-gemini-api-key --replication-policy=user-managed --locations=asia-south1",
        "'--data-file=<GEMINI_API_KEY from your environment, never printed>'",
        "+ gcloud firestore databases create --location=asia-south1 --type=firestore-native",
        "python -m editions.gcp.app.seed world --out data/generated/judging",
        "python -m wapsi.adapters.bigquery_ledger load --world data/generated/judging --project wapsi-test "
        "--dataset wapsi --location asia-south1 --reader serviceAccount:wapsi-api@wapsi-test.iam.gserviceaccount.com",
        "+ gcloud builds submit . --region=asia-south1 --config=editions/gcp/deploy/cloudbuild.yaml",
        "_IMAGE=asia-south1-docker.pkg.dev/wapsi-test/wapsi/wapsi-gcp:dryrun",
        '    WAPSI_SEED_CASES: "showcase"', '    WAPSI_DEMO_MODE: "0"', '    WAPSI_TRUST_ROLE_HEADER: "1"',
        "+ uv run --extra docs python -m wapsi.search.bake --fetch",
        "+ GET <wapsi-api-url>/healthz", "+ POST <wapsi-api-url>/demo/seed?wait_s=240&redo_fallback=1&reset=1",
        "+ GET <wapsi-api-url>/demo/cases/CASE-31009260000101", "+ GET <wapsi-ui-url>/_stcore/health",
        "Cloud Run warm instances (min-instances 1): 45.36 USD/month (1.51 USD/day)",
        "Dry run complete: nothing was executed.",
    ):
        assert needle in out, needle
    deploys = [ln for ln in out.splitlines() if ln.startswith("+ gcloud run deploy")]
    assert len(deploys) == 2
    for d in deploys:
        for flag in ("--min-instances=1", "--allow-unauthenticated", "--env-vars-file=<env.yaml>",
                     "--set-secrets=WAPSI_ROLE_SIGNING_KEY=wapsi-role-signing-key:latest,"
                     "GEMINI_API_KEY=wapsi-gemini-api-key:latest"):
            assert flag in d, (flag, d)
        assert "--gpu" not in d and "--no-allow-unauthenticated" not in d
    assert "--cpu=2 --memory=2Gi" in deploys[0] and "--max-instances=1" in deploys[0]
    assert "--command=streamlit" in deploys[1] and "--session-affinity" in deploys[1]
    assert "roles/editor" not in out and "roles/owner" not in out


@needs_bash
@needs_ps
@pytest.mark.parametrize("shell", SHELLS or ["pwsh"])
def test_powershell_and_bash_print_the_same_plan(shell, tmp_path):
    for sh_args, ps_args in (
        (["--project-id", "wapsi-test", "--dry-run"], ["-ProjectId", "wapsi-test", "-DryRun"]),
        (["--project-id", "wapsi-x1", "--dry-run", "--gemini-backend", "vertex", "--ledger", "parquet",
          "--min-instances", "0", "--budget-usd", "0", "--private-api", "--skip", "build", "--demo-now", ""],
         ["-ProjectId", "wapsi-x1", "-DryRun", "-GeminiBackend", "vertex", "-Ledger", "parquet", "-MinInstances", "0",
          "-BudgetUsd", "0", "-PrivateApi", "-Skip", "build", "-DemoNow", ""]),
    ):
        rc, sh_out, sh_err, _ = run_sh(sh_args, tmp_path, key=None)
        assert rc == 0, sh_err
        rc, ps_out, ps_err, calls = run_ps(shell, ps_args, tmp_path, key=None)
        assert rc == 0, ps_err
        assert calls == ""
        assert _norm(ps_out) == _norm(sh_out)


@needs_bash
def test_dry_run_variants(tmp_path):
    rc, out, _, _ = run_sh(["--project-id", "wapsi-test", "--dry-run", "--gemini-backend", "vertex",
                            "--budget-usd", "0", "--ledger", "parquet", "--private-api", "--min-instances", "0"],
                           tmp_path, key=None)
    assert rc == 0
    apis = next(ln for ln in out.splitlines() if "services enable" in ln)
    assert "aiplatform.googleapis.com" in apis and "billingbudgets" not in apis
    assert "--role=roles/aiplatform.user" in out and "secrets create wapsi-gemini-api-key" not in out
    assert 'GOOGLE_GENAI_USE_VERTEXAI: "TRUE"' in out and "GEMINI_API_KEY=wapsi-gemini-api-key" not in out
    assert 'WAPSI_WORLD: "/app/data/world"' in out and "bigquery_ledger load" not in out
    assert 'WAPSI_ADK_WEB: "0"' in out and "--no-allow-unauthenticated" in out and "/dev-ui/" not in out
    assert "no budget alert" in out and "0.00 USD/month" in out and "After judging" not in out
    rc, out, _, _ = run_sh(["--project-id", "wapsi-test", "--dry-run", "--only", "deploy,smoke"], tmp_path)
    assert rc == 0 and "== [10/12] build: skipped" in out and "== [11/12] deploy: Cloud Run services" in out
    for bad in (["--project-id", "Bad_Project"], ["--project-id", "wapsi-test", "--skip", "nope"],
                ["--project-id", "wapsi-test", "--gemini-backend", "openai"], ["--dry-run"],
                ["--project-id", "wapsi-test", "--min-instances", "9"]):
        rc, _, err, calls = run_sh([*bad, "--dry-run"], tmp_path)
        assert rc != 0 and "ERROR:" in err and calls == "", bad


# --------------------------------------------------------------------------- live runs against fakes


def _assert_live(out: str, calls: str):
    assert "Deployed." in out and "https://wapsi-ui-test.a.run.app" in out
    assert "CASE-31009260000101: 7 verified drafts" in out and "UI healthy" in out
    assert calls.count("SECRET_OK") == 1 and "SECRET_MISMATCH" not in calls
    assert re.search(r"CALL gcloud secrets create wapsi-gemini-api-key .*--data-file=", calls)
    deploys = [ln for ln in calls.splitlines() if ln.startswith("CALL gcloud run deploy")]
    assert len(deploys) == 2 and all("--min-instances=1" in d and "--allow-unauthenticated" in d for d in deploys)
    assert all("<env.yaml>" not in d for d in deploys)  # the real temp file was passed
    for env in ('WAPSI_SEED_CASES: "showcase"', 'WAPSI_GEMINI_BACKEND: "aistudio"', 'WAPSI_LEDGER: "bigquery"',
                'WAPSI_NOW: "2026-09-16T11:00:00+05:30"'):
        assert f"ENVFILE {env}" in calls, env
    assert "CALL uv run --extra gcp python -m wapsi.adapters.bigquery_ledger load" in calls
    assert re.search(r"CALL gcloud builds submit \. .*_IMAGE=asia-south1-docker\.pkg\.dev/wapsi-test/wapsi/"
                     r"wapsi-gcp:t1,_BAKE_WORLD=1", calls)
    # the docs index goes up with the source: found on disk, or baked here before the upload
    assert "docs index found: data/docs/index" in out or \
        "CALL uv run --extra docs python -m wapsi.search.bake --fetch" in calls
    lines = calls.splitlines()
    bake = [i for i, ln in enumerate(lines) if "wapsi.search.bake" in ln]
    submit = next(i for i, ln in enumerate(lines) if "builds submit" in ln)
    assert all(i < submit for i in bake)
    assert "demo/seed?wait_s=240&redo_fallback=1&reset=1" in calls  # every judge starts on a fresh case
    assert "citations-only search corpus" not in out


@needs_bash
def test_bash_live_run_against_fakes(tmp_path):
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1"], tmp_path)
    assert rc == 0, out + err
    _assert_live(out, calls)
    assert "secrets create wapsi-gemini-api-key --replication-policy=user-managed --locations=asia-south1 " \
           "--labels=app=wapsi --data-file=-" in calls
    assert (tmp_path / "last_image").read_text().strip().endswith("wapsi-gcp:t1")
    # deploy again without the build step: reuses the last built image
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--only", "deploy", "--min-instances", "0"], tmp_path,
                                 SECRET_EXISTS="1")
    assert rc == 0, err
    assert "wapsi-gcp:t1" in out and calls.count("--min-instances=0") == 2


@needs_bash
def test_bash_live_missing_key_stops_before_deploying(tmp_path):
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1"], tmp_path, key=None)
    assert rc != 0 and "https://aistudio.google.com/api-keys" in err and "read -rs GEMINI_API_KEY" in err
    assert "secrets create wapsi-gemini-api-key" not in calls and "run deploy" not in calls
    # an existing secret is kept, unless --update-key
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "secrets"], tmp_path,
                                 key=None, SECRET_EXISTS="1")
    assert rc == 0 and "secret wapsi-gemini-api-key exists; kept" in out and "SECRET_OK" not in calls
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "secrets", "--update-key"],
                                 tmp_path, SECRET_EXISTS="1")
    assert rc == 0 and calls.count("SECRET_OK") == 1 and "secrets versions add wapsi-gemini-api-key" in calls


@needs_bash
def test_bash_smoke_fails_without_verified_drafts(tmp_path):
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "smoke"], tmp_path,
                                 VERIFIED="0")
    assert rc != 0 and "no verified drafts" in err


@needs_ps
def test_powershell_live_run_against_fakes(tmp_path):
    shell = LIVE_SHELL
    rc, out, err, calls = run_ps(shell, ["-ProjectId", "wapsi-test", "-Tag", "t1"], tmp_path)
    assert rc == 0, out + err
    _assert_live(out, calls)
    secret_files = re.findall(r"^SECRET_FILE (.+)$", calls, re.MULTILINE)
    assert secret_files and not any(Path(p.strip()).exists() for p in secret_files)  # deleted right after use
    assert (tmp_path / "last_image").read_text().strip().endswith("wapsi-gcp:t1")
    rc, out, err, calls = run_ps(shell, ["-ProjectId", "wapsi-test", "-Tag", "t1"], tmp_path, key=None)
    assert rc != 0 and "https://aistudio.google.com/api-keys" in err
    assert "secrets create wapsi-gemini-api-key" not in calls and "run deploy" not in calls


# --------------------------------------------------------------------------- the guide


def test_deploy_guide_covers_the_checklist_options_and_teardown():
    guide = (ROOT / "editions" / "gcp" / "DEPLOY.md").read_text(encoding="utf-8")
    for needle in ("winget install -e --id Google.CloudSDK", "gcloud auth login", "gcloud auth application-default login",
                   "aistudio.google.com/api-keys", "$env:GEMINI_API_KEY", "deploy.ps1 -ProjectId <PROJECT_ID> -DryRun",
                   "deploy.sh --project-id", "## Troubleshooting", "## Teardown", "-Teardown",
                   "gcloud projects delete", "--llm gemini", "-MinInstances 0 -Only deploy", "asia-south1"):
        assert needle in guide, needle
    text = PS1.read_text(encoding="ascii").replace("\r\n", "\n")
    block = text.split("param(", 1)[1].split("\n)\n", 1)[0]
    params = re.findall(r"\]\$(\w+)", block)
    assert len(params) >= 15
    for name in params:
        if name not in ("Tag", "SmokeTimeoutSec", "GeminiLocation"):
            assert f"`-{name}`" in guide, name
    readme = (ROOT / "editions" / "gcp" / "README.md").read_text(encoding="utf-8")
    assert "DEPLOY.md" in readme.split("## ", 1)[0]


# --------------------------------------------------------------------------- review fixes


def test_gcloudignore_uploads_everything_the_image_copies():
    """``gcloud builds submit .`` filters the upload with .gcloudignore (gitignore semantics): an unanchored
    ``docs/`` also drops ``data/docs`` (the citation corpus) and ``COPY data/docs`` fails in Cloud Build. Emulated
    with the ignore matcher of git itself over the tracked files."""
    git = shutil.which("git")
    if git is None or not _works([git, "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"]):
        pytest.skip("not a git work tree")

    def ls(*args: str) -> list[str]:
        return subprocess.run([git, "-C", str(ROOT), "ls-files", "-c", *args], capture_output=True, text=True,
                              check=True, timeout=60).stdout.splitlines()

    excluded = set(ls("-i", f"--exclude-from={ROOT / '.gcloudignore'}"))
    sources: list[str] = []
    for line in (ROOT / "editions" / "gcp" / "Dockerfile").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and not any(p.startswith("--from") for p in parts):
            sources += parts[1:-1]
    assert {"src", "ui", "data/docs", "evals", "editions/gcp", "uv.lock", "pyproject.toml"} <= set(sources)
    for src in sources:
        files = [f for f in ls("--", src) if not f.endswith(".gitignore")]
        assert files, src
        assert not [f for f in files if f in excluded], (src, [f for f in files if f in excluded][:5])
    assert "data/docs/citations.json" not in excluded and "src/wapsi/citations.py" not in excluded
    assert any(f.startswith("tests/") for f in excluded) and any(f.startswith("docs/") for f in excluded)
    assert not [f for f in ls() if f.startswith("editions/snowflake/")]  # that edition is gone from main


# files the merged features read at run time: voice intake (the sample voice notes and their scripts), the agent
# trace panel (the recorded showcase run), Firestore audit sealing (the rules shipped next to the deploy)
RUNTIME_FILES = ("data/samples/voice/manifest.json", "data/samples/voice/meera_impersonation.wav",
                 "data/samples/voice/meera_impersonation.expected.json", "src/wapsi/agenttrace/showcase_recording.json",
                 "src/wapsi/intake/voice.py", "editions/gcp/app/tracing.py", "editions/gcp/app/intake.py",
                 "editions/gcp/deploy/firestore.rules", "data/docs/citations.json", "data/docs/facts.json")


def _dockerignored(path: str, patterns: list[str]) -> bool:
    """BuildKit .dockerignore semantics for the simple patterns this repository uses: anchored at the context root,
    a directory pattern excludes everything below it, ``**/`` matches at any depth, ``!`` re-includes."""
    from fnmatch import fnmatchcase

    out = False
    for raw in patterns:
        neg = raw.startswith("!")
        pat = raw[1:] if neg else raw
        cands = [pat[3:], pat] if pat.startswith("**/") else [pat]
        parts = path.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        hit = any(fnmatchcase(p, c) for c in cands for p in prefixes) or (
            pat.startswith("**/") and any(fnmatchcase("/".join(parts[i:j]), pat[3:])
                                          for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)))
        if hit:
            out = not neg
    return out


def test_image_gets_the_runtime_files_of_the_merged_features():
    """Cloud Build upload (.gcloudignore), docker build context (Dockerfile.dockerignore) and the Dockerfile's COPY
    lines all carry what voice intake, the agent trace panel and the docs corpus read at run time."""
    git = shutil.which("git")
    if git is None or not _works([git, "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"]):
        pytest.skip("not a git work tree")
    tracked = set(subprocess.run([git, "-C", str(ROOT), "ls-files", "-c"], capture_output=True, text=True, check=True,
                                 timeout=60).stdout.splitlines())
    excluded = set(subprocess.run([git, "-C", str(ROOT), "ls-files", "-c", "-i",
                                   f"--exclude-from={ROOT / '.gcloudignore'}"], capture_output=True, text=True,
                                  check=True, timeout=60).stdout.splitlines())
    dockerignore = [ln.strip() for ln in (ROOT / "editions" / "gcp" / "Dockerfile.dockerignore").read_text(
        encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    copies = [ln.split()[1] for ln in (ROOT / "editions" / "gcp" / "Dockerfile").read_text(encoding="utf-8")
              .splitlines() if ln.startswith("COPY ") and "--from" not in ln]
    for f in RUNTIME_FILES:
        assert f in tracked, f
        assert f not in excluded, f"{f} is not uploaded to Cloud Build (.gcloudignore)"
        assert not _dockerignored(f, dockerignore), f"{f} is not in the docker build context (Dockerfile.dockerignore)"
        assert any(f == c or f.startswith(c.rstrip("/") + "/") for c in copies), f"no COPY line ships {f}"
    assert _dockerignored("data/docs/raw/sop-2026.pdf", dockerignore)  # never the raw documents
    assert _dockerignored("tests/gcp/gcp_fakes.py", dockerignore) and not _dockerignored("src/wapsi/models.py",
                                                                                         dockerignore)


@needs_bash
def test_bash_smoke_reseeds_fallback_prose_and_warns(tmp_path):
    rc, out, err, calls = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "smoke"], tmp_path,
                                 FALLBACK="true")
    assert rc == 0, err
    assert "demo/seed?wait_s=240&redo_fallback=1&reset=1" in calls
    assert "WARNING: 1 of 2 drafts use the deterministic fallback paragraph (Gemini was not reached)" in out
    assert ("Judged URL (case room, no login): https://wapsi-ui-test.a.run.app/?page=case-room&case="
            "CASE-31009260000101") in out
    rc, out, err, _ = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "smoke"], tmp_path)
    assert rc == 0 and "fallback paragraph" not in out and "CASE-31009260000101: 7 verified drafts" in out


@needs_ps
def test_powershell_smoke_reseeds_fallback_prose_and_warns(tmp_path):
    rc, out, err, calls = run_ps(LIVE_SHELL, ["-ProjectId", "wapsi-test", "-Tag", "t1", "-Only", "smoke"], tmp_path,
                                 FALLBACK="true")
    assert rc == 0, out + err
    assert "demo/seed?wait_s=240&redo_fallback=1&reset=1" in calls
    assert "WARNING: 1 of 2 drafts use the deterministic fallback paragraph (Gemini was not reached)" in out
    assert "https://wapsi-ui-test.a.run.app/?page=case-room&case=CASE-31009260000101" in out
    rc, out, err, _ = run_ps(LIVE_SHELL, ["-ProjectId", "wapsi-test", "-Tag", "t1", "-Only", "smoke"], tmp_path)
    assert rc == 0 and "fallback paragraph" not in out


def test_sh_avoids_bash4_only_syntax():
    """DEPLOY.md offers deploy.sh on macOS, whose /bin/bash is 3.2: no case-changing expansions, mapfile,
    associative arrays or namerefs."""
    text = SH.read_text(encoding="utf-8")
    for bad in (r"\$\{[A-Za-z_][A-Za-z_0-9]*(,,|\^\^)", r"\bmapfile\b", r"\breadarray\b", r"declare -A", r"local -n"):
        assert not re.search(bad, text), bad


@needs_bash
def test_bash_smoke_warns_when_the_image_lacks_the_docs_index(tmp_path):
    rc, out, err, _ = run_sh(["--project-id", "wapsi-test", "--tag", "t1", "--only", "smoke"], tmp_path,
                             DOCS_FALLBACK="true")
    assert rc == 0, err
    assert "WARNING: the image serves the citations-only search corpus" in out
    assert "python -m wapsi.search.bake --fetch" in out


@needs_ps
def test_powershell_smoke_warns_when_the_image_lacks_the_docs_index(tmp_path):
    rc, out, err, _ = run_ps(LIVE_SHELL, ["-ProjectId", "wapsi-test", "-Tag", "t1", "-Only", "smoke"], tmp_path,
                             DOCS_FALLBACK="true")
    assert rc == 0, out + err
    assert "WARNING: the image serves the citations-only search corpus" in out
    rc, out, err, _ = run_ps(LIVE_SHELL, ["-ProjectId", "wapsi-test", "-Tag", "t1", "-Only", "smoke"], tmp_path)
    assert rc == 0 and "citations-only" not in out


def test_image_bakes_the_docs_index():
    """The Cloud Run image carries the docs search index (``python -m wapsi.search.bake``): after the corpus is
    copied, before the image is locked down for the unprivileged user; Cloud Build passes the switch through."""
    text = (ROOT / "editions" / "gcp" / "Dockerfile").read_text(encoding="utf-8")
    bake = text.index("python -m wapsi.search.bake --fetch --prune-raw")
    assert text.index("COPY data/docs ") < bake < text.index("USER 10001")
    assert "ARG BAKE_DOCS_INDEX=1" in text and 'if [ "${BAKE_DOCS_INDEX}" = "1" ]' in text
    build = (DEPLOY / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert '"BAKE_DOCS_INDEX=${_BAKE_DOCS_INDEX}"' in build and '_BAKE_DOCS_INDEX: "1"' in build
    for ignore in (ROOT / ".gcloudignore", ROOT / "editions" / "gcp" / "Dockerfile.dockerignore"):
        lines = [ln.strip().strip("/") for ln in ignore.read_text(encoding="utf-8").splitlines()]
        assert "data/docs/index/text" in lines and "data/docs/raw" in lines  # never the documents' full text
        assert not [ln for ln in lines if ln in ("data/docs/index", "data/docs", "data")], ignore
