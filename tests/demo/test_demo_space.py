"""The Hugging Face Space folder builder (scripts/make_space.py) and the Streamlit Community Cloud files: the Space
holds exactly what the demo Dockerfile copies, the card Spaces read, no key and no licensed text; the Community
Cloud requirements match uv.lock. Offline; nothing is uploaded."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLOUD = ROOT / "deploy" / "streamlit_cloud"
EXPORT = ["export", "--frozen", "--no-dev", "--extra", "ui", "--extra", "docs", "--no-hashes", "--no-emit-project",
          "--format", "requirements-txt"]


@pytest.fixture(scope="module")
def make_space():
    spec = importlib.util.spec_from_file_location("wapsi_make_space", ROOT / "scripts" / "make_space.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def space(make_space, tmp_path_factory):
    out = tmp_path_factory.mktemp("space") / "wapsi-space"
    report = make_space.build_space(out, repo="https://github.com/example/wapsi")
    assert report.ok, report.problems
    return out, report


def front_matter(readme: str) -> dict[str, str]:
    assert readme.startswith("---\n"), "the YAML block must be the first thing in README.md"
    block = readme.split("---\n", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        if value.strip() and not line.startswith(" "):
            out[key.strip()] = value.strip()
    return out


def test_space_holds_what_the_dockerfile_copies_and_nothing_else(make_space, space):
    out, report = space
    dockerfile = (ROOT / "deploy" / "demo" / "Dockerfile").read_text(encoding="utf-8")
    sources = make_space.copy_sources(dockerfile)
    assert "src" in sources and "data/recordings" in sources and "deploy/demo" in sources
    for src in sources:
        assert (out / src).exists(), f"the Dockerfile copies {src}, but the Space folder has none"
    assert (out / "Dockerfile").read_bytes() == (ROOT / "deploy" / "demo" / "Dockerfile").read_bytes()
    on_disk = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert on_disk == report.files and len(on_disk) > 100
    top = {p.split("/")[0] for p in on_disk}
    assert top == {"Dockerfile", "README.md", ".gitattributes", "LICENSE", "pyproject.toml", "uv.lock", "src", "ui",
                   ".streamlit", "data", "evals", "deploy"}
    for needed in ("src/wapsi/adapters/llm_replay.py", "ui/app.py", "deploy/demo/app.py", "deploy/demo/prepare.py",
                   "data/recordings/demo_prose.json", "data/docs/citations.json", "evals/reports/latest.json",
                   "data/samples/voice/manifest.json", "data/samples/voice/meera_impersonation.wav"):
        assert needed in on_disk, needed
    for f in on_disk:
        assert "__pycache__" not in f and not f.endswith(".pyc") and ".env" not in f.split("/")[-1], f
        assert not f.startswith(("tests/", "editions/", "docs/", "pitch/", "scripts/", "data/generated/",
                                 "data/golden/", "data/docs/raw/", "data/docs/index/")), f
    assert [f for f in on_disk if f.startswith("evals/")] == ["evals/reports/latest.json"]
    assert report.bytes < 40_000_000  # small enough to push in one go


def test_space_card_is_what_spaces_read(make_space, space):
    out, _ = space
    readme = (out / "README.md").read_text(encoding="utf-8")
    meta = front_matter(readme)
    assert meta["sdk"] == "docker" and meta["app_port"] == "7860" and meta["license"] == "mit"
    assert meta["title"] == "Wapsi" and len(meta["short_description"]) <= 60
    body = readme.split("---\n", 2)[2]
    for needle in ("All data here is synthetic", "Recorded run — live AMD endpoint not configured",
                   "WAPSI_LLM_BASE_URL", "?page=case-room&case=CASE-31009260000101", "never by a language model",
                   "https://github.com/example/wapsi", "amount-specific holds"):
        assert needle in body, needle
    assert "Snowflake" not in readme and "Cortex" not in readme
    lfs = (out / ".gitattributes").read_text(encoding="utf-8")
    assert "*.wav filter=lfs diff=lfs merge=lfs -text" in lfs
    # the image builds from the Space's own README: the package metadata names it
    assert 'readme = "README.md"' in (out / "pyproject.toml").read_text(encoding="utf-8")


def test_space_port_matches_the_dockerfile(make_space):
    dockerfile = (ROOT / "deploy" / "demo" / "Dockerfile").read_text(encoding="utf-8")
    assert f"EXPOSE {make_space.APP_PORT}" in dockerfile and f"PORT={make_space.APP_PORT}" in dockerfile
    assert f"app_port: {make_space.APP_PORT}" in make_space.space_readme()


def test_rebuild_needs_force_and_keeps_the_clone(make_space, tmp_path):
    out = tmp_path / "space"
    assert make_space.build_space(out).ok
    (out / ".git").mkdir()
    (out / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (out / "stale.txt").write_text("left over", encoding="utf-8")
    again = make_space.build_space(out)
    assert not again.ok and "--force" in again.problems[0] and (out / "stale.txt").exists()
    forced = make_space.build_space(out, force=True)
    assert forced.ok and not (out / "stale.txt").exists()
    assert (out / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"  # still a clone
    assert not make_space.build_space(ROOT).ok and not make_space.build_space(ROOT.parent).ok  # never the repository


def test_check_mode_writes_nothing(make_space, tmp_path, capsys):
    out = tmp_path / "nothing"
    assert make_space.main(["--out", str(out), "--check"]) == 0
    assert not out.exists() and "no key-shaped strings found" in capsys.readouterr().out
    assert make_space.main(["--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert (out / "Dockerfile").exists() and "hf upload" in text and "WAPSI_LLM_API_KEY" in text
    assert "nothing was uploaded" in text


def copy_repo_slice(dest: Path) -> Path:
    """A small stand-in repository with the layout make_space.py reads (no git: every file counts as tracked)."""
    (dest / "deploy" / "demo").mkdir(parents=True)
    (dest / "deploy" / "demo" / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY --from=uv /uv /bin/\nCOPY pyproject.toml README.md ./\nCOPY src ./src\n"
        "COPY data/docs ./data/docs\nEXPOSE 7860\n", encoding="utf-8")
    (dest / "pyproject.toml").write_text('[project]\nname = "x"\nreadme = "README.md"\n', encoding="utf-8")
    (dest / "README.md").write_text("# the repository readme\n", encoding="utf-8")
    (dest / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (dest / "src" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (dest / "src" / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (dest / "src" / ".env").write_text("WAPSI_LLM_API_KEY=whatever\n", encoding="utf-8")
    for sub in ("raw", "index", "index/text"):
        (dest / "data" / "docs" / sub).mkdir(parents=True)
    (dest / "data" / "docs" / "citations.json").write_text("{}", encoding="utf-8")
    (dest / "data" / "docs" / "raw" / "sop.pdf").write_bytes(b"%PDF")
    (dest / "data" / "docs" / "index" / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    (dest / "data" / "docs" / "index" / "text" / "sop.txt").write_text("full text", encoding="utf-8")
    return dest


def test_licensed_text_and_local_files_stay_out(make_space, tmp_path, monkeypatch):
    monkeypatch.setattr(make_space, "tracked_files", lambda root: None)
    repo = copy_repo_slice(tmp_path / "repo")
    report = make_space.build_space(tmp_path / "space", root=repo)
    assert report.ok, report.problems
    assert report.files == [".gitattributes", "Dockerfile", "README.md", "data/docs/citations.json", "pyproject.toml",
                            "src/pkg/mod.py"]  # no raw documents, no index, no .env, no bytecode
    assert "sdk: docker" in (tmp_path / "space" / "README.md").read_text(encoding="utf-8")  # the card, not the repo's
    private = make_space.build_space(tmp_path / "private", root=repo, with_docs_index=True)
    assert "data/docs/index/chunks.jsonl" in private.files
    assert not [f for f in private.files if f.startswith(("data/docs/raw/", "data/docs/index/text/"))]

    # only tracked files are copied: a stray local file cannot ride along
    monkeypatch.setattr(make_space, "tracked_files", lambda root: {"pyproject.toml", "README.md", "src/pkg/mod.py"})
    partial = make_space.build_space(tmp_path / "tracked-only", root=repo)
    assert not partial.ok and "data/docs" in partial.problems[0]  # the Dockerfile's COPY would fail: say so now


def test_a_key_in_the_sources_stops_the_build(make_space, tmp_path, monkeypatch):
    monkeypatch.setattr(make_space, "tracked_files", lambda root: None)
    repo = copy_repo_slice(tmp_path / "repo")
    planted = "rc-" + "A1b2C3d4" * 4
    (repo / "src" / "pkg" / "settings.py").write_text(f'KEY = "{planted}"\n', encoding="utf-8")
    report = make_space.build_space(tmp_path / "space", root=repo)
    assert not report.ok and report.problems == ["src/pkg/settings.py: looks like a AMD Token Factory key"]
    assert not (tmp_path / "space").exists() and planted not in " ".join(report.problems)

    (repo / "src" / "pkg" / "settings.py").write_text('NOTE = "tok-live-0123456789abcdef"\n', encoding="utf-8")
    monkeypatch.setenv("WAPSI_LLM_API_KEY", "tok-live-0123456789abcdef")  # the real key's literal value
    report = make_space.build_space(tmp_path / "space", root=repo)
    assert report.problems == ["src/pkg/settings.py: contains the value of $WAPSI_LLM_API_KEY"]
    assert make_space.main(["--out", str(tmp_path / "space"), "--check"]) in (0, 1)  # never raises


def test_the_real_sources_hold_no_key_shaped_string(make_space):
    report = make_space.build_space(ROOT.parent / "unused", check=True)
    assert report.ok, report.problems


# --------------------------------------------------------------------------- Streamlit Community Cloud


def test_community_cloud_entry_point_has_its_own_requirements():
    """Community Cloud takes the first dependency file it finds, in the entry point's folder first and uv.lock
    before requirements.txt: so the entry point lives in a folder that has a requirements.txt and no uv.lock."""
    names = {p.name for p in CLOUD.iterdir()}
    assert {"streamlit_app.py", "requirements.txt"} <= names
    assert not names & {"uv.lock", "Pipfile", "environment.yml", "pyproject.toml"}
    assert not (ROOT / "streamlit_app.py").exists()  # a root entry point would get uv.lock (no ui extra)
    reqs = (CLOUD / "requirements.txt").read_text(encoding="utf-8")
    pinned = {ln.split("==")[0].strip().lower() for ln in reqs.splitlines() if "==" in ln and not ln.startswith(" ")}
    assert {"streamlit", "plotly", "pandas", "pyarrow", "pydantic", "jinja2", "httpx", "numpy", "pypdf"} <= pinned
    assert not pinned & {"torch", "transformers", "google-adk", "google-cloud-bigquery", "litellm", "pytest", "ruff"}
    assert "--hash" not in reqs and "-e ." not in reqs and "file://" not in reqs


def test_community_cloud_requirements_match_the_lock():
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    done = subprocess.run([uv, *EXPORT], capture_output=True, text=True, cwd=ROOT, timeout=180)
    if done.returncode != 0:
        pytest.skip(f"uv export failed: {done.stderr[-300:]}")

    def pins(text: str) -> list[str]:
        return sorted(ln.strip() for ln in text.splitlines() if ln and not ln.lstrip().startswith("#"))

    committed = (CLOUD / "requirements.txt").read_text(encoding="utf-8")
    assert pins(committed) == pins(done.stdout), (
        "deploy/streamlit_cloud/requirements.txt is stale; regenerate it:\n  uv " + " ".join(EXPORT)
        + " -o deploy/streamlit_cloud/requirements.txt")
