"""The edition-neutral demo container (deploy/demo): what the Dockerfile copies against what the ignore files let
through, the build steps that bake the demo world and the docs index, no key in the image, and the launcher's
start-up step (defaults, an idempotent demo world). Offline; nothing is built."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "deploy" / "demo"
DOCKERFILE = DEMO / "Dockerfile"
DOCKERIGNORE = DEMO / "Dockerfile.dockerignore"
# what the shared app reads at run time in the demo: code, corpus, voice samples, the recorded prose, the KPI report
RUNTIME_FILES = ("src/wapsi/models.py", "src/wapsi/adapters/llm_replay.py", "src/wapsi/adapters/llm_env.py",
                 "src/wapsi/draft/templates/str.j2", "src/wapsi/_data/citations.json", "ui/app.py",
                 "ui/components/llm_status.py", ".streamlit/config.toml", "data/docs/citations.json",
                 "data/docs/facts.json", "data/docs/reg_radar.json", "data/docs/sources.json",
                 "data/samples/voice/manifest.json", "data/samples/voice/meera_impersonation.wav",
                 "data/recordings/demo_prose.json", "evals/reports/latest.json", "deploy/demo/app.py",
                 "deploy/demo/prepare.py", "pyproject.toml", "uv.lock", "README.md", "LICENSE")
NEVER_IN_CONTEXT = (".env", ".env.local", "editions/amd/.env", ".streamlit/secrets.toml", ".secrets/amd.env",
                    "data/docs/raw/sop-2026.pdf", "data/docs/index/text/sop-2026.txt", ".git/config",
                    ".venv/pyvenv.cfg", ".cache/llm/ab/abcd.json", "tests/demo/conftest.py",
                    "data/generated/demo/accounts.parquet", "models/xgb.json")


def ignore_patterns(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def dockerignored(path: str, patterns: list[str]) -> bool:
    """BuildKit .dockerignore semantics for the simple patterns this repository uses: anchored at the context root,
    a directory pattern excludes everything below it, ``**/`` matches at any depth, ``!`` re-includes."""
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


def copies(dockerfile: Path = DOCKERFILE) -> list[str]:
    out: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"] and not any(p.startswith("--from") for p in parts):
            out += [p.rstrip("/") for p in parts[1:-1] if not p.startswith("--")]
    return out


def tracked() -> set[str]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    done = subprocess.run([git, "-C", str(ROOT), "ls-files", "-c", "-o", "--exclude-standard"], capture_output=True,
                          text=True, timeout=60)
    if done.returncode != 0:
        pytest.skip("not a git work tree")
    return set(done.stdout.splitlines())


# --------------------------------------------------------------------------- COPY list against the ignore files


def test_every_copy_source_exists_and_survives_the_dockerignore():
    """A COPY whose source the ignore file drops fails the build ("not found"); a COPY of something git ignores
    works on the author's machine and fails on a fresh clone (Hugging Face, Cloud Build)."""
    patterns = ignore_patterns(DOCKERIGNORE)
    files = tracked()
    sources = copies()
    assert {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "src", "ui", ".streamlit", "data/docs",
            "data/samples", "data/recordings", "evals/reports/latest.json", "deploy/demo"} == set(sources)
    for src in sources:
        assert (ROOT / src).exists(), f"COPY {src}: not in the repository"
        assert not dockerignored(src, patterns), f"COPY {src}: excluded by {DOCKERIGNORE.name}"
        inside = [f for f in files if f == src or f.startswith(src + "/")]
        assert inside, f"COPY {src}: no file known to git (a fresh clone could not build the image)"
        kept = [f for f in inside if not dockerignored(f, patterns)]
        assert kept, f"COPY {src}: every file under it is excluded by {DOCKERIGNORE.name}"


def test_runtime_files_reach_the_image_and_secrets_never_reach_the_build_context():
    patterns = ignore_patterns(DOCKERIGNORE)
    files = tracked()
    sources = copies()
    for f in RUNTIME_FILES:
        assert f in files, f"{f} is not in the repository"
        assert not dockerignored(f, patterns), f"{f} is excluded by {DOCKERIGNORE.name}"
        assert any(f == c or f.startswith(c + "/") for c in sources), f"no COPY line ships {f}"
    for f in NEVER_IN_CONTEXT:
        assert dockerignored(f, patterns), f"{f} would be sent to the docker build context"
    # nothing the image does not use is copied wholesale
    assert not {"editions", "tests", "docs", "pitch", "scripts", "data", "evals", "."} & set(sources)


