"""Expected OANDA XAU_USD trading sessions for data-quality checks."""

from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

import pandas as pd


NO_MARKET_CALENDAR = "none"
OANDA_XAU_USD_CALENDAR = "oanda_xau_usd"
SUPPORTED_MARKET_CALENDARS = frozenset({NO_MARKET_CALENDAR, OANDA_XAU_USD_CALENDAR})

_NEW_YORK = ZoneInfo("America/New_York")
_DAILY_PAUSE_START = time(17, 0)
# OANDA's M1 feed resumes at 18:04 New York time. Keeping this local makes
# the expected UTC maintenance interval correct on both sides of DST changes.
_DAILY_PAUSE_END = time(18, 4)
_US_HOLIDAY_EARLY_CLOSE = time(13, 0)


def _observed_independence_day(year: int) -> date:
    """Return the U.S. Independence Day observance date for ``year``."""

    holiday = date(year, 7, 4)
    if holiday.weekday() == 5:  # Saturday is observed on the prior Friday.
        return date(year, 7, 3)
    if holiday.weekday() == 6:  # Sunday is observed on the following Monday.
        return date(year, 7, 5)
    return holiday


def is_expected_trading_bar(
    open_time: object,
    *,
    market_calendar: str = NO_MARKET_CALENDAR,
    closed_weekdays: tuple[int, ...] = (),
) -> bool:
    """Return whether a bar beginning at ``open_time`` is expected to exist."""

    timestamp = pd.Timestamp(open_time)
    if timestamp.tzinfo is None:
        raise ValueError("market-calendar timestamps must be timezone-aware")
    if timestamp.weekday() in closed_weekdays:
        return False
    if market_calendar == NO_MARKET_CALENDAR:
        return True
    if market_calendar != OANDA_XAU_USD_CALENDAR:
        raise ValueError(f"unsupported market calendar: {market_calendar}")

    local = timestamp.tz_convert(_NEW_YORK)
    clock = local.time().replace(tzinfo=None)
    weekday = local.weekday()
    # OANDA's XAU_USD feed closed at 13:00 New York time on Friday
    # 2026-07-03, the observed U.S. Independence Day. Restrict this to the
    # Friday observance because an observed Monday has a different schedule.
    if weekday == 4 and local.date() == _observed_independence_day(local.year):
        return clock < _US_HOLIDAY_EARLY_CLOSE
    if weekday == 5:  # Saturday
        return False
    if weekday == 4:  # Friday closes for the weekend at 17:00 New York time.
        return clock < _DAILY_PAUSE_START
    if weekday == 6:  # Sunday opens after the same maintenance window.
        return clock >= _DAILY_PAUSE_END
    return not (_DAILY_PAUSE_START <= clock < _DAILY_PAUSE_END)


def unexpected_missing_bar_ranges(
    previous: object,
    current: object,
    interval: pd.Timedelta,
    *,
    market_calendar: str = NO_MARKET_CALENDAR,
    closed_weekdays: tuple[int, ...] = (),
) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Return contiguous missing ranges that should have traded."""

    if interval <= pd.Timedelta(0):
        raise ValueError("market-calendar interval must be positive")
    previous_time = pd.Timestamp(previous)
    current_time = pd.Timestamp(current)
    if current_time <= previous_time:
        return []

    ranges: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    start: pd.Timestamp | None = None
    count = 0
    expected = previous_time + interval
    while expected < current_time:
        if is_expected_trading_bar(
            expected,
            market_calendar=market_calendar,
            closed_weekdays=closed_weekdays,
        ):
            if start is None:
                start = expected
                count = 1
            elif expected == start + interval * count:
                count += 1
            else:
                ranges.append((start, start + interval * count, count))
                start = expected
                count = 1
        elif start is not None:
            ranges.append((start, start + interval * count, count))
            start = None
            count = 0
        expected += interval
    if start is not None:
        ranges.append((start, start + interval * count, count))
    return ranges
