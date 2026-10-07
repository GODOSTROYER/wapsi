"""Streamlit entry point for the GCP edition's UI service.

    uv run streamlit run editions/gcp/ui_app.py

Seeds the shared UI (``ui/app.py``) with ``CaseStoreDataSource`` (Firestore + BigQuery on Cloud Run; in-memory
showcase in demo mode, plus one offline ADK agent run whose traces the Case Room's Agent trace tab shows) and then
runs the shared app unchanged.
"""

from __future__ import annotations

import runpy
import sys
from contextlib import nullcontext
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
    services = _services()
    # demo mode, first session on an empty store: seeding runs the offline ADK agent once (about 10-30 s on a
    # laptop, mostly ADK's one-time start-up); say so instead of showing a blank page
    first = services.settings.demo_mode and not services.store.list_cases()
    with st.spinner("Preparing the demo: loading the showcase cases and recording one offline agent run for the "
                    "Agent trace tab (first start only)…", show_time=True) if first else nullcontext():
        st.session_state[DS_KEY] = CaseStoreDataSource(services, seed_agent_trace=True)

runpy.run_path(str(ROOT / "ui" / "app.py"), run_name="__main__")
