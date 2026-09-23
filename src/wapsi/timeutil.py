from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")  # India has no DST


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def to_ist(ts: datetime) -> datetime:
    return ensure_utc(ts).astimezone(IST)


def fmt_ist(ts: datetime, with_time: bool = True) -> str:
    """'16-09-2026 14:05 IST' (DD-MM-YYYY, the format Indian bank notices use)."""
    t = to_ist(ts)
    return t.strftime("%d-%m-%Y %H:%M IST") if with_time else t.strftime("%d-%m-%Y")
