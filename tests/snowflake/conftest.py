"""Fixtures for the Snowflake edition tests: everything runs offline against ``sf_fakes.SqliteSession``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EDITION = ROOT / "editions" / "snowflake"
for p in (ROOT, ROOT / "src", EDITION / "snowpark", EDITION / "app", Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sf_fakes import SqliteSession  # noqa: E402
from sf_helpers import (  # noqa: E402
    DDL_FILES,
    fake_ai_complete,
    fake_search_preview,
    install_calls,
    showcase_world,
)


@pytest.fixture()
def sf_session(tmp_path) -> SqliteSession:
    """Case tables + RAW_SHOWCASE loaded through deploy.load_world (Parquet -> write_pandas), docs loaded."""
    from editions.snowflake import deploy

    s = SqliteSession(user="ANALYST_A", roles=("WAPSI_ADMIN",))
    s.apply_ddl(*DDL_FILES)
    deploy.load_world(s, showcase_world(tmp_path), "RAW_SHOWCASE", labels=False)
    deploy.load_docs(s)
    s.on(r"SNOWFLAKE\.CORTEX\.SEARCH_PREVIEW", fake_search_preview)
    ai, s.ai_prompts = fake_ai_complete()
    s.on(r"\bAI_COMPLETE\s*\(", ai)
    install_calls(s)
    return s


@pytest.fixture()
def procs():
    import wapsi_sf.procs as procs

    return procs
