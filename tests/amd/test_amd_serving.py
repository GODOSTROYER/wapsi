"""AMD edition serving assets: compose file, env template, shell scripts, Dockerfile, smoke test (offline)."""

from __future__ import annotations

import json
import os
import re
import subprocess

import httpx
import pytest

from wapsi.adapters.llm_fake import FakeLLM

yaml = pytest.importorskip("yaml")

SECRETS = {"HF_TOKEN", "VLLM_API_KEY", "WAPSI_API_TOKEN"}
_VAR = re.compile(r"\$\{([A-Z0-9_]+)(?::?-([^}]*))?\}")


def _compose(amd_dir):
    return yaml.safe_load((amd_dir / "docker-compose.yml").read_text(encoding="utf-8"))


def _interp(value: str, env: dict[str, str] | None = None) -> str:
    """Resolve ${VAR:-default} the way Compose does with an empty environment (or ``env``)."""
    env = env or {}
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), str(value))


def _env_example(amd_dir) -> dict[str, str]:
    out = {}
    for line in (amd_dir / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- compose


def test_compose_has_required_services_and_profiles(amd_dir):
    c = _compose(amd_dir)
    services = c["services"]
    assert {"llm", "verifier-llm", "gnn", "app", "app-demo"} <= set(services)
    assert services["llm"]["profiles"] == ["gpu"]
    assert services["app"]["profiles"] == ["gpu"]
    assert services["verifier-llm"]["profiles"] == ["verifier"]
    assert services["gnn"]["profiles"] == ["gnn"]
    assert services["app-demo"]["profiles"] == ["demo"]


@pytest.mark.parametrize("name", ["llm", "verifier-llm", "gnn"])
def test_gpu_services_have_rocm_passthrough(amd_dir, name):
    svc = _compose(amd_dir)["services"][name]
    assert set(svc["devices"]) >= {"/dev/kfd", "/dev/dri"}
    assert "video" in svc["group_add"]
    assert svc["ipc"] == "host"
    assert "seccomp=unconfined" in svc["security_opt"]


def test_llm_service_serves_the_configured_model(amd_dir):
    llm = _compose(amd_dir)["services"]["llm"]
    assert llm["entrypoint"][-2:] == ["/opt/wapsi/serving/start_llm.sh", "serve"]
    env = llm["environment"]
    assert _interp(env["VLLM_GPU_UTIL"]) == "0.55"
    assert _interp(env["WAPSI_LLM_MODEL"]) == "/models/wapsi-gemma-4-31b-str-merged"
    assert _interp(env["VLLM_ATTENTION_BACKEND"]) == "TRITON_ATTN"
    assert int(_interp(env["VLLM_MAX_MODEL_LEN"])) > 0
    assert "HF_TOKEN" in env and _interp(env["HF_TOKEN"]) == ""  # from the shell, never a default
    vols = [_interp(v) for v in llm["volumes"]]
    assert any(v.endswith(":/root/.cache/huggingface") for v in vols)
    assert any(v.endswith(":/models:ro") for v in vols)
    assert [_interp(p) for p in llm["ports"]] == ["127.0.0.1:8000:8000"]


def test_vllm_healthchecks_probe_v1_models(amd_dir):
    services = _compose(amd_dir)["services"]
    for name in ("llm", "verifier-llm"):
        hc = services[name]["healthcheck"]
        assert "/v1/models" in " ".join(hc["test"])
        assert hc["start_period"]
    assert services["gnn"]["healthcheck"]["test"]
    assert "/healthz" in " ".join(services["app"]["healthcheck"]["test"])


def test_verifier_runs_small_model_at_low_utilization(amd_dir):
    v = _compose(amd_dir)["services"]["verifier-llm"]
    assert v["entrypoint"][-2:] == ["serve", "verifier"]
    assert float(_interp(v["environment"]["VERIFIER_GPU_UTIL"])) <= 0.2
    assert "gemma-4" in _interp(v["environment"]["WAPSI_VERIFIER_MODEL_ID"])
    assert v["depends_on"]["llm"]["condition"] == "service_healthy"  # vLLM first, then the rest


def test_app_services_build_from_the_edition_dockerfile(amd_dir):
    root = amd_dir.parents[1]
    services = _compose(amd_dir)["services"]
    for name in ("app", "app-demo"):
        build = services[name]["build"]
        assert (amd_dir / build["context"]).resolve() == root.resolve()
        assert build["dockerfile"] == "editions/amd/app/Dockerfile"
        assert (root / build["dockerfile"]).exists()
    assert services["gnn"]["build"]["dockerfile"] == "editions/amd/gnn/Dockerfile"
    app = services["app"]
    assert app["environment"]["WAPSI_LLM_BASE_URL"] == "http://llm:8000/v1"
    assert app["depends_on"]["llm"]["condition"] == "service_healthy"
    assert app["depends_on"]["gnn"]["required"] is False
    assert app["depends_on"]["verifier-llm"]["required"] is False
    assert {_interp(p) for p in app["ports"]} == {"127.0.0.1:8501:8501", "127.0.0.1:8080:8080"}
    demo = services["app-demo"]
    assert demo["environment"]["WAPSI_DEMO_MODE"] == "1"
    assert "depends_on" not in demo


def test_compose_never_uses_latest_and_images_come_from_env(amd_dir):
    services = _compose(amd_dir)["services"]
    for name in ("llm", "verifier-llm"):
        assert services[name]["image"].startswith("${VLLM_IMAGE")
    for name, svc in services.items():
        assert not _interp(svc.get("image", "")).endswith(":latest"), name


def test_env_example_pins_digests_and_has_no_secrets(amd_dir):
    env = _env_example(amd_dir)
    for key in ("VLLM_IMAGE", "VLLM_GEMMA4_IMAGE", "PYTHON_IMAGE", "UV_IMAGE"):
        assert re.fullmatch(r"[\w./-]+@sha256:[0-9a-f]{64}", env[key]), key
    assert not (SECRETS & set(env)), "secrets must come from the shell, not .env"
    assert not [k for k, v in env.items() if v.endswith(":latest")]
    assert env["COMPOSE_PROFILES"] == "gpu"
    assert env["VLLM_GPU_UTIL"] == "0.55"


def test_every_compose_variable_is_documented_or_defaulted(amd_dir):
    text = (amd_dir / "docker-compose.yml").read_text(encoding="utf-8")
    env = _env_example(amd_dir)
    for var, default in _VAR.findall(text):
        assert var in env or var in SECRETS or default != "" or var.startswith("COMPOSE_"), var


def test_docker_compose_config_validates(amd_dir, tmp_path):
    """``docker compose config`` (client-side only: no daemon, no network) accepts every profile."""
    import shutil

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker CLI not installed")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "WAPSI_", "COMPOSE_"))}
    env["COMPOSE_PROFILES"] = "gpu,verifier,gnn,demo"
    res = subprocess.run([docker, "compose", "-f", str(amd_dir / "docker-compose.yml"), "--env-file",
                          str(amd_dir / ".env.example"), "config", "--quiet"], capture_output=True, text=True,
                         env=env, timeout=60, cwd=tmp_path)
    if res.returncode != 0 and "unknown" in res.stderr.lower() and "compose" in res.stderr.lower():
        pytest.skip("docker compose plugin not available")
    assert res.returncode == 0, res.stderr


# --------------------------------------------------------------------------- shell scripts and image


def _scripts(amd_dir):
    return sorted(p for p in amd_dir.rglob("*.sh") if "gnn" not in p.parts and "lora" not in p.parts)


def test_shell_scripts_are_lf_and_strict(amd_dir):
    scripts = _scripts(amd_dir)
    assert {p.name for p in scripts} >= {"start_llm.sh", "pull_and_pin.sh", "entrypoint.sh"}
    for p in scripts:
        raw = p.read_bytes()
        assert b"\r\n" not in raw, f"{p.name} has CRLF line endings"
        text = raw.decode("utf-8")
        assert text.startswith("#!/usr/bin/env bash"), p.name
        head = [ln.strip() for ln in text.splitlines()[:40] if ln.strip() and not ln.lstrip().startswith("#")]
        assert head and head[0] == "set -euo pipefail", f"{p.name}: set -euo pipefail must be the first command"


def test_app_dockerfile_runs_as_non_root(amd_dir):
    text = (amd_dir / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in text
    assert "EXPOSE 8080 8501" in text
    assert "HEALTHCHECK" in text
    assert "ARG PYTHON_IMAGE" in text and "FROM ${PYTHON_IMAGE}" in text
    ignore = (amd_dir / "app" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and "data/generated" in ignore


def _run(bash, script, *args, env=None):
    inherited = {k: v for k, v in os.environ.items() if not k.startswith(("VLLM_", "WAPSI_"))}
    return subprocess.run([bash, script.as_posix(), *args], capture_output=True, text=True,
                          env={**inherited, **(env or {})}, timeout=60)


def test_start_llm_print_gemma_merged(amd_dir, bash):
    res = _run(bash, amd_dir / "serving" / "start_llm.sh", "print", "gemma4-merged")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "vllm serve /models/wapsi-gemma-4-31b-str-merged" in out
    for flag in ("--attention-backend TRITON_ATTN", "--gpu-memory-utilization 0.55", "--served-model-name wapsi-llm",
                 "--device=/dev/kfd", "--device=/dev/dri", "--group-add=video", "--ipc=host",
                 "seccomp=unconfined", "127.0.0.1:8000:8000"):
        assert flag in out, flag
    assert "--limit-mm-per-prompt" not in out  # merged checkpoint may be text-only


def test_start_llm_print_profiles(amd_dir, bash):
    script = amd_dir / "serving" / "start_llm.sh"
    base = _run(bash, script, "print", "gemma4-base").stdout
    assert "google/gemma-4-31B-it" in base and "--limit-mm-per-prompt" in base
    qwen = _run(bash, script, "print", "qwen-fallback").stdout
    assert "Qwen/Qwen3.6-35B-A3B" in qwen and "--reasoning-parser qwen3" in qwen
    assert "enable_thinking" in qwen and "--attention-backend" not in qwen
    ver = _run(bash, script, "print", "verifier", env={"VLLM_API_KEY": "s3cret-key"}).stdout
    assert "google/gemma-4-E4B-it" in ver and "--gpu-memory-utilization 0.15" in ver
    assert "127.0.0.1:8001:8000" in ver
    assert "s3cret-key" not in ver and "--api-key" in ver  # redacted
    bad = _run(bash, script, "print", "nope")
    assert bad.returncode == 64


def test_start_llm_docker_refuses_unpinned_image(amd_dir, bash):
    res = _run(bash, amd_dir / "serving" / "start_llm.sh", "docker", "gemma4-base", env={"VLLM_IMAGE": ""})
    assert res.returncode == 65
    assert "pull_and_pin" in res.stderr


def test_pull_and_pin_dry_run(amd_dir, bash, tmp_path):
    env_file = tmp_path / ".env"
    script = amd_dir / "serving" / "pull_and_pin.sh"
    res = _run(bash, script, "--dry-run", env={"WAPSI_ENV_FILE": env_file.as_posix()})
    assert res.returncode == 0, res.stderr
    assert "rocm/vllm:rocm10.0.0_ubuntu24.04_py3.14_pytorch_2.12.0_vllm_0.27.0" in res.stdout
    assert "VLLM_GEMMA4_IMAGE=vllm/vllm-openai-rocm@sha256:<digest>" in res.stdout
    assert not env_file.exists()  # dry run touches nothing
    latest = _run(bash, script, "--dry-run", "EXTRA_IMAGE=foo/bar:latest", env={"WAPSI_ENV_FILE": env_file.as_posix()})
    assert latest.returncode == 65


# --------------------------------------------------------------------------- smoke test (mock OpenAI server)


def _openai_mock(llm=None, *, models=None, calls=None):
    llm = llm or FakeLLM()
    cards = models or [{"id": "wapsi-llm", "object": "model", "root": "/models/wapsi-gemma-4-31b-str-merged",
                        "max_model_len": 16384}]

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": cards})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema")
            text = llm.complete(body["messages"], json_schema=schema)
            if "single word OK" in body["messages"][-1]["content"]:
                text = "OK"
            return httpx.Response(200, json={
                "id": "cmpl-1", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return httpx.MockTransport(handler)


def test_smoke_test_passes_against_openai_compatible_server(capsys):
    from editions.amd.serving import smoke_test

    calls: list[httpx.Request] = []
    code = smoke_test.main(["--base-url", "http://llm.test/v1", "--json"], transport=_openai_mock(calls=calls))
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True
    assert out["model"] == "wapsi-llm" and out["model_root"].endswith("merged")
    assert out["chat"]["reply"] == "OK"
    p = out["pipeline"]
    assert p["case_id"] == "CASE-31009260000101"
    assert p["drafts"] > 0 and p["all_verified"] is True
    assert p["llm_drafts"] > 0 and p["prose_fallbacks"] == 0
    posts = [c for c in calls if c.url.path.endswith("/chat/completions")]
    assert len(posts) > 1  # one smoke completion + the pipeline's prose requests
    sent = json.loads(posts[-1].content)
    assert sent["model"] == "wapsi-llm" and sent["temperature"] == 0.0


def test_smoke_test_counts_prose_fallbacks_when_the_model_invents_numbers(capsys):
    from editions.amd.serving import smoke_test

    code = smoke_test.main(["--base-url", "http://llm.test/v1", "--json"],
                           transport=_openai_mock(FakeLLM(hallucinate=True)))
    out = json.loads(capsys.readouterr().out)
    assert code == 0  # the verifier still passes: bad prose is replaced by the deterministic paragraph
    assert out["pipeline"]["prose_fallbacks"] > 0
    assert out["pipeline"]["all_verified"] is True


def test_smoke_test_reports_endpoint_down(capsys):
    from editions.amd.serving import smoke_test

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    code = smoke_test.main(["--base-url", "http://llm.test/v1", "--skip-pipeline"], transport=httpx.MockTransport(down))
    assert code == smoke_test.EXIT_ENDPOINT
    assert "endpoint check failed" in capsys.readouterr().err


def test_smoke_test_rejects_unknown_model(capsys):
    from editions.amd.serving import smoke_test

    code = smoke_test.main(["--base-url", "http://llm.test/v1", "--model", "other", "--skip-pipeline"],
                           transport=_openai_mock())
    assert code == smoke_test.EXIT_ENDPOINT
    assert "not served" in capsys.readouterr().err