def test_the_gcloudignore_uploads_what_the_demo_image_copies():
    """Cloud Run builds from the upload ``gcloud builds submit`` makes (.gcloudignore, gitignore semantics)."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    done = subprocess.run([git, "-C", str(ROOT), "ls-files", "-c", "-i", f"--exclude-from={ROOT / '.gcloudignore'}"],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        pytest.skip("not a git work tree")
    excluded = set(done.stdout.splitlines())
    files = tracked()
    assert any(f.startswith("tests/") for f in excluded)  # the matcher works: tests are not uploaded
    for src in copies():
        inside = [f for f in files if (f == src or f.startswith(src + "/")) and not f.endswith(".gitignore")]
        dropped = [f for f in inside if f in excluded]
        assert not dropped, (src, dropped[:5])


# --------------------------------------------------------------------------- the Dockerfile


def test_dockerfile_bakes_the_world_and_the_docs_index_and_holds_no_key():
    text = DOCKERFILE.read_text(encoding="utf-8")
    world = text.index("python deploy/demo/prepare.py")
    bake = text.index("python -m wapsi.search.bake --fetch --prune-raw")
    assert text.index("COPY deploy/demo ") < world < text.index("USER 1000")
    assert text.index("COPY data/docs ") < bake < text.index("USER 1000")  # baked before the tree is locked down
    assert "ARG BAKE_WORLD=1" in text and 'if [ "${BAKE_WORLD}" = "1" ]' in text
    assert "ARG BAKE_DOCS_INDEX=1" in text and 'if [ "${BAKE_DOCS_INDEX}" = "1" ]' in text
    assert "--prune-raw && rm -rf /app/data/docs/index/text" in text  # the image keeps the index, not the texts
    assert "--extra ui --extra docs" in text and "--extra gcp" not in text and "--extra amd" not in text
    assert "uv sync --frozen --no-dev" in text  # versions come from uv.lock
    assert "USER 1000:1000" in text and "useradd --create-home --uid 1000" in text  # Hugging Face Spaces' user
    assert "EXPOSE 7860" in text and "PORT=7860" in text and "--server.port ${PORT}" in text
    assert "streamlit run deploy/demo/app.py" in text and "--server.address 0.0.0.0" in text
    assert "WAPSI_UI_LLM=auto" in text and "HEALTHCHECK" in text and "/_stcore/health" in text
    # the endpoint is configured at run time only: no key, URL or model is baked into the image
    env_lines = " ".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for name in ("WAPSI_LLM_API_KEY", "WAPSI_LLM_BASE_URL", "WAPSI_LLM_MODEL", "RADEON_API_KEY", "GEMINI_API_KEY"):
        assert name not in env_lines, name
    assert not re.search(r"\brc-[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{10,}", text)
    assert "--mount=type=secret" not in text and "ARG WAPSI" not in text
    for ignore in (DOCKERIGNORE, ROOT / ".gcloudignore"):
        lines = [ln.strip().strip("/") for ln in ignore.read_text(encoding="utf-8").splitlines()]
        assert "data/docs/raw" in lines and "data/docs/index/text" in lines  # never the documents' full text
        assert not [ln for ln in lines if ln in ("data/docs/index", "data/docs", "data", "data/recordings")], ignore


def test_streamlit_config_is_shipped_and_identical_in_both_places():
    assert (ROOT / ".streamlit" / "config.toml").read_bytes() == (ROOT / "ui" / ".streamlit" / "config.toml").read_bytes()


# --------------------------------------------------------------------------- the launcher's start-up step


def load_prepare():
    spec = importlib.util.spec_from_file_location("wapsi_demo_prepare", DEMO / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_defaults_never_override_the_deployment():
    prepare = load_prepare()
    env: dict[str, str] = {}
    assert prepare.apply_defaults(env) == {"WAPSI_UI_LLM": "auto", "WAPSI_NOW": "2026-09-16T11:00:00+05:30"}
    mine = {"WAPSI_UI_LLM": "recorded", "WAPSI_NOW": ""}
    assert prepare.apply_defaults(mine) == {} and mine == {"WAPSI_UI_LLM": "recorded", "WAPSI_NOW": ""}


def test_demo_world_is_generated_once_and_reused(tmp_path):
    prepare = load_prepare()
    assert prepare.ensure_demo_world(tmp_path, enabled=False) == "skipped"
    assert not (tmp_path / "data").exists()
    assert prepare.ensure_demo_world(tmp_path, enabled=True) == "built"
    world = tmp_path / "data" / "generated" / "demo"
    stamp = (world / "transactions.parquet").stat().st_mtime_ns
    assert (world / "golden" / "cases.jsonl").exists() and not list(world.parent.glob("*.tmp"))
    assert prepare.ensure_demo_world(tmp_path, enabled=True) == "present"  # idempotent: a second start is a no-op
    assert prepare.ensure_demo_world(tmp_path, enabled=False) == "present"
    assert (world / "transactions.parquet").stat().st_mtime_ns == stamp

    # the app reads its case list from the world's own golden folder (data/golden is not in the image)
    import ui.data_source as data_source
    from wapsi.adapters.parquet_ledger import ParquetLedger

    groups = data_source.world_case_groups(ParquetLedger(world), world, 12)
    assert len(groups) == 12 and all(groups)


def test_demo_world_failure_is_reported_not_raised(tmp_path, monkeypatch):
    prepare = load_prepare()
    blocker = tmp_path / "data"
    blocker.write_text("a file where the data folder should be", encoding="utf-8")
    assert prepare.ensure_demo_world(tmp_path, enabled=True) == "failed"  # the app then shows the showcase cases


def test_launchers_run_the_shared_app():
    app = (DEMO / "app.py").read_text(encoding="utf-8")
    assert 'runpy.run_path(str(ROOT / "ui" / "app.py"), run_name="__main__")' in app
    assert app.index("prepare.apply_defaults()") < app.index("runpy.run_path")
    assert "import streamlit" not in app  # ui/app.py must make the first Streamlit call (set_page_config)
    cloud = (ROOT / "deploy" / "streamlit_cloud" / "streamlit_app.py").read_text(encoding="utf-8")
    assert 'runpy.run_path(str(ROOT / "deploy" / "demo" / "app.py"), run_name="__main__")' in cloud
