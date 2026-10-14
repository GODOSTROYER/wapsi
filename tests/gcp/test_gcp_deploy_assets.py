from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GCP = ROOT / "editions" / "gcp"
DEPLOY = GCP / "deploy"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_dockerfile_python311_uv_non_root():
    text = read(GCP / "Dockerfile")
    assert "FROM python:3.11-slim" in text
    assert "ghcr.io/astral-sh/uv" in text and "uv sync --frozen" in text and "--extra gcp" in text
    user = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
    assert user and not user[-1].startswith(("root", "0"))
    assert "editions.gcp.app.server:app" in text and "${PORT}" in text
    assert "ARG BAKE_WORLD=1" in text and "--out /app/data/world" in text  # WAPSI_LEDGER=parquet without BigQuery
    for path in ("src", "ui", "editions/gcp", "data/docs"):
        assert f"COPY {path} " in text, path
    ignore = read(GCP / "Dockerfile.dockerignore")
    for pattern in ("data/generated", ".venv", ".git", "tests", "editions/amd"):
        assert pattern in ignore


def test_cloudrun_script_flags():
    text = read(DEPLOY / "cloudrun.sh")
    for flag in ("--service-account", "--min-instances", "--max-instances", "--set-secrets",
                 "wapsi-role-signing-key:latest", "--env-vars-file", "--no-allow-unauthenticated",
                 "--startup-probe", "--push-auth-service-account", "--dead-letter-topic",
                 "scheduler jobs create pubsub", "Asia/Kolkata", "budgets create",
                 "/apps/case_pipeline/trigger/pubsub", "OBJECT_FINALIZE", "judging-on", "judging-off"):
        assert flag in text, flag
    for var in ("WAPSI_PROJECT", "WAPSI_LOCATION", "WAPSI_BQ_DATASET", "WAPSI_MODEL", "WAPSI_PRIVATE_LLM_URL",
                "WAPSI_MODEL_ARMOR_TEMPLATE", "WAPSI_MEMORY_BANK_ID", "WAPSI_DEMO_MODE", "WAPSI_GEMINI_BACKEND",
                "WAPSI_LEDGER", "WAPSI_ADK_STORE"):
        assert var in text, var
    assert "roles/editor" not in text and "roles/owner" not in text
    assert 'MIN_INSTANCES="${MIN_INSTANCES:-0}"' in text
    assert 'PROFILE="${PROFILE:-free}"' in text and 'MODEL="${MODEL:-gemini-3.8-flash}"' in text


def test_cloudrun_script_has_no_gpu_and_never_expands_the_api_key():
    text = read(DEPLOY / "cloudrun.sh")
    for gpu in ("--gpu", "gpu-type", "nvidia", "no-gpu-zonal-redundancy"):
        assert gpu not in text, gpu
    # the only place $GEMINI_API_KEY appears is inside the quoted heredoc the user copies (never expanded)
    help_start = text.index("gemini_key_help() {")
    help_end = text.index("\nEOF\n", help_start)
    assert "<<'EOF'" in text[help_start:help_end]
    outside = text[:help_start] + text[help_end:]
    assert not re.search(r"\$\{?GEMINI_API_KEY", outside)
    assert "--set-env-vars" not in outside and "GEMINI_API_KEY=${GEMINI_SECRET}:latest" in outside
    assert "printf '%s' \"$GEMINI_API_KEY\" | gcloud secrets create" in text[help_start:help_end]


def test_gemma_script_follows_the_cloud_run_gpu_guide():
    text = read(DEPLOY / "gemma_vllm_service.sh")
    assert "OPTIONAL PAID ADD-ON, OFF BY DEFAULT" in text and 'if [[ "${ALLOW_GPU:-0}" != "1" ]]; then' in text
    assert text.index("ALLOW_GPU:-0") < text.index("gcloud --project")  # the guard runs before any gcloud call
    for needle in ("nvidia-l4", "us-central1", "nvidia-rtx-pro-6000", "asia-south2", "--gpu 1",
                   "--no-gpu-zonal-redundancy", "--no-cpu-throttling", "google/gemma-4-E4B-it",
                   "pytorch-vllm-serve:gemma4", "--command \"vllm\"", "tcpSocket.port=8080",
                   "--min-instances", "--no-allow-unauthenticated"):
        assert needle in text, needle


