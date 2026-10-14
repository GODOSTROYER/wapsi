"""Streamlit Community Cloud entry point for the public demo.

Deploy with **Main file path** ``deploy/streamlit_cloud/streamlit_app.py`` (not a file at the repository root).
Community Cloud looks for dependencies in the entry point's own folder first, so it installs the
``requirements.txt`` next to this file; from the repository root it would pick ``uv.lock`` instead and install the
project without the ``ui`` extra (no plotly).

    uv export --frozen --no-dev --extra ui --extra docs --no-hashes --no-emit-project \
        --format requirements-txt -o deploy/streamlit_cloud/requirements.txt       # after any dependency change

The model endpoint goes into the app's **Secrets** (Settings -> Secrets), as top-level TOML keys, which Community
Cloud also exposes as environment variables::

    WAPSI_LLM_BASE_URL = "https://developer.amd.com.cn/radeon/api/v1"
    WAPSI_LLM_API_KEY = "<your key>"
    WAPSI_LLM_MODEL = "Gemma-4-31B-IT"

Without them the app runs the recorded run and says so. Guide: ``deploy/DEMO_HOSTING.md``.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# a small shared host: fewer demo-world cases are processed at start-up than the app's default of 12
os.environ.setdefault("WAPSI_UI_MAX_CASES", "8")

runpy.run_path(str(ROOT / "deploy" / "demo" / "app.py"), run_name="__main__")
