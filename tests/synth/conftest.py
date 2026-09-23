from __future__ import annotations

from datetime import datetime

import pytest

from wapsi.fixtures import showcase as sc
from wapsi.synth.export import write_frames
from wapsi.synth.world import WorldConfig, build_world
from wapsi.timeutil import IST

SHOWCASE_NOW = datetime(2026, 9, 30, tzinfo=IST)


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
