from datetime import date

import pytest

from papertrack.calendar_plan import (
    iter_date_range,
    month_days,
    parse_iso_date,
    rolling_refresh_days,
)


def test_parse_iso_date_accepts_valid_date():
    assert parse_iso_date("2026-06-09") == date(2026, 6, 9)


def test_parse_iso_date_rejects_invalid_date():
    with pytest.raises(ValueError, match="Expected YYYY-MM-DD"):
        parse_iso_date("2026.06.09")


def test_parse_iso_date_rejects_compact_date():
    with pytest.raises(ValueError, match="Expected YYYY-MM-DD"):
        parse_iso_date("20260609")


def test_parse_iso_date_rejects_week_date():
    with pytest.raises(ValueError, match="Expected YYYY-MM-DD"):
        parse_iso_date("2026-W24-2")


def test_iter_date_range_is_inclusive():
    assert iter_date_range(date(2026, 6, 7), date(2026, 6, 9)) == [
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 6, 9),
    ]


def test_iter_date_range_rejects_reversed_dates():
    with pytest.raises(ValueError, match="start date must be on or before end date"):
        iter_date_range(date(2026, 6, 10), date(2026, 6, 9))


def test_rolling_refresh_days_includes_anchor_and_previous_days():
    assert rolling_refresh_days(date(2026, 6, 9), refresh_days=3) == [
        date(2026, 6, 6),
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 6, 9),
    ]


def test_rolling_refresh_days_crosses_month_boundary():
    assert rolling_refresh_days(date(2026, 3, 2), refresh_days=3) == [
        date(2026, 2, 27),
        date(2026, 2, 28),
        date(2026, 3, 1),
        date(2026, 3, 2),
    ]


def test_rolling_refresh_days_rejects_negative_window():
    with pytest.raises(ValueError, match="refresh_days must be non-negative"):
        rolling_refresh_days(date(2026, 6, 9), refresh_days=-1)


def test_month_days_handles_full_month():
    days = month_days("2026-02")
    assert days[0] == date(2026, 2, 1)
    assert days[-1] == date(2026, 2, 28)
    assert len(days) == 28


def test_month_days_handles_leap_year():
    days = month_days("2024-02")
    assert days[-1] == date(2024, 2, 29)
    assert len(days) == 29


def test_month_days_rejects_bad_month_format():
    with pytest.raises(ValueError, match="Expected YYYY-MM"):
        month_days("2026.02")
