"""UTC persistence with IANA project timezone for local dates (DST-aware)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def parse_utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local_date_for(occurred_at: datetime, tz_name: str) -> date:
    utc = parse_utc(occurred_at)
    return utc.astimezone(ZoneInfo(tz_name)).date()
