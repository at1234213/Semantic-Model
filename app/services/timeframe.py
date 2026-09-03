"""Turning a time phrase into concrete dates.

Deterministic and offline. The model reports what the user said — "last
quarter" — and this decides what that means, so the same question asked twice
on the same day compiles to the same SQL.

Ranges are half-open: start inclusive, end exclusive. That makes
`order_date >= start AND order_date < end` correct for both dates and
timestamps, with no "23:59:59" fudge.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.models.dimension import TimeGranularity


class TimeframeError(ValueError):
    """The phrase is not one we can resolve without guessing."""


@dataclass(frozen=True)
class TimeRange:
    start: date
    end: date  # exclusive
    grain: TimeGranularity | None = None
    phrase: str = ""

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise TimeframeError(f"{self.phrase!r} produced an empty range")


def _quarter_start(value: date) -> date:
    return date(value.year, 3 * ((value.month - 1) // 3) + 1, 1)


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


_RELATIVE = re.compile(r"^(?:the\s+)?(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?$")


def resolve_timeframe(phrase: str, *, today: date | None = None) -> TimeRange:
    """Resolve a time phrase relative to `today` (defaults to the current date)."""
    today = today or date.today()
    text = " ".join(phrase.strip().lower().split())
    if not text:
        raise TimeframeError("Empty time phrase")

    month_start = date(today.year, today.month, 1)
    quarter_start = _quarter_start(today)
    year_start = date(today.year, 1, 1)
    week_start = today - timedelta(days=today.weekday())

    fixed: dict[str, TimeRange] = {
        "today": TimeRange(today, today + timedelta(days=1), TimeGranularity.DAY, text),
        "yesterday": TimeRange(today - timedelta(days=1), today, TimeGranularity.DAY, text),
        "this week": TimeRange(week_start, week_start + timedelta(days=7),
                               TimeGranularity.WEEK, text),
        "last week": TimeRange(week_start - timedelta(days=7), week_start,
                               TimeGranularity.WEEK, text),
        "this month": TimeRange(month_start, _add_months(month_start, 1),
                                TimeGranularity.MONTH, text),
        "last month": TimeRange(_add_months(month_start, -1), month_start,
                                TimeGranularity.MONTH, text),
        "this quarter": TimeRange(quarter_start, _add_months(quarter_start, 3),
                                  TimeGranularity.QUARTER, text),
        "last quarter": TimeRange(_add_months(quarter_start, -3), quarter_start,
                                  TimeGranularity.QUARTER, text),
        "this year": TimeRange(year_start, date(today.year + 1, 1, 1),
                               TimeGranularity.YEAR, text),
        "last year": TimeRange(date(today.year - 1, 1, 1), year_start,
                               TimeGranularity.YEAR, text),
        # Year to date deliberately ends tomorrow, so today's rows are included.
        "year to date": TimeRange(year_start, today + timedelta(days=1),
                                  TimeGranularity.DAY, text),
        "ytd": TimeRange(year_start, today + timedelta(days=1), TimeGranularity.DAY, text),
    }
    if text in fixed:
        return fixed[text]

    match = _RELATIVE.match(text)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        if count < 1:
            raise TimeframeError(f"{phrase!r} asks for a range of no length")
        end = today + timedelta(days=1)
        if unit == "day":
            return TimeRange(end - timedelta(days=count), end, TimeGranularity.DAY, text)
        if unit == "week":
            return TimeRange(end - timedelta(weeks=count), end, TimeGranularity.WEEK, text)
        if unit == "month":
            return TimeRange(_add_months(end, -count), end, TimeGranularity.MONTH, text)
        return TimeRange(_add_months(end, -12 * count), end, TimeGranularity.YEAR, text)

    raise TimeframeError(
        f"{phrase!r} is not a time range this system understands. Try phrases like "
        "'last quarter', 'this month', 'year to date', or 'last 30 days'."
    )
