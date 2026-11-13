"""Date ranges: named periods, Meta's two-year limit, request windows and buckets."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from instagram_mcp.periods import (
    PERIODS,
    DateRange,
    PeriodError,
    detect_timezone,
    earliest_day,
    resolve_period,
)

PARIS = ZoneInfo("Europe/Paris")
TODAY = date(2026, 11, 11)


def resolve(period: str | None = None, start: str | None = None, end: str | None = None) -> DateRange:
    return resolve_period(period, start, end, tz=PARIS, today=TODAY)


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("last_7_days", "2026-11-04", "2026-11-10"),
        ("last_28_days", "2026-10-14", "2026-11-10"),
        ("last_90_days", "2026-08-13", "2026-11-10"),
        ("this_month", "2026-11-01", "2026-11-10"),
        ("last_month", "2026-10-01", "2026-10-31"),
        ("this_year", "2026-01-01", "2026-11-10"),
        ("last_year", "2025-01-01", "2025-12-31"),
        ("all_available", "2024-11-12", "2026-11-10"),
    ],
)
def test_named_periods_end_yesterday(period: str, start: str, end: str) -> None:
    rng = resolve(period)
    assert (rng.start.isoformat(), rng.end.isoformat(), rng.label) == (start, end, period)


def test_every_named_period_resolves() -> None:
    for period in PERIODS:
        assert resolve(period).days >= 1


def test_custom_dates_and_their_limits() -> None:
    rng = resolve(start="2026-11-01", end="2026-11-11")
    assert rng.days == 11 and rng.label == "custom"  # today is allowed (a partial day)
    assert resolve(start="2026-11-01").end == date(2026, 11, 10)
    with pytest.raises(PeriodError, match="two years"):
        resolve(start="2024-11-11")
    with pytest.raises(PeriodError, match="future"):
        resolve(start="2026-11-01", end="2026-11-12")
    with pytest.raises(PeriodError, match="without start_date"):
        resolve(end="2026-11-01")
    with pytest.raises(PeriodError, match="YYYY-MM-DD"):
        resolve(start="11/01/2026")
    with pytest.raises(PeriodError, match="Unknown period"):
        resolve("last_decade")


def test_first_day_of_the_month_and_year() -> None:
    first = resolve_period("this_month", None, None, tz=PARIS, today=date(2026, 12, 1))
    assert (first.start, first.end) == (date(2026, 12, 1), date(2026, 12, 1))
    assert earliest_day(date(2028, 2, 29)) == date(2026, 3, 1)


def test_windows_stay_under_metas_30_day_limit_across_dst() -> None:
    rng = resolve("last_365_days")
    windows = rng.windows()
    assert windows[0].start == rng.start and windows[-1].end == rng.end
    assert sum(w.days for w in windows) == rng.days
    assert all(w.until - w.since <= 2_592_000 for w in windows)
    # A 30-day window across the October DST change would be 30 days + 1 hour: never built.
    assert all(w.days <= 29 for w in windows)


def test_previous_range_and_dict() -> None:
    rng = resolve("last_28_days")
    previous = rng.previous()
    assert (previous.start, previous.end, previous.days) == (date(2026, 9, 16), date(2026, 10, 13), 28)
    assert rng.to_dict() == {
        "start_date": "2026-10-14",
        "end_date": "2026-11-10",
        "days": 28,
        "label": "last_28_days",
        "timezone": "Europe/Paris",
    }


def test_buckets_flag_partial_weeks_and_months() -> None:
    weeks = resolve(start="2026-10-14", end="2026-11-10").buckets("week")
    assert [(b.start.isoformat(), b.end.isoformat(), partial) for b, partial in weeks[:2]] == [
        ("2026-10-14", "2026-10-18", True),
        ("2026-10-19", "2026-10-25", False),
    ]
    months = resolve("last_90_days").buckets("month")
    assert [(b.start.month, partial) for b, partial in months] == [
        (8, True),
        (9, False),
        (10, False),
        (11, True),
    ]
    days = resolve("last_7_days").buckets("day")
    assert len(days) == 7 and not any(partial for _, partial in days)


def test_timezone_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert detect_timezone() == "America/New_York"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert detect_timezone()  # falls back to /etc/localtime or UTC
