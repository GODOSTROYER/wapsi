from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.synth.export import write_frames
from wapsi.synth.world import WorldConfig, build_world
from wapsi.timeutil import IST

SHOWCASE_NOW = datetime(2026, 9, 30, tzinfo=IST)
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # the evaluation harness (evals/) lives at the repo root
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def demo_world():
    """(frames, reference cases) of the demo world, seed 7."""
    return build_world(WorldConfig(name="demo", size="demo", seed=7))


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory, demo_world):
    path = tmp_path_factory.mktemp("demo")
    write_frames(demo_world[0], path)
    return path


@pytest.fixture(scope="session")
def hard_world():
    """(frames, reference cases) of the hard world, seed 7 (same config as data/generated/hard)."""
    return build_world(WorldConfig(name="hard", size="hard", seed=7))


@pytest.fixture(scope="session")
def hard_dir(tmp_path_factory, hard_world):
    """The hard world written like ``python -m wapsi.synth --size hard`` (Parquet + golden/)."""
    from wapsi.synth.golden import write_golden

    path = tmp_path_factory.mktemp("hard")
    write_frames(hard_world[0], path)
    write_golden(hard_world[0], hard_world[1], path / "golden", "hard")
    return path


@pytest.fixture(scope="session")
def showcase_world():
    return build_world(WorldConfig(name="showcase", size="showcase"))


@pytest.fixture(scope="session")
def showcase_dir(tmp_path_factory, showcase_world):
    path = tmp_path_factory.mktemp("showcase")
    write_frames(showcase_world[0], path)
    return path


@pytest.fixture(scope="session")
def showcase_memory():
    return sc.showcase_ledger()
