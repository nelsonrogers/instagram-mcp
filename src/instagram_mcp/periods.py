"""Date ranges, timezones and request windows for Instagram insights.

Meta keeps account insights for two years and accepts at most 30 days (2,592,000 s) between
``since`` and ``until`` in one request, so longer ranges are read as consecutive windows of at
most 29 days (29 days stay under the limit even across a daylight-saving change).

Days are the report timezone's days: ``INSTAGRAM_MCP_TIMEZONE``, else the computer's timezone,
else UTC. Rolling periods end yesterday, the latest complete day; Meta's numbers can still move
for up to 48 hours.
"""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_WINDOW_DAYS = 29
HISTORY_YEARS = 2

Period = Literal[
    "last_7_days",
    "last_14_days",
    "last_28_days",
    "last_30_days",
    "last_90_days",
    "last_180_days",
    "last_365_days",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_available",
]
PERIODS: tuple[str, ...] = (
    "last_7_days",
    "last_14_days",
    "last_28_days",
    "last_30_days",
    "last_90_days",
    "last_180_days",
    "last_365_days",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "all_available",
)
_ROLLING = {
    "last_7_days": 7,
    "last_14_days": 14,
    "last_28_days": 28,
    "last_30_days": 30,
    "last_90_days": 90,
    "last_180_days": 180,
    "last_365_days": 365,
}
Granularity = Literal["day", "week", "month"]


class PeriodError(ValueError):
    """Invalid period or date input (message is user-facing)."""


def detect_timezone() -> str:
    """The computer's IANA timezone name, or ``"UTC"`` when it can't be told."""
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if name and _valid_zone(name):
        return name
    try:
        target = str(Path("/etc/localtime").resolve())
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    if marker in target:
        candidate = target.split(marker, 1)[1]
        if _valid_zone(candidate):
            return candidate
    return "UTC"


def _valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PeriodError(
            f"Unknown timezone {name!r}: use an IANA name such as 'Europe/Paris' or 'America/New_York'."
        ) from exc


def now(tz: tzinfo) -> datetime:
    """The current time in ``tz`` (tests pin it)."""
    return datetime.now(tz)


def today_in(tz: tzinfo) -> date:
    return now(tz).date()


def midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def earliest_day(today: date) -> date:
    """The first day Meta still serves account insights for (two years back)."""
    try:
        two_years_ago = today.replace(year=today.year - HISTORY_YEARS)
    except ValueError:  # 29 February
        two_years_ago = today.replace(year=today.year - HISTORY_YEARS, day=28)
    return two_years_ago + timedelta(days=1)


def parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise PeriodError(f"{field_name} must be a date in YYYY-MM-DD format, got {value!r}.") from exc


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


@dataclass(frozen=True)
class DateRange:
    """Inclusive range of days in ``tz``."""

    start: date
    end: date
    label: str
    tz: tzinfo

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise PeriodError(
                f"Start date {self.start.isoformat()} is after end date {self.end.isoformat()}."
            )

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def since(self) -> int:
        """Unix time of the first day's midnight."""
        return int(midnight(self.start, self.tz).timestamp())

    @property
    def until(self) -> int:
        """Unix time of the midnight after the last day."""
        return int(midnight(self.end + timedelta(days=1), self.tz).timestamp())

    def contains(self, instant: datetime) -> bool:
        return midnight(self.start, self.tz) <= instant < midnight(self.end + timedelta(days=1), self.tz)

    def previous(self) -> DateRange:
        """The range of equal length just before this one (for comparisons)."""
        end = self.start - timedelta(days=1)
        return DateRange(end - timedelta(days=self.days - 1), end, f"previous {self.days} days", self.tz)

    def windows(self, max_days: int = MAX_WINDOW_DAYS) -> list[DateRange]:
        """Consecutive sub-ranges of at most ``max_days`` days (one Meta request each)."""
        parts: list[DateRange] = []
        start = self.start
        while start <= self.end:
            end = min(self.end, start + timedelta(days=max_days - 1))
            parts.append(DateRange(start, end, self.label, self.tz))
            start = end + timedelta(days=1)
        return parts

    def buckets(self, granularity: Granularity) -> list[tuple[DateRange, bool]]:
        """Day, week (Monday to Sunday) or calendar-month buckets, each with a ``partial`` flag
        when the range cuts it off."""
        out: list[tuple[DateRange, bool]] = []
        day = self.start
        while day <= self.end:
            if granularity == "day":
                full_start, full_end = day, day
            elif granularity == "week":
                full_start = day - timedelta(days=day.weekday())
                full_end = full_start + timedelta(days=6)
            else:
                full_start, full_end = day.replace(day=1), month_end(day)
            start, end = max(full_start, self.start), min(full_end, self.end)
            out.append((DateRange(start, end, granularity, self.tz), (start, end) != (full_start, full_end)))
            day = end + timedelta(days=1)
        return out

    def to_dict(self) -> dict[str, str | int]:
        return {
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "days": self.days,
            "label": self.label,
            "timezone": str(self.tz),
        }


def resolve_period(
    period: str | None,
    start_date: str | None,
    end_date: str | None,
    *,
    tz: tzinfo,
    today: date | None = None,
    default: str = "last_28_days",
) -> DateRange:
    """A named period or explicit dates, as a :class:`DateRange` in ``tz``.

    Explicit dates win over ``period``; ``end_date`` defaults to yesterday and may be today (a
    partial day). Starts more than two years back are refused, since Meta has no data there.
    """
    today = today or today_in(tz)
    latest = today - timedelta(days=1)
    earliest = earliest_day(today)

    if start_date or end_date:
        if not start_date:
            raise PeriodError("end_date was given without start_date; provide both, or use `period`.")
        start = parse_date(start_date, "start_date")
        end = parse_date(end_date, "end_date") if end_date else latest
        if end > today:
            raise PeriodError(f"end_date {end.isoformat()} is in the future (today is {today.isoformat()}).")
        if start < earliest:
            raise PeriodError(
                f"start_date {start.isoformat()} is too far back: Instagram keeps account insights for "
                f"two years, so the earliest start is {earliest.isoformat()}."
            )
        return DateRange(start, end, "custom", tz)

    name = (period or default).strip().lower()
    if name in _ROLLING:
        return DateRange(latest - timedelta(days=_ROLLING[name] - 1), latest, name, tz)
    if name == "this_month":
        start = today.replace(day=1)
        return DateRange(start, latest if latest >= start else today, name, tz)
    if name == "last_month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        return DateRange(last_month_end.replace(day=1), last_month_end, name, tz)
    if name == "this_year":
        start = date(today.year, 1, 1)
        return DateRange(start, latest if latest >= start else today, name, tz)
    if name == "last_year":
        start = max(date(today.year - 1, 1, 1), earliest)
        return DateRange(start, date(today.year - 1, 12, 31), name, tz)
    if name in {"all_available", "all_time", "lifetime"}:
        return DateRange(earliest, latest, "all_available", tz)
    raise PeriodError(f"Unknown period {period!r}. Use one of: {', '.join(PERIODS)}, or start_date/end_date.")
