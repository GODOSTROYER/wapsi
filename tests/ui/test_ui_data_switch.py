"""The UI's data-source switch: showcase world (default), the local demo world, or a parquet world by path."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui.data_source as data_source
from ui.data_source import (
    DATA_SOURCE_ENV,
    MAX_CASES_ENV,
    SHOWCASE_SOURCE,
    SOURCE_WORLD,
    available_sources,
    load_source,
    max_world_cases,
    resolve_source,
    world_case_groups,
)
from ui.views.common import PAGES
from wapsi.adapters.parquet_ledger import ParquetLedger
from wapsi.fixtures import showcase as sc
from wapsi.synth.export import export_ledger

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
SHOWCASE_CASES = {"CASE-31009260000101", "CASE-31109260000102"}


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory) -> Path:
    """The showcase ledger written as a parquet world (``python -m wapsi.synth`` layout)."""
    path = tmp_path_factory.mktemp("worlds") / "parquet-world"
    export_ledger(sc.showcase_ledger(), path, name="parquet-world")
    return path


@pytest.fixture
def generated(tmp_path, monkeypatch, world_dir) -> Path:
    """A private data/generated + data/golden with a 'demo' world (the showcase ledger)."""
    gen, golden = tmp_path / "generated", tmp_path / "golden"
    export_ledger(sc.showcase_ledger(), gen / "demo", name="demo")
    monkeypatch.setattr(data_source, "GENERATED_DIR", gen.resolve())
    monkeypatch.setattr(data_source, "GOLDEN_DIR", golden.resolve())
    return gen


def test_resolve_source(monkeypatch, world_dir, generated):
    monkeypatch.delenv(DATA_SOURCE_ENV, raising=False)
    assert resolve_source() == SHOWCASE_SOURCE
    assert resolve_source("Showcase") == SHOWCASE_SOURCE
    demo = resolve_source("demo")
    assert (demo.kind, demo.key, demo.label) == ("world", "demo", "Demo world (synthetic)")
    assert demo.path == (generated / "demo").resolve()
    by_path = resolve_source(str(world_dir))
    assert by_path.kind == "world" and by_path.label == "Synthetic world · parquet-world"
    monkeypatch.setenv(DATA_SOURCE_ENV, str(world_dir))
    assert resolve_source() == by_path
    with pytest.raises(ValueError, match="no synthetic world"):
        resolve_source(str(world_dir.parent / "missing"))


def test_available_sources(world_dir, generated):
    demo = resolve_source("demo")
    assert available_sources() == [SHOWCASE_SOURCE, demo]
    same_world = resolve_source(str(generated / "demo"))  # another key for the demo world replaces its entry
    assert [s.key for s in available_sources(same_world)] == ["showcase", str(generated / "demo")]
    other = resolve_source(str(world_dir))
    assert available_sources(other)[-1] == other and len(available_sources(other)) == 3


def test_world_case_groups(world_dir, generated):
    ledger = ParquetLedger(world_dir)
    assert world_case_groups(ledger, world_dir) == [[sc.N1], [sc.N2, sc.N3]]  # no golden set: group_complaints
    assert world_case_groups(ledger, world_dir, limit=1) == [[sc.N1]]
    # a generated world uses the committed golden set of the same name
    demo = generated / "demo"
    golden = data_source.GOLDEN_DIR / "demo" / "cases.jsonl"
    golden.parent.mkdir(parents=True)
    golden.write_text(json.dumps({"case_id": "C", "ncrp_ids": [sc.N3, sc.N2]}) + "\n", encoding="utf-8")
    assert world_case_groups(ParquetLedger(demo), demo.resolve()) == [[sc.N2, sc.N3]]
    assert world_case_groups(ledger, world_dir) == [[sc.N1], [sc.N2, sc.N3]]  # not for a world elsewhere


def test_max_world_cases(monkeypatch):
    monkeypatch.delenv(MAX_CASES_ENV, raising=False)
    assert max_world_cases() == 12
    monkeypatch.setenv(MAX_CASES_ENV, "0")
    assert max_world_cases() is None
    monkeypatch.setenv(MAX_CASES_ENV, "x")
    assert max_world_cases() == 12


def test_load_source_runs_a_world_through_the_pipeline(world_dir, monkeypatch):
    monkeypatch.setenv(MAX_CASES_ENV, "0")
    bundles, ledger, source = load_source(resolve_source(str(world_dir)))
    assert source == SOURCE_WORLD and isinstance(ledger, ParquetLedger)
    assert {b.case.case_id for b in bundles} == SHOWCASE_CASES
    plan = next(b.plan for b in bundles if b.case.case_id == "CASE-31009260000101")
    assert (plan.total_hold, plan.total_forward_hold, plan.total_recoverable) == (142_848.0, 96_000.0, 334_848.0)
    assert all(b.drafts and b.clocks for b in bundles)


def _text(at: AppTest) -> str:
    parts = [html.unescape(m.value) for m in at.markdown]
    parts += [str(x.value) for x in (*at.success, *at.info, *at.warning, *at.error)]
    return "\n".join(parts)


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert "This page couldn't load" not in _text(at)


@pytest.fixture
def app_env(monkeypatch, world_dir):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_NOW", "2026-09-16T11:00:00+05:30")
    monkeypatch.setenv(DATA_SOURCE_ENV, str(world_dir))
    monkeypatch.setenv(MAX_CASES_ENV, "0")
    monkeypatch.delenv("WAPSI_EVAL_REPORT", raising=False)


def test_app_shows_a_parquet_world_and_switches_back_to_the_showcase(app_env, world_dir):
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    _ok(at)
    assert "Synthetic world · parquet-world · 2 cases processed by the live pipeline" in _text(at)
    switch = at.selectbox(key="data_source")
    assert switch.value == str(world_dir)
    for page in PAGES:
        at.radio(key="page").set_value(page).run()
        _ok(at)
    at.selectbox(key="data_source").set_value("showcase").run()
    _ok(at)
    text = _text(at)
    assert "Showcase ledger" in text or "Showcase fixtures" in text
    assert "Synthetic world" not in text
    assert at.session_state["data_source"] == "showcase" and at.session_state["case_id"] in SHOWCASE_CASES


def test_an_unknown_source_falls_back_to_the_showcase(monkeypatch, app_env, world_dir):
    monkeypatch.setenv(DATA_SOURCE_ENV, str(world_dir.parent / "missing"))
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Showing the showcase world instead" in w.value for w in at.warning)
    assert at.session_state["data_source"] == "showcase"
