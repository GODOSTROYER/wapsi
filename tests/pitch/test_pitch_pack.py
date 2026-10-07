"""The pitch pack (pitch/) cites only numbers that trace to repository files, and stays inside form limits."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    name = "pitch_check_numbers"
    spec = importlib.util.spec_from_file_location(name, ROOT / "pitch" / "check_numbers.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclasses look the module up while the class is created
    spec.loader.exec_module(module)
    return module


check = _load_checker()


def test_every_number_matches_its_source():
    rows = check.parse_rows()
    assert len(rows) > 100
    errors, resolved = check.check_numbers(rows)
    assert errors == []
    # the showcase headline numbers the whole repository is built around
    assert resolved["sc1_total_hold"] == 142848.0
    assert resolved["sc1_forward"] == 96000.0
    assert resolved["sc1_recoverable"] == 334848.0
    assert resolved["sop_restored_pct_check"] == pytest.approx(2.18, abs=0.005)


def test_every_money_percent_and_memory_figure_is_registered():
    assert check.check_tokens() == []


def test_form_limits_hold_and_are_stated():
    errors, found = check.check_limits()
    assert errors == []
    limits = {(Path(path).as_posix(), limit) for path, _, limit in found}
    assert ("pitch/aibc/SUBMISSION_FIELDS.md", 1000) in limits
    assert ("pitch/amd/LABLAB_FIELDS.md", 50) in limits
    assert ("pitch/amd/LABLAB_FIELDS.md", 255) in limits


def test_checker_catches_a_wrong_value():
    row = check.Row("x", "₹1,42,848", "142849", "src/wapsi/fixtures/showcase.py", 'py:SHOWCASE_1["total_hold"]')
    errors, _ = check.check_numbers([row])
    assert errors and "142849" in errors[0]


def test_checker_catches_an_unregistered_amount(tmp_path, monkeypatch):
    page = tmp_path / "pitch"
    page.mkdir()
    (page / "NUMBERS.md").write_text("| ID | Pitched as | Value | Source | Locator |\n|---|---|---|---|---|\n"
                                     "| a | ₹1,42,848 held | 142848 | derived | `calc:142848` |\n", encoding="utf-8")
    (page / "slide.md").write_text("We hold ₹1,42,848 and recover ₹9,99,999.", encoding="utf-8")
    monkeypatch.setattr(check, "PITCH", page)
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "NUMBERS_MD", page / "NUMBERS.md")
    errors = check.check_tokens()
    assert len(errors) == 1 and "₹9,99,999" in errors[0]


def test_safe_eval_refuses_code():
    with pytest.raises(ValueError):
        check.safe_eval("__import__('os').getcwd()", {})
    with pytest.raises(ValueError):
        check.safe_eval("SHOWCASE_1.__class__", {"SHOWCASE_1": {}})
