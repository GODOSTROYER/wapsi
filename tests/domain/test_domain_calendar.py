from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from wapsi.domain import calendar as cal
from wapsi.timeutil import IST


def eod(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 23, 59, 59, tzinfo=IST)


@pytest.mark.parametrize(
    "d,closed",
    [
        (date(2026, 9, 5), False),  # 1st Saturday
        (date(2026, 9, 12), True),  # 2nd Saturday
        (date(2026, 9, 19), False),  # 3rd Saturday
        (date(2026, 9, 26), True),  # 4th Saturday
        (date(2026, 8, 29), False),  # 5th Saturday
        (date(2026, 8, 8), True),  # 2nd Saturday
        (date(2026, 9, 13), True),  # Sunday
        (date(2026, 9, 14), False),  # Monday
    ],
)
def test_weekends(d, closed):
    assert cal.is_bank_holiday(d) is closed
    assert cal.is_working_day(d) is not closed


@pytest.mark.parametrize(
    "d,name",
    [
        (date(2026, 1, 26), "Republic Day"),
        (date(2026, 10, 2), "Mahatma Gandhi's Birthday"),
        (date(2026, 11, 24), "Guru Nanak's Birthday"),
        (date(2026, 4, 14), "Birthday of Dr. B.R. Ambedkar"),  # declared by DoPT O.M. of 09.04.2026
        (date(2026, 5, 28), "Id-ul-Zuha (Bakrid)"),  # moved from 27.05.2026
        (date(2027, 1, 26), "Republic Day"),
        (date(2027, 10, 29), "Diwali (Deepavali)"),
    ],
)
def test_listed_holidays(d, name):
    assert cal.is_bank_holiday(d)
    assert cal.holiday_name(d) == name


def test_holiday_details():
    assert not cal.is_bank_holiday(date(2026, 5, 27))  # Bakrid moved to the 28th
    assert cal.holiday_name(date(2026, 9, 12)) is None  # weekends are not named
    assert "Independence Day" in cal.holiday_name(date(2027, 8, 15))
    assert "Id-e-Milad" in cal.holiday_name(date(2027, 8, 15))
    assert len(cal.holidays(2026)) == 18 and len(cal.holidays(2027)) == 16  # 2027 has two on 15 Aug
    assert cal.covered_years() == {2026, 2027}


def test_uncovered_year_falls_back_to_fixed_national_holidays():
    assert set(cal.holidays(2028)) == {date(2028, 1, 26), date(2028, 8, 15), date(2028, 10, 2)}
    assert cal.is_bank_holiday(date(2028, 1, 26))
    assert cal.is_working_day(date(2028, 3, 1))


def test_sources_are_official():
    urls = [s["url"] for s in cal.sources().values()]
    assert any(u.startswith("https://dopt.gov.in/") and "2026" in u for u in urls)
    assert any(u.startswith("https://dopt.gov.in/") and "2027" in u for u in urls)


def test_datetimes_are_read_in_ist():
    # 20:00 UTC on Fri 11-09 is 01:30 IST on Sat 12-09 (2nd Saturday)
    assert cal.is_bank_holiday(datetime(2026, 9, 11, 20, 0, tzinfo=UTC))
    assert cal.is_working_day(datetime(2026, 9, 11, 18, 0, tzinfo=UTC))  # 23:30 IST Friday


def test_add_working_days_across_weekend():
    start = datetime(2026, 9, 11, 16, 25, tzinfo=IST)  # Friday; Sat 12 is the 2nd Saturday, Sun 13
    due = cal.add_working_days(start, 1)
    assert due == eod(2026, 9, 14)
    assert due.utcoffset().total_seconds() == 0  # returned in UTC
    assert cal.add_working_days(start, 7) == eod(2026, 9, 21)


def test_add_working_days_counts_third_saturday_and_skips_holiday():
    assert cal.add_working_days(datetime(2026, 9, 18, 10, 0, tzinfo=IST), 1) == eod(2026, 9, 19)
    assert cal.add_working_days(datetime(2026, 9, 18, 10, 0, tzinfo=IST), 2) == eod(2026, 9, 21)
    # Fri 02-10 is Gandhi Jayanti, Sat 03-10 is the 1st Saturday
    assert cal.add_working_days(datetime(2026, 10, 1, 10, 0, tzinfo=IST), 1) == eod(2026, 10, 3)
    # Diwali 08-11 is a Sunday anyway; Sat 14-11 is the 2nd Saturday
    assert cal.add_working_days(date(2026, 11, 13), 1) == eod(2026, 11, 16)


def test_str_deadline_for_showcase_1():
    assert cal.add_working_days(datetime(2026, 9, 10, 13, 45, tzinfo=IST), 7) == eod(2026, 9, 19)


def test_start_day_not_counted_even_when_it_is_a_holiday():
    assert cal.add_working_days(date(2026, 9, 13), 1) == eod(2026, 9, 14)  # Sunday start
    assert cal.add_working_days(date(2026, 9, 14), 0) == eod(2026, 9, 14)
    with pytest.raises(ValueError):
        cal.add_working_days(date(2026, 9, 14), -1)


def test_working_days_between_is_inverse():
    assert cal.working_days_between(date(2026, 9, 11), date(2026, 9, 14)) == 1
    assert cal.working_days_between(date(2026, 9, 14), date(2026, 9, 11)) == -1
    assert cal.working_days_between(date(2026, 9, 14), date(2026, 9, 14)) == 0
    start = datetime(2026, 12, 20, 9, 0, tzinfo=IST)
    for n in range(0, 30):
        assert cal.working_days_between(start, cal.add_working_days(start, n)) == n


def test_next_working_day():
    # Fri 25-12 is Christmas, Sat 26-12 is the 4th Saturday, then Sunday
    assert cal.next_working_day(date(2026, 12, 24)) == date(2026, 12, 28)
