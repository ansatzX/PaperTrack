import calendar
import re
from datetime import date, timedelta


def parse_iso_date(value: str) -> date:
    """Parse YYYY-MM-DD into a date."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Expected YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Expected YYYY-MM-DD, got {value!r}") from exc


def iter_date_range(start: date, end: date) -> list[date]:
    """Return every date from start through end, inclusive."""
    if start > end:
        raise ValueError("start date must be on or before end date")
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def rolling_refresh_days(anchor: date, refresh_days: int = 30) -> list[date]:
    """Return the production refresh window: anchor-refresh_days through anchor."""
    if refresh_days < 0:
        raise ValueError("refresh_days must be non-negative")
    return iter_date_range(anchor - timedelta(days=refresh_days), anchor)


def month_days(month: str) -> list[date]:
    """Return every day in a YYYY-MM month."""
    try:
        year_text, month_text = month.split("-", 1)
        year = int(year_text)
        month_num = int(month_text)
        if len(year_text) != 4 or len(month_text) != 2:
            raise ValueError
        _, num_days = calendar.monthrange(year, month_num)
    except ValueError as exc:
        raise ValueError(f"Expected YYYY-MM, got {month!r}") from exc
    return [date(year, month_num, day) for day in range(1, num_days + 1)]
