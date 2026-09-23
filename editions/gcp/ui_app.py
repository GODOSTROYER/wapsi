"""Streamlit entry point for the GCP edition's UI service.

    uv run streamlit run editions/gcp/ui_app.py

Seeds the shared UI (``ui/app.py``) with ``CaseStoreDataSource`` (Firestore + BigQuery on Cloud Run; in-memory
showcase in demo mode) and then runs the shared app unchanged.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from editions.gcp.app.services import get_services  # noqa: E402
from editions.gcp.app.ui_source import CaseStoreDataSource  # noqa: E402

DS_KEY = "_wapsi_ds"  # the key ui/app.py reads its data source from


@st.cache_resource(show_spinner=False)
def _services():
    return get_services()


if DS_KEY not in st.session_state:
    st.session_state[DS_KEY] = CaseStoreDataSource(_services())

runpy.run_path(str(ROOT / "ui" / "app.py"), run_name="__main__")
