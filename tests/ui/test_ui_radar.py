"""Regulatory Radar page: curated rows, per-case impact, unverified rows hidden, missing registry."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.views.common import CASE_PAGES, PAGES
from wapsi.domain.regradar import REG_RADAR_JSON

APP = Path(__file__).resolve().parents[2] / "ui" / "app.py"
CASE_1 = "CASE-31009260000101"
PAGE = "Regulatory Radar"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WAPSI_UI_RAISE", "1")
    monkeypatch.setenv("WAPSI_UI_SOURCE", "fixtures")
    monkeypatch.setenv("WAPSI_NOW", "2026-11-20T11:00:00+05:30")  # day 70 of the showcase-1 holds
    monkeypatch.delenv("WAPSI_REG_RADAR_JSON", raising=False)


def _run(role: str = "analyst", case_id: str = CASE_1) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["case_id"] = case_id
    at.session_state["role"] = role
    at.session_state["page"] = PAGE
    return at.run()


def _text(at: AppTest) -> str:
    return "\n".join(html.unescape(m.value) for m in at.markdown)


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert "This page couldn't load" not in _text(at)
    assert not at.warning


def test_page_is_registered_in_the_nav():
    assert PAGE in PAGES and PAGE in CASE_PAGES
    assert PAGES.index(PAGE) == PAGES.index("Clocks") + 1


def test_radar_shows_rows_and_what_changes_for_the_case():
    at = _run()
    _ok(at)
    text = _text(at)
    assert "Regulatory radar" in text
    assert "What would change for this case" in text
    assert "5 of 5 holds are older than the RBI DRAFT's 60-day maximum" in text
    assert "HB01-2000000001: ₹4,800.00 held as a bona fide recipient" in text
    assert "DRAFT · not in force" in text
    assert "₹1,42,848.00" in text
    assert "Needs review" in text
    labels = [e.label for e in at.expander]
    assert labels[0].startswith("RR-01") and any(lab.startswith("RR-11") for lab in labels)
    assert "02-10-2026" in text  # draft comment deadline, from the registry


def test_auditor_view_masks_names():
    at = _run(role="auditor")
    _ok(at)
    assert "Meera Joshi" not in _text(at)


def test_unverified_rows_are_hidden_until_asked(monkeypatch, workdir):
    data = json.loads(REG_RADAR_JSON.read_text(encoding="utf-8"))
    data["clauses"][1]["verification"]["status"] = "unverified"  # RR-02
    path = workdir / "reg_radar.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("WAPSI_REG_RADAR_JSON", str(path))
    at = _run()
    _ok(at)
    assert not any(e.label.startswith("RR-02") for e in at.expander)
    assert "1 unverified hidden" in _text(at)
    assert "60-day maximum" not in _text(at)
    at.checkbox(key="radar_unverified").check().run()
    _ok(at)
    assert any(e.label.startswith("RR-02") for e in at.expander)
    assert "Unverified · hidden by default" in _text(at)


def test_missing_registry_shows_an_empty_state(monkeypatch, workdir):
    monkeypatch.setenv("WAPSI_REG_RADAR_JSON", str(workdir / "absent.json"))
    at = _run()
    _ok(at)
    assert "No regulatory radar yet" in _text(at)
