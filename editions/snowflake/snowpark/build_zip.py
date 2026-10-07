"""Build the Snowpark import zips: the portable ``wapsi`` core + ``wapsi_sf`` (+ ``ui`` for Streamlit).

    uv run python editions/snowflake/snowpark/build_zip.py            # dist/snowflake/wapsi.zip, wapsi_app.zip

``wapsi.zip`` is what the stored procedures import (``IMPORTS = ('@WAPSI.APP.CODE/wapsi.zip')``);
``wapsi_app.zip`` adds the shared ``ui`` package and ships next to the Streamlit app's main file. Both are
byte-for-byte reproducible (sorted entries, fixed timestamps), include package data (Jinja templates, the
citation quotes, the holiday calendar) and leave out tests, data, caches and the modules that need packages the
Snowflake Anaconda channel does not provide or that only other editions use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "wapsi"
WAPSI_SF = Path(__file__).resolve().parent / "wapsi_sf"
UI = ROOT / "ui"
DIST = ROOT / "dist" / "snowflake"
ZIP_NAME = "wapsi.zip"
APP_ZIP_NAME = "wapsi_app.zip"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

# Paths inside the zip (posix) that are never shipped.
EXCLUDE_PREFIXES = (
    "wapsi/synth/",  # world generator: local only (numpy-heavy, not needed at runtime)
    "wapsi/adapters/gemini_llm.py",  # GCP / AMD editions
    "wapsi/adapters/firestore_store.py",
    "wapsi/adapters/bigquery_ledger.py",
    "wapsi/adapters/model_armor_guard.py",
    "wapsi/adapters/llm_openai_compat.py",  # httpx client; Snowflake uses CortexLLM
    "wapsi/search/fetch.py",  # network fetcher for the docs corpus
    "wapsi/search/build.py",  # index builder (imports fetch)
    "wapsi/search/check_citations.py",  # corpus maintenance CLI (imports build)
)
EXCLUDE_PARTS = {"__pycache__", "tests", "data", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".md")
# Snowflake Anaconda-channel packages the procedures need (RUNTIME_VERSION = '3.12'); keep in sync with
# sql/30_procs.sql and app/environment.yml (tested).
PROC_PACKAGES = ("snowflake-snowpark-python", "pydantic", "pandas", "numpy", "jinja2", "pyarrow")
APP_PACKAGES = ("streamlit", "snowflake-snowpark-python", "pydantic", "pandas", "numpy", "jinja2", "plotly")


@dataclass(frozen=True)
class ZipReport:
    path: Path
    files: tuple[str, ...]
    size: int
    sha256: str

    def summary(self) -> str:
        return f"{self.path.name}: {len(self.files)} files, {self.size:,} bytes, sha256 {self.sha256[:12]}"


def _excluded(arc: str) -> bool:
    parts = arc.split("/")
    if any(p in EXCLUDE_PARTS for p in parts[:-1]):
        return True
    if arc.endswith(EXCLUDE_SUFFIXES):
        return True
    return any(arc == p or arc.startswith(p) for p in EXCLUDE_PREFIXES)


def collect(include_ui: bool = False) -> list[tuple[str, Path]]:
    """(arcname, source) pairs, sorted by arcname."""
    roots = [("wapsi", SRC), ("wapsi_sf", WAPSI_SF)] + ([("ui", UI)] if include_ui else [])
    out: dict[str, Path] = {}
    for top, base in roots:
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            arc = f"{top}/{f.relative_to(base).as_posix()}"
            if not _excluded(arc):
                out[arc] = f
    return sorted(out.items())


def build_zip(out: Path | str | None = None, *, include_ui: bool = False) -> ZipReport:
    target = Path(out) if out is not None else DIST / (APP_ZIP_NAME if include_ui else ZIP_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = collect(include_ui)
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("_wapsi_version", SRC / "__init__.py")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    build_info = json.dumps({"wapsi": mod.__version__, "files": len(entries) + 1, "ui": include_ui,
                             "packages": list(APP_PACKAGES if include_ui else PROC_PACKAGES)}, sort_keys=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc, src in entries:
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, src.read_bytes())
        info = zipfile.ZipInfo("wapsi_sf/BUILD.json", date_time=FIXED_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, build_info)
    data = target.read_bytes()
    files = tuple(arc for arc, _ in entries) + ("wapsi_sf/BUILD.json",)
    return ZipReport(target, files, len(data), hashlib.sha256(data).hexdigest())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(DIST))
    ap.add_argument("--list", action="store_true", help="print every file in the zips")
    args = ap.parse_args(argv)
    for include_ui, name in ((False, ZIP_NAME), (True, APP_ZIP_NAME)):
        rep = build_zip(Path(args.out_dir) / name, include_ui=include_ui)
        print(rep.summary())
        if args.list:
            for f in rep.files:
                print("  " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
