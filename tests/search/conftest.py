import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A fresh temporary directory (plain ``tempfile``; independent of pytest's ``basetemp`` location)."""
    with tempfile.TemporaryDirectory(prefix="wapsi-search-") as d:
        yield Path(d)
