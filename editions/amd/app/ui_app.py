"""Streamlit entry point for the AMD edition.

    uv run streamlit run editions/amd/app/ui_app.py            # WAPSI_DEMO_MODE=1 for the offline demo

Seeds the shared UI (``ui/app.py``) with ``AmdDataSource`` (FileCaseStore + vLLM-ROCm, or FakeLLM in demo mode)
and then runs the shared app unchanged.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st  # noqa: E402

from editions.amd.app.services import get_services  # noqa: E402
from editions.amd.app.ui_source import AmdDataSource  # noqa: E402

DS_KEY = "_wapsi_ds"  # the key ui/app.py reads its data source from


@st.cache_resource(show_spinner=False)
def _services():
    return get_services()


if DS_KEY not in st.session_state:
    st.session_state[DS_KEY] = AmdDataSource(_services())

runpy.run_path(str(ROOT / "ui" / "app.py"), run_name="__main__")
