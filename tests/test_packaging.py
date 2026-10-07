"""The package works without the source tree's files beside it: imported from a zip of ``src/wapsi``, the Jinja
templates come through ``PackageLoader`` and the citation quotes from the packaged ``wapsi/_data/citations.json``."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _zip_package(dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted((SRC / "wapsi").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                zf.write(path, path.relative_to(SRC).as_posix())
    return dest


def test_templates_and_quotes_load_from_a_zipped_package(tmp_path):
    zip_path = _zip_package(tmp_path / "wapsi.zip")
    n_templates = len(list((SRC / "wapsi" / "draft" / "templates").glob("*.j2")))
    code = textwrap.dedent(f"""
        import os, sys
        _drop = {{os.path.normcase(os.path.abspath(p)) for p in ({str(SRC)!r}, {str(ROOT)!r})}}
        sys.path[:] = [p for p in sys.path if p and os.path.normcase(os.path.abspath(p)) not in _drop]
        sys.path.insert(0, {str(zip_path)!r})

        import wapsi
        assert ".zip" in wapsi.__file__, wapsi.__file__
        from jinja2 import PackageLoader
        from wapsi import citations
        from wapsi.adapters.llm_fake import FakeLLM
        from wapsi.draft import render
        from wapsi.fixtures.showcase import N1, SHOWCASE_1, showcase_ledger
        from wapsi.pipeline.run import process_case

        assert not render.TEMPLATE_DIR.is_dir(), render.TEMPLATE_DIR
        assert isinstance(render._loader(), PackageLoader), render._loader()
        names = [n for n in render.environment().list_templates() if n.endswith(".j2")]
        assert len(names) == {n_templates}, names
        assert not citations.CITATIONS_JSON.exists(), citations.CITATIONS_JSON
        quotes = citations._load_quotes()
        assert quotes and sum(1 for c in citations.registry().values() if c.quote) > 10, len(quotes)

        b = process_case(showcase_ledger(), [N1], llm=FakeLLM())
        assert b.plan.total_hold == SHOWCASE_1["total_hold"]
        assert b.drafts and all(r.passed for r in b.verifications.values())
        print("ZIP-OK", len(names), len(quotes), len(b.drafts))
    """)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "WAPSI_CITATIONS_JSON")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, env=env,
                       timeout=240)
    assert r.returncode == 0, r.stderr[-3000:]
    assert "ZIP-OK" in r.stdout, r.stdout
