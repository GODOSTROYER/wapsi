"""The demo model's clock summary reads the provisional flag from ``Clock.notes`` (``subject`` is an id only)."""

from __future__ import annotations

from editions.gcp.app.demo_model import _summ_clocks
from wapsi.domain.clocks import HOLD_PROVISIONAL_NOTE, LAPSE_START_NOTE, join_notes


def _clock(kind: str, subject: str | None, notes: str) -> dict:
    return {"kind": kind, "subject": subject, "due": "09 Dec 2026, 23:59 IST", "status": "OPEN", "notes": notes}


def test_summary_flags_provisional_clocks_from_notes():
    out = _summ_clocks({
        "case_id": "CASE-1",
        "as_of": "29 Sep 2026, 10:00 IST",
        "clocks": [
            _clock("HOLD_LAPSE", "HB01-1", join_notes(HOLD_PROVISIONAL_NOTE, LAPSE_START_NOTE)),
            _clock("HOLD_LAPSE", "HB01-2", LAPSE_START_NOTE),
            _clock("BANK_DATA", None, ""),
        ],
    })
    assert "HOLD_LAPSE (HB01-1, provisional): due" in out
    assert "HOLD_LAPSE (HB01-2): due" in out
    assert "BANK_DATA: due" in out
