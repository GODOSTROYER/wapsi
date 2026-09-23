"""India bank working-day calendar (used for statutory clocks such as the 7-working-day STR deadline).

Non-working days modelled here:

- every Sunday;
- the 2nd and 4th Saturday of every month (Government of India / RBI arrangement for banks since September 2015,
  notified under s.25 of the Negotiable Instruments Act, 1881); 1st, 3rd and 5th Saturdays are working days;
- closed (gazetted) holidays from the DoPT annual holiday O.M. for Central Government administrative offices at
  Delhi / New Delhi (Annexure-I), plus DoPT's ad-hoc declarations and moon-sighting date changes. The dates and
  source URLs live in ``holidays_in.json`` next to this module.

Limitations (read before relying on a due date):

- Bank holidays are declared under the NI Act by the Central **and State** Governments and vary by State (RBI
  publishes a state-wise list). DoPT's O.M. itself says bank holidays follow Department of Financial Services
  instructions. This module is a single national approximation; a production deployment should load the home
  bank's state list instead.
- Years not present in the JSON fall back to the fixed-date national holidays only (26 Jan, 15 Aug, 2 Oct).

Dates are evaluated in IST: a ``datetime`` argument is converted to its IST calendar date first. Returned
datetimes are timezone-aware UTC (store UTC, display IST).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

from wapsi.timeutil import IST, ensure_utc, to_ist

DateLike = date | datetime

_DATA_FILE = "holidays_in.json"


def _to_date(d: DateLike) -> date:
    if isinstance(d, datetime):  # datetime is a subclass of date: check it first
        return to_ist(d).date()
    return d


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files("wapsi.domain").joinpath(_DATA_FILE).read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=1)
def _listed() -> dict[date, str]:
    out: dict[date, list[str]] = {}
    for year in _data()["years"].values():
        for h in year["holidays"]:
            out.setdefault(date.fromisoformat(h["date"]), []).append(h["name"])
    return {d: " / ".join(names) for d, names in out.items()}


def covered_years() -> frozenset[int]:
    """Years with a full published holiday list in ``holidays_in.json``."""
    return frozenset(int(y) for y in _data()["years"])


def sources() -> dict[str, dict[str, str]]:
    """Source id -> {title, url} for the holiday data."""
    return dict(_data()["sources"])


@lru_cache(maxsize=16)
def holidays(year: int) -> dict[date, str]:
    """Listed closed holidays (not weekends) for ``year``: date -> name."""
    if year in covered_years():
        return {d: n for d, n in _listed().items() if d.year == year}
    return {date(year, h["month"], h["day"]): h["name"] for h in _data()["fixed_national_holidays"]}


def holiday_name(d: DateLike) -> str | None:
    """Name of the listed holiday on ``d`` (weekends are not named)."""
    day = _to_date(d)
    return holidays(day.year).get(day)


def is_second_or_fourth_saturday(d: DateLike) -> bool:
    day = _to_date(d)
    return day.weekday() == 5 and (day.day - 1) // 7 + 1 in (2, 4)


def is_bank_holiday(d: DateLike) -> bool:
    """True when banks are closed on ``d``: Sunday, 2nd/4th Saturday, or a listed closed holiday."""
    day = _to_date(d)
    if day.weekday() == 6 or is_second_or_fourth_saturday(day):
        return True
    return day in holidays(day.year)


def is_working_day(d: DateLike) -> bool:
    return not is_bank_holiday(d)


def end_of_day_ist(d: DateLike) -> datetime:
    """23:59:59 IST on the IST date of ``d``, returned in UTC."""
    return ensure_utc(datetime.combine(_to_date(d), time(23, 59, 59), tzinfo=IST))


def next_working_day(d: DateLike) -> date:
    """First working day strictly after ``d``."""
    day = _to_date(d) + timedelta(days=1)
    while is_bank_holiday(day):
        day += timedelta(days=1)
    return day


def add_working_days(start: DateLike, n: int) -> datetime:
    """Due time = 23:59:59 IST at the end of the ``n``-th working day *after* the start date.

    The start day itself is never counted (even if it is a working day). ``n == 0`` gives the end of the start
    date. Returns a UTC-aware datetime.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    day = _to_date(start)
    for _ in range(n):
        day = next_working_day(day)
    return end_of_day_ist(day)


def working_days_between(a: DateLike, b: DateLike) -> int:
    """Number of working days ``d`` with ``date(a) < d <= date(b)`` (IST dates); negative if ``b < a``.

    Inverse of :func:`add_working_days`: ``working_days_between(s, add_working_days(s, n)) == n``.
    """
    da, db = _to_date(a), _to_date(b)
    if db < da:
        return -working_days_between(db, da)
    count = 0
    day = da + timedelta(days=1)
    while day <= db:
        if is_working_day(day):
            count += 1
        day += timedelta(days=1)
    return count
