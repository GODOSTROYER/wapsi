"""Snowpark import zips: reproducible, complete (package data included), lean (tests / data / other editions
excluded), and runnable from the zip alone, the way Snowflake imports them."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
from build_zip import APP_ZIP_NAME, ZIP_NAME, build_zip, collect

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def zips(tmp_path_factory):
    d = tmp_path_factory.mktemp("zips")
    return build_zip(d / ZIP_NAME), build_zip(d / APP_ZIP_NAME, include_ui=True)


def test_zips_are_reproducible(zips, tmp_path):
    again = build_zip(tmp_path / ZIP_NAME)
    assert again.sha256 == zips[0].sha256 and again.files == zips[0].files


def test_zip_includes_the_core_package_data_and_handlers(zips):
    files = set(zips[0].files)
    for f in ("wapsi/__init__.py", "wapsi/models.py", "wapsi/graph/trace.py", "wapsi/pipeline/run.py",
              "wapsi/draft/templates/str.j2", "wapsi/draft/templates/hold_instruction.j2",
              "wapsi/_data/citations.json", "wapsi/domain/holidays_in.json",
              "wapsi/adapters/snowflake_ledger.py", "wapsi/adapters/cortex_llm.py", "wapsi/adapters/llm_fake.py",
              "wapsi/fixtures/showcase.py", "wapsi_sf/procs.py", "wapsi_sf/casestore.py", "wapsi_sf/docsearch.py",
              "wapsi_sf/BUILD.json"):
        assert f in files, f


def test_zip_excludes_tests_data_caches_and_other_editions(zips):
    for f in zips[0].files + zips[1].files:
        parts = f.split("/")
        assert parts[0] in ("wapsi", "wapsi_sf", "ui"), f
        assert not {"tests", "data", "__pycache__"} & set(parts[:-1]), f
        assert not f.endswith((".pyc", ".md")), f
        assert not f.startswith(("wapsi/synth/", "wapsi/adapters/gemini_llm", "wapsi/adapters/llm_openai_compat",
                                 "wapsi/adapters/bigquery_ledger", "wapsi/adapters/firestore_store",
                                 "wapsi/search/fetch")), f
    assert not any(f.startswith("ui/") for f in zips[0].files)
    assert "ui/app.py" in zips[1].files and "ui/components/trail_dot.py" in zips[1].files


def test_collect_matches_zip_listing(zips):
    assert [a for a, _ in collect()] + ["wapsi_sf/BUILD.json"] == list(zips[0].files)
    with zipfile.ZipFile(zips[0].path) as zf:
        assert zf.namelist() == list(zips[0].files)
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in zf.infolist())


def _run_from_zip(zip_path: Path, code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run ``code`` with only the zip providing wapsi / wapsi_sf (the repo's src and root removed from sys.path)."""
    prelude = textwrap.dedent(f"""
        import os, sys
        _drop = {{os.path.normcase(os.path.abspath(p)) for p in ({str(ROOT / 'src')!r}, {str(ROOT)!r},
                 {str(ROOT / 'editions' / 'snowflake' / 'snowpark')!r})}}
        sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.abspath(p)) not in _drop]
        sys.path.insert(0, {str(zip_path)!r})
    """)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "WAPSI_CITATIONS_JSON")}
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)], capture_output=True, text=True,
                          cwd=tmp_path, env=env, timeout=240)


def test_the_core_runs_from_the_zip_alone(zips, tmp_path):
    r = _run_from_zip(zips[0].path, """
        import wapsi, wapsi_sf.procs as procs
        assert ".zip" in wapsi.__file__, wapsi.__file__
        from wapsi.adapters.llm_fake import FakeLLM
        from wapsi.citations import registry
        from wapsi.fixtures.showcase import N1, SHOWCASE_1, showcase_ledger
        from wapsi.pipeline.run import process_case
        v = procs.version_proc(None)
        assert v["matches_fixture"], v
        b = process_case(showcase_ledger(), [N1], llm=FakeLLM())   # Jinja templates + holiday calendar from the zip
        assert b.plan.total_hold == SHOWCASE_1["total_hold"]
        assert b.drafts and all(r.passed for r in b.verifications.values())
        assert sum(1 for c in registry().values() if c.quote) > 10   # quotes packaged in wapsi/_data
        print("ZIP-OK", len(b.drafts))
    """, tmp_path)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "ZIP-OK" in r.stdout


def test_every_module_in_the_zip_imports(zips, tmp_path):
    r = _run_from_zip(zips[0].path, """
        import importlib, zipfile, sys
        names = [n[:-3].replace("/", ".").removesuffix(".__init__") for n in zipfile.ZipFile(sys.path[0]).namelist()
                 if n.endswith(".py")]
        bad = []
        for n in names:
            try:
                importlib.import_module(n)
            except Exception as exc:
                bad.append(f"{n}: {type(exc).__name__}: {exc}")
        print("\\n".join(bad) or "ALL-IMPORT")
    """, tmp_path)
    assert r.returncode == 0, r.stderr[-3000:]
    assert r.stdout.strip() == "ALL-IMPORT", r.stdout
