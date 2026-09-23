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
    for path in ("src", "ui", "editions/gcp", "data/docs"):
        assert f"COPY {path} " in text, path
    ignore = read(GCP / "Dockerfile.dockerignore")
    for pattern in ("data/generated", ".venv", ".git", "tests", "editions/snowflake", "editions/amd"):
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
                "WAPSI_MODEL_ARMOR_TEMPLATE", "WAPSI_MEMORY_BANK_ID", "WAPSI_DEMO_MODE"):
        assert var in text, var
    assert "roles/editor" not in text and "roles/owner" not in text
    assert 'MIN_INSTANCES="${MIN_INSTANCES:-0}"' in text


def test_gemma_script_follows_the_cloud_run_gpu_guide():
    text = read(DEPLOY / "gemma_vllm_service.sh")
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
    out = subprocess.run(["bash", "-n"], input=data, capture_output=True, timeout=30)
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


def test_architecture_shows_only_google_cloud_in_the_live_path():
    text = read(GCP / "architecture.md")
    diagram = text.split("```mermaid", 1)[1].split("```", 1)[0]
    for service in ("Cloud Run", "ADK", "Gemini", "Gemma 4 on Cloud Run GPU", "BigQuery", "Firestore",
                    "Cloud Storage", "Pub/Sub", "Cloud Scheduler", "Model Armor", "Memory Bank",
                    "Secret Manager", "Cloud Logging"):
        assert service in diagram, service
    for other in ("Snowflake", "AMD", "ROCm", "OpenAI", "Jira", "Slack", "AWS", "Azure"):
        assert other.lower() not in diagram.lower(), other


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