def _bash_works() -> bool:
    if shutil.which("bash") is None:
        return False
    try:
        out = subprocess.run(["bash", "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "ok"


@pytest.mark.skipif(not _bash_works(), reason="a working bash is not available")
@pytest.mark.parametrize("script", ["cloudrun.sh", "gemma_vllm_service.sh"])
def test_scripts_parse(script):
    # piped on stdin: no path translation between Windows and the bash in use
    data = (DEPLOY / script).read_bytes()
    assert b"\r\n" not in data, "shell scripts must use LF line endings"
    try:  # Git Bash can take a while to start on a loaded Windows machine: a slow start is not a parse error
        out = subprocess.run(["bash", "-n"], input=data, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        pytest.skip("bash did not start within 120 s on this machine")
    assert out.returncode == 0, out.stderr.decode(errors="replace")


def test_model_armor_template_is_a_valid_template():
    from google.cloud import modelarmor_v1 as ma

    raw = read(DEPLOY / "model_armor_template.json")
    json.loads(raw)
    t = ma.Template.from_json(raw)
    fc = t.filter_config
    assert fc.pi_and_jailbreak_filter_settings.filter_enforcement == ma.PiAndJailbreakFilterSettings.PiAndJailbreakFilterEnforcement.ENABLED
    assert fc.malicious_uri_filter_settings.filter_enforcement
    assert {f.filter_type for f in fc.rai_settings.rai_filters} == {
        ma.RaiFilterType.DANGEROUS, ma.RaiFilterType.HARASSMENT, ma.RaiFilterType.HATE_SPEECH,
        ma.RaiFilterType.SEXUALLY_EXPLICIT}
    assert t.template_metadata.log_sanitize_operations is False  # complaint text must not land in logs


def test_cloudbuild_config():
    text = read(DEPLOY / "cloudbuild.yaml")
    assert "editions/gcp/Dockerfile" in text and "${_IMAGE}" in text and "CLOUD_LOGGING_ONLY" in text
    assert "machineType" not in text  # the free tier covers the default e2-standard-2 build machine only
    assert "BAKE_WORLD=${_BAKE_WORLD}" in text and '_BAKE_WORLD: "1"' in text


def test_architecture_shows_only_google_cloud_in_the_live_path():
    text = read(GCP / "architecture.md")
    diagram = text.split("```mermaid", 1)[1].split("```", 1)[0]
    for service in ("Cloud Run", "ADK", "Gemini", "Gemma 4 on Cloud Run GPU", "BigQuery", "Firestore",
                    "Cloud Storage", "Pub/Sub", "Cloud Scheduler", "Model Armor", "Memory Bank",
                    "Secret Manager", "Cloud Logging"):
        assert service in diagram, service
    for other in ("Snowflake", "AMD", "ROCm", "OpenAI", "Jira", "Slack", "AWS", "Azure"):
        assert other.lower() not in diagram.lower(), other


def test_readme_starts_with_the_free_trial_setup():
    readme = read(GCP / "README.md")
    first = readme.index("## ")
    assert readme[first:].startswith("## Free-trial setup (what you need)")
    section = readme[first:readme.index("\n## ", first + 3)]
    for needle in ("gcloud auth login", "gcloud auth application-default login", "aistudio.google.com/api-keys",
                   "asia-south1", "gcloud secrets create", "--data-file=-", "PROFILE=free", "$0",
                   "gemini-3.8-flash", "judging-on", "Model Armor", "Memory Bank", "GPU"):
        assert needle in section, needle
    assert "| Service |" in section  # the cost table


def test_readme_and_iam_cover_the_essentials():
    readme = read(GCP / "README.md")
    for needle in ("uv run uvicorn editions.gcp.app.server:app", "adk web editions/gcp/agents",
                   "Cost guardrails", "scale to zero", "judging-on", "Budget", "Open items",
                   "Nothing in this folder has been deployed"):
        assert needle.lower() in readme.lower(), needle
    iam = read(DEPLOY / "iam.md")
    for role in ("roles/aiplatform.user", "roles/bigquery.jobUser", "roles/bigquery.dataViewer",
                 "roles/datastore.user", "roles/secretmanager.secretAccessor", "roles/run.invoker"):
        assert role in iam, role
    assert not re.search(r"\|\s*`?roles/(editor|owner)`?\s*\|", iam)  # never granted in a table row


SENTINEL = "AIzaSy-SENTINEL-must-never-appear-0123"
FAKES = r"""
set -e
export PROJECT_ID=wapsi-test GEMINI_API_KEY=@KEY@ GOOGLE_API_KEY=@KEY@ @EXTRA@
SECRET_EXISTS=@SECRET@
gcloud() {
  printf 'CALL gcloud %s\n' "$*" >&2
  [[ "$*" == *"--data-file=-"* ]] && cat >/dev/null
  case "$*" in
    *"--env-vars-file"*) local a="$*"; local f="${a##*--env-vars-file }"; f="${f%% *}"; sed 's/^/ENVFILE /' "$f" >&2 ;;
  esac
  case "$*" in
    *"secrets describe wapsi-gemini-api-key"*) [[ "$SECRET_EXISTS" == 1 ]]; return ;;
    *"billing projects describe"*) echo "billingAccounts/0000AA-BBBBBB-CCCCCC"; return 0 ;;
    *"projects describe"*) echo 123456; return 0 ;;
    *"run services describe"*) echo "https://svc.example.run.app"; return 0 ;;
    *" describe "*|*" list "*) return 1 ;;
  esac
  return 0
}
bq() { printf 'CALL bq %s\n' "$*" >&2; [[ "$*" != *" show "* ]]; }
curl() { printf 'CALL curl %s\n' "$*" >&2; }
uv() { printf 'CALL uv %s\n' "$*" >&2; }
"""


def _dry_run(script: str, step: str, *, secret: bool = False, extra: str = "") -> tuple[int, str]:
    prelude = (FAKES.replace("@KEY@", SENTINEL).replace("@EXTRA@", extra)
               .replace("@SECRET@", "1" if secret else "0"))
    body = (prelude + read(DEPLOY / script)).encode("utf-8")
    out = subprocess.run(["bash", "-s", "--", step], input=body, capture_output=True, timeout=120)
    text = out.stdout.decode(errors="replace") + out.stderr.decode(errors="replace")
    assert SENTINEL not in text, f"the Gemini API key leaked in {script} {step}"
    return out.returncode, text


FREE_APIS = {"run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
             "firestore.googleapis.com", "secretmanager.googleapis.com", "bigquery.googleapis.com",
             "logging.googleapis.com", "iam.googleapis.com", "billingbudgets.googleapis.com"}


@pytest.mark.skipif(not _bash_works(), reason="a working bash is not available")
def test_free_profile_bootstrap_dry_run():
    rc, out = _dry_run("cloudrun.sh", "bootstrap")
    assert rc == 0, out
    enabled = [line for line in out.splitlines() if "services enable" in line]
    assert len(enabled) == 1 and set(enabled[0].split("services enable ", 1)[1].split()) == FREE_APIS
    for paid_only in ("aiplatform", "modelarmor", "iap.googleapis", "--gpu", "roles/aiplatform.user",
                      "reasoningEngines", "gs://wapsi-test-wapsi-intake"):
        assert paid_only not in out, paid_only
    assert "--budget-amount=5USD" in out and "billing-account=0000AA-BBBBBB-CCCCCC" in out
    # no key in Secret Manager yet: the script prints the command for the user to run, with the key unexpanded
    assert "printf '%s' \"$GEMINI_API_KEY\" | gcloud secrets create wapsi-gemini-api-key --project wapsi-test" in out
    assert not re.search(r"CALL gcloud .*secrets (create|versions add) wapsi-gemini-api-key", out)


@pytest.mark.skipif(not _bash_works(), reason="a working bash is not available")
def test_free_profile_deploy_dry_run():
    rc, out = _dry_run("cloudrun.sh", "deploy", secret=False)
    assert rc != 0 and "Secret wapsi-gemini-api-key does not exist yet" in out and "beta run deploy" not in out

    rc, out = _dry_run("cloudrun.sh", "deploy", secret=True)
    assert rc == 0, out
    deploys = [line for line in out.splitlines() if "run deploy" in line]
    assert len(deploys) == 2 and all("--min-instances 0" in d for d in deploys)
    assert all("--set-secrets WAPSI_ROLE_SIGNING_KEY=wapsi-role-signing-key:latest,"
               "GEMINI_API_KEY=wapsi-gemini-api-key:latest" in d for d in deploys)
    assert "--max-instances 1" in deploys[0]  # in-memory ADK sessions: one API instance
    assert "--gpu" not in out and "GOOGLE_GENAI_USE_VERTEXAI" not in out
    for env in ('WAPSI_GEMINI_BACKEND: "aistudio"', 'WAPSI_MODEL: "gemini-3.8-flash"', 'WAPSI_LEDGER: "bigquery"',
                'WAPSI_MEMORY_BANK_ID: ""', 'WAPSI_PRIVATE_LLM_URL: ""', 'WAPSI_MODEL_ARMOR_TEMPLATE: ""',
                'WAPSI_OTEL_TO_CLOUD: "0"'):
        assert f"ENVFILE {env}" in out, env
    assert "secrets add-iam-policy-binding wapsi-gemini-api-key" in out

    rc, out = _dry_run("cloudrun.sh", "deploy", secret=True, extra="LEDGER=parquet")
    assert rc == 0 and 'ENVFILE WAPSI_WORLD: "/app/data/world"' in out


@pytest.mark.skipif(not _bash_works(), reason="a working bash is not available")
def test_free_profile_refuses_paid_features_and_judging_costs_are_announced():
    for extra, msg in (("GEMMA_URL=https://g.example.run.app", "GPUs are blocked"),
                       ("MEMORY_BANK_ID=123", "Memory Bank is billed")):
        rc, out = _dry_run("cloudrun.sh", "deploy", secret=True, extra=extra)
        assert rc != 0 and msg in out and "beta run deploy" not in out
    rc, out = _dry_run("cloudrun.sh", "judging-on")
    assert rc == 0 and "COST:" in out and out.count("--min-instances 1") == 2
    rc, out = _dry_run("gemma_vllm_service.sh", "")
    assert rc == 2 and "OFF by default" in out and "CALL gcloud" not in out
