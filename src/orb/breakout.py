"""First opening-range breakout of each regular-hours session.

``breakout_signals`` reads 1-minute bars and the per-session ranges from
``opening_ranges``. A session emits at most one signal: the first eligible
bar that breaks the range. Later bars that day are ignored, including a
break of the other side. A session that never breaks is omitted.

The opening range occupies ``[09:30, 09:30 + W)`` America/New_York, where
``W`` is that session's ``window_minutes``. Eligible bars are
minute-aligned stamps in ``[09:30 + W, 16:00)``. Bars inside the window,
bars before 09:30, and bars at or after 16:00 do not signal. Sub-minute
stamps are ignored. 16:00 is exclusive because these bars are stamped at
the start of the minute, matching the loader's regular-hours filter.

Two trigger rules are supported. Both use a strict comparison, so a print
that only tags the level is not a breakout.

``close`` (the default). Long when the bar closes strictly above the
opening-range high. Short when it closes strictly below the opening-range
low. A wick through the level with the close back inside the range is not
a signal. ``entry_price`` is that close.

``high_low``. Long when the bar's high trades strictly above the
opening-range high. Short when its low trades strictly below the
opening-range low. ``entry_price`` is the broken level, the reference for
a stop resting there, not the wick extreme.

A bar that breaks both sides under the selected rule is ambiguous and is
skipped. Scanning continues with the next eligible bar. A close cannot be
on both sides when the range high is at least the range low; breaking both
sides on one bar is a ``high_low`` case.

``entry_price`` is a reference on the signal. It is not a fill. This
module does not size a position, choose a stop or target, or compute P&L.

Early-close sessions are not signaled. Callers should pass
``signal_bars(load_ohlcv(...))`` into both ``opening_ranges`` and
``breakout_signals``, so the range table and the scan cover the same
sessions. ``signal_bars`` always drops sessions flagged ``early_close``,
including holiday short sessions, and has no parameter that puts them
back. When ``bars`` still carries that column, this function applies the
same drop before it scans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, TypedDict

import numpy as np
import pandas as pd

from orb.loader import NY_TZ, RTH_CLOSE_MINUTES, RTH_OPEN_MINUTES, signal_bars

DEFAULT_TRIGGER = "close"
TRIGGER_CLOSE = "close"
TRIGGER_HIGH_LOW = "high_low"
LONG = "long"
SHORT = "short"

MAX_WINDOW_MINUTES = RTH_CLOSE_MINUTES - RTH_OPEN_MINUTES
TRIGGERS = (TRIGGER_CLOSE, TRIGGER_HIGH_LOW)

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
RANGE_COLUMNS = ("session_date", "range_high", "range_low", "window_minutes")
PRICE_COLUMNS = ("high", "low", "close")

__all__ = [
    "DEFAULT_TRIGGER",
    "LONG",
    "SHORT",
    "TRIGGER_CLOSE",
    "TRIGGER_HIGH_LOW",
    "Signal",
    "breakout_signals",
]


@dataclass(frozen=True, slots=True)
class Signal:
    """One opening-range breakout.

    ``session_date`` is the New York trading date. ``direction`` is
    ``"long"`` or ``"short"``. ``or_high`` and ``or_low`` are the
    opening-range levels the signal was based on. ``entry_ts`` is the
    start of the triggering 1-minute bar, timezone-aware in
    America/New_York. ``entry_price`` is the reference for the trigger
    that fired: the bar close for ``"close"``, and the broken level
    (``or_high`` or ``or_low``) for ``"high_low"``.
    """

    session_date: date
    direction: Literal["long", "short"]
    or_high: float
    or_low: float
    entry_ts: pd.Timestamp
    entry_price: float


class _RangeSession(TypedDict):
    session_date: date
    range_high: float
    range_low: float
    window_minutes: int


def breakout_signals(
    bars: pd.DataFrame,
    ranges: pd.DataFrame,
    *,
    trigger: str = DEFAULT_TRIGGER,
) -> list[Signal]:
    """Emit the first opening-range breakout of each session, if there is one.

    Parameters
    ----------
    bars:
        1-minute OHLCV from ``load_ohlcv``. The index must be a
        timezone-aware ``DatetimeIndex``. ``America/New_York`` is used
        as-is; any other zone is converted to New York before the clock
        is applied. Naive timestamps are rejected. Columns ``open``,
        ``high``, ``low``, ``close``, and ``volume`` are required. When
        ``symbol`` is present it must name a single instrument. Duplicate
        timestamps are rejected. The frame is not modified.

        Pass the frame from ``signal_bars(load_ohlcv(...))``. If
        ``early_close`` is still present, rows with that flag set are
        dropped before the scan. There is no parameter that keeps them.
        A frame without the column is scanned as given, which is how
        hand-built bars that never came from the loader are accepted.
    ranges:
        Opening ranges from ``opening_ranges``. Required columns are
        ``session_date``, ``range_high``, ``range_low``, and
        ``window_minutes``. ``session_date`` is the New York trading
        date. ``window_minutes`` is the window that produced that row, so
        the first eligible bar is ``09:30 + window_minutes``. Sessions
        absent from this table are not scanned. Duplicate dates, a high
        below the low, and a window outside ``[1, 390]`` minutes are
        rejected. The frame is not modified.
    trigger:
        ``"close"`` (the default) or ``"high_low"``. See the module
        docstring for the comparison and for which price is stored as
        ``entry_price``.

    Returns
    -------
    list[Signal]
        Signals in session-date order. At most one per date. Days with
        no one-sided break after the window are not included. An empty
        input returns an empty list. ``or_high`` and ``or_low`` are the
        range row's high and low. ``entry_ts`` is the triggering bar.
    """
    trigger = _validate_trigger(trigger)
    frame = _prepare_bars(bars)
    sessions = _prepare_ranges(ranges)
    if frame.empty or not sessions:
        return []

    by_day = _bars_by_session(frame)
    signals: list[Signal] = []
    for session in sessions:
        day = by_day.get(session["session_date"])
        if day is None or day.empty:
            continue
        signal = _signal_for_day(day, session, trigger)
        if signal is not None:
            signals.append(signal)
    return signals


def _validate_trigger(trigger: object) -> str:
    if not isinstance(trigger, str):
        raise TypeError("trigger must be 'close' or 'high_low'")
    if trigger not in TRIGGERS:
        raise ValueError("trigger must be 'close' or 'high_low'")
    return trigger


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars index must be a timezone-aware DatetimeIndex")
    missing = [name for name in REQUIRED_COLUMNS if name not in bars.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    if bars.index.tz is None:
        raise ValueError(
            "bars index must be timezone-aware America/New_York; "
            "naive timestamps are not treated as ET"
        )

    if str(bars.index.tz) == "America/New_York":
        frame = bars
    else:
        frame = bars.copy()
        frame.index = bars.index.tz_convert(NY_TZ)

    if frame.index.isna().any():
        raise ValueError("bars index contains missing timestamps")
    # Same rule as ``signal_bars``: early-close sessions never signal.
    # Frames built without the column are left unchanged.
    if "early_close" in frame.columns:
        frame = signal_bars(frame)
    _reject_multiple_symbols(frame)
    if frame.empty:
        return frame
    if frame.index.has_duplicates:
        raise ValueError("bars index contains duplicate timestamps")
    if not frame.index.is_monotonic_increasing:
        frame = frame.sort_index(kind="mergesort")

    for column in PRICE_COLUMNS:
        if pd.api.types.is_bool_dtype(frame[column]) or not pd.api.types.is_numeric_dtype(
            frame[column]
        ):
            raise ValueError(f"{column} must be numeric")
        if frame[column].isna().any():
            raise ValueError(f"{column} contains missing values")
        if not np.isfinite(frame[column].to_numpy(dtype="float64")).all():
            raise ValueError(f"{column} must be finite")
    return frame


def _reject_multiple_symbols(frame: pd.DataFrame) -> None:
    if "symbol" not in frame.columns:
        return
    symbols = frame["symbol"].astype("string").str.strip()
    present = symbols.dropna()
    present = present[present != ""]
    distinct = sorted(set(present.unique()))
    if len(distinct) > 1:
        preview = ", ".join(distinct[:5])
        raise ValueError(
            f"bars contain multiple symbols ({preview}); "
            "filter to one symbol before computing breakout signals"
        )


def _prepare_ranges(ranges: pd.DataFrame) -> list[_RangeSession]:
    if not isinstance(ranges, pd.DataFrame):
        raise TypeError("ranges must be a pandas DataFrame")
    missing = [name for name in RANGE_COLUMNS if name not in ranges.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    # No rows means no signals. Dtype and bound checks apply to rows that exist.
    if ranges.empty:
        return []

    for name in ("range_high", "range_low"):
        column = ranges[name]
        if pd.api.types.is_bool_dtype(column) or not pd.api.types.is_numeric_dtype(column):
            raise ValueError(f"{name} must be numeric")
        if column.isna().any():
            raise ValueError(f"{name} contains missing values")
        if len(column) and not np.isfinite(column.to_numpy(dtype="float64")).all():
            raise ValueError(f"{name} must be finite")

    windows = ranges["window_minutes"]
    if pd.api.types.is_bool_dtype(windows) or not pd.api.types.is_integer_dtype(windows):
        raise TypeError("window_minutes must be an int number of minutes")
    if windows.isna().any():
        raise ValueError("window_minutes contains missing values")

    sessions: list[_RangeSession] = []
    seen: set[date] = set()
    rows = zip(
        ranges["session_date"].tolist(),
        ranges["range_high"].tolist(),
        ranges["range_low"].tolist(),
        ranges["window_minutes"].tolist(),
        strict=True,
    )
    for raw_date, raw_high, raw_low, raw_window in rows:
        session_date = _as_date(raw_date)
        if session_date in seen:
            raise ValueError(
                f"ranges contain duplicate session_date {session_date.isoformat()}"
            )
        seen.add(session_date)
        window_minutes = _validate_window(raw_window)
        range_high = float(raw_high)
        range_low = float(raw_low)
        if range_high < range_low:
            raise ValueError(
                f"{session_date.isoformat()} range_high is below range_low"
            )
        sessions.append(
            {
                "session_date": session_date,
                "range_high": range_high,
                "range_low": range_low,
                "window_minutes": window_minutes,
            }
        )
    sessions.sort(key=lambda session: session["session_date"])
    return sessions


def _validate_window(window_minutes: object) -> int:
    # ``bool`` is an ``int`` subclass; reject it so ``True`` is not 1 minute.
    if isinstance(window_minutes, bool) or not isinstance(
        window_minutes, (int, np.integer)
    ):
        raise TypeError("window_minutes must be an int number of minutes")
    minutes = int(window_minutes)
    if minutes < 1:
        raise ValueError("window_minutes must be a positive number of minutes")
    if minutes > MAX_WINDOW_MINUTES:
        raise ValueError(
            "window_minutes extends past the 16:00 ET RTH close; "
            f"maximum is {MAX_WINDOW_MINUTES}"
        )
    return minutes


def _bars_by_session(frame: pd.DataFrame) -> dict[date, pd.DataFrame]:
    grouped: dict[date, pd.DataFrame] = {}
    for raw_date, group in frame.groupby(frame.index.date, sort=False):
        grouped[_as_date(raw_date)] = group
    return grouped


def _signal_for_day(
    day: pd.DataFrame,
    session: _RangeSession,
    trigger: str,
) -> Signal | None:
    eligible = _eligible_bars(day, session["window_minutes"])
    range_high = session["range_high"]
    range_low = session["range_low"]
    for timestamp, row in eligible.iterrows():
        long_break, short_break, long_price, short_price = _bar_break(
            row, range_high, range_low, trigger
        )
        if not long_break and not short_break:
            continue
        # One bar can trade through both levels. That bar does not choose
        # a direction; the next one-sided break still can.
        if long_break and short_break:
            continue
        if long_break:
            direction = LONG
            entry_price = long_price
        else:
            direction = SHORT
            entry_price = short_price
        return Signal(
            session_date=session["session_date"],
            direction=direction,
            or_high=range_high,
            or_low=range_low,
            entry_ts=pd.Timestamp(timestamp),
            entry_price=float(entry_price),
        )
    return None


def _eligible_bars(day: pd.DataFrame, window_minutes: int) -> pd.DataFrame:
    index = day.index
    minute_of_day = index.hour * 60 + index.minute
    # ``[09:30 + W, 16:00)``. The window itself is ``[09:30, 09:30 + W)``.
    start = RTH_OPEN_MINUTES + window_minutes
    aligned = (index.second == 0) & (index.microsecond == 0) & (index.nanosecond == 0)
    in_session = (minute_of_day >= start) & (minute_of_day < RTH_CLOSE_MINUTES)
    return day.loc[in_session & aligned]


def _bar_break(
    row: pd.Series,
    range_high: float,
    range_low: float,
    trigger: str,
) -> tuple[bool, bool, float, float]:
    if trigger == TRIGGER_CLOSE:
        price = float(row["close"])
        return price > range_high, price < range_low, price, price
    bar_high = float(row["high"])
    bar_low = float(row["low"])
    return bar_high > range_high, bar_low < range_low, range_high, range_low


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        if pd.isna(value):
            raise ValueError("ranges session_date contains a missing date")
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ranges session_date could not be read: {value!r}") from exc
    if pd.isna(parsed):
        raise ValueError("ranges session_date contains a missing date")
    return parsed.date()
