from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # editions.amd.app.file_store, ui.*
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def ledger():
    from wapsi.fixtures import showcase as sc

    return sc.showcase_ledger()
