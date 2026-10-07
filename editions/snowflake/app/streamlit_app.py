"""Streamlit in Snowflake entry point (warehouse runtime) for the Wapsi case room.

Deployed by ``uv run python -m editions.snowflake.deploy --step app`` to ``@WAPSI.APP.SIS_STAGE/wapsi`` together
with ``sf_data_source.py``, ``environment.yml`` and ``wapsi_app.zip`` (the portable core, ``wapsi_sf`` and the
shared ``ui`` package). It seeds the shared UI with ``SnowflakeDataSource`` and runs ``ui/app.py`` unchanged.

Local preview against the same account (reads WAPSI_SF_CONNECTION, default VJ22713):
    uv run streamlit run editions/snowflake/app/streamlit_app.py
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_BUNDLE = HERE / "wapsi_app.zip"
if _BUNDLE.exists():  # inside Snowflake: import the core, wapsi_sf and ui from the zip
    sys.path.insert(0, str(_BUNDLE))
else:  # local checkout
    _ROOT = HERE.parents[2]
    for p in (_ROOT, _ROOT / "src", _ROOT / "editions" / "snowflake" / "snowpark"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import streamlit as st  # noqa: E402

DS_KEY = "_wapsi_ds"  # the key ui/app.py reads its data source from


def _session():
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()
    except Exception:  # noqa: BLE001 - not inside Snowflake: open the named connection
        from snowflake.snowpark import Session

        name = os.environ.get("WAPSI_SF_CONNECTION", "VJ22713")
        return Session.builder.config("connection_name", name).create()


@st.cache_resource(show_spinner=False)
def _cached_session():
    return _session()


def _latest_eval_report(session) -> None:
    """Expose the newest APP.EVAL_RUNS report to the KPI page (it reads WAPSI_EVAL_REPORT)."""
    if os.environ.get("WAPSI_EVAL_REPORT"):
        return
    try:
        rows = session.sql("SELECT report FROM WAPSI.APP.EVAL_RUNS ORDER BY generated_at DESC LIMIT 1").collect()
    except Exception:  # noqa: BLE001 - KPI page shows its empty state
        return
    if not rows:
        return
    raw = rows[0][0]
    report = json.loads(raw) if isinstance(raw, str) else raw
    path = Path(tempfile.gettempdir()) / "wapsi_eval_latest.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    os.environ["WAPSI_EVAL_REPORT"] = str(path)


session = _cached_session()
if DS_KEY not in st.session_state:
    from sf_data_source import SnowflakeDataSource

    st.session_state[DS_KEY] = SnowflakeDataSource(session)
    _latest_eval_report(session)

runpy.run_module("ui.app", run_name="__main__")
