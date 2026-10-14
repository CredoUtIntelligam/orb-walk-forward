"""First opening-range breakout of each regular-hours session.

This research scan is not the frozen ES ORB spec. The spec signal is
``frozen_es_signals`` (CLI ``--preset frozen-es``): a 30-minute box and
the first clock-aligned 5-minute close strictly above that high, long
only. ``breakout_signals`` is the separate research rule below.

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

Early-close sessions are included. Callers should pass
``signal_bars(load_ohlcv(...))`` into both ``opening_ranges`` and
``breakout_signals``, so the range table and the scan cover the same
sessions. ``signal_bars`` keeps sessions flagged ``early_close``,
including holiday short sessions. The ``early_close`` flag is
metadata: when ``bars`` still carries that column, this function keeps
those rows. An entry is not taken at or after that session's close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, TypedDict

import numpy as np
import pandas as pd

from orb.loader import (
    NY_TZ,
    RTH_CLOSE_MINUTES,
    RTH_OPEN_MINUTES,
    entry_cutoff_minute,
    signal_bars,
)

DEFAULT_TRIGGER = "close"
TRIGGER_CLOSE = "close"
TRIGGER_HIGH_LOW = "high_low"
# Frozen ES spec: first clock-aligned 5-minute close strictly above the box.
TRIGGER_CLOSE_5M = "close_5m"
LONG = "long"
SHORT = "short"

MAX_WINDOW_MINUTES = RTH_CLOSE_MINUTES - RTH_OPEN_MINUTES
# Research scan only. ``close_5m`` is recorded by the frozen preset, not
# accepted by ``breakout_signals``.
TRIGGERS = (TRIGGER_CLOSE, TRIGGER_HIGH_LOW)
SIGNAL_TRIGGERS = (TRIGGER_CLOSE, TRIGGER_HIGH_LOW, TRIGGER_CLOSE_5M)
DIRECTIONS = (LONG, SHORT)

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
RANGE_COLUMNS = ("session_date", "range_high", "range_low", "window_minutes")
PRICE_COLUMNS = ("high", "low", "close")

__all__ = [
    "DEFAULT_TRIGGER",
    "DIRECTIONS",
    "LONG",
    "SHORT",
    "SIGNAL_TRIGGERS",
    "TRIGGER_CLOSE",
    "TRIGGER_CLOSE_5M",
    "TRIGGER_HIGH_LOW",
    "Signal",
    "breakout_signals",
]


@dataclass(frozen=True, slots=True)
class Signal:
    """One opening-range breakout.

    ``session_date`` is the New York trading date. ``direction`` is
    ``"long"`` or ``"short"``. ``or_high`` and ``or_low`` are the
    opening-range levels the signal was based on. ``entry_ts`` is
    timezone-aware in America/New_York. In the research scan it is the
    start of the triggering 1-minute bar. Under the frozen ES preset it
    is that bucket's close minute (the 10:00 bucket is stamped 10:04).
    ``entry_price`` is the reference for the trigger that fired: the bar
    close for ``"close"`` and for ``"close_5m"``, and the broken level
    (``or_high`` or ``or_low``) for ``"high_low"``. ``window_minutes`` is
    the opening-range length that produced the levels. ``trigger`` is
    ``"close"``, ``"high_low"``, or ``"close_5m"``.

    ``__post_init__`` checks those types and the direction. ``to_dict``
    returns JSON-safe values. The record stays frozen.
    """

    session_date: date
    direction: Literal["long", "short"]
    or_high: float
    or_low: float
    entry_ts: pd.Timestamp
    entry_price: float
    window_minutes: int
    trigger: str

    def __post_init__(self) -> None:
        session_date = _require_session_date(self.session_date)
        direction = _require_direction(self.direction)
        or_high = _require_price(self.or_high, "or_high")
        or_low = _require_price(self.or_low, "or_low")
        entry_ts = _require_entry_ts(self.entry_ts)
        entry_price = _require_price(self.entry_price, "entry_price")
        window_minutes = _require_window_minutes(self.window_minutes)
        trigger = _require_signal_trigger(self.trigger)
        object.__setattr__(self, "session_date", session_date)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "or_high", or_high)
        object.__setattr__(self, "or_low", or_low)
        object.__setattr__(self, "entry_ts", entry_ts)
        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "window_minutes", window_minutes)
        object.__setattr__(self, "trigger", trigger)

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe fields.

        Dates and timestamps are ISO-8601 strings. Prices are ``float``
        and ``window_minutes`` is ``int``.
        """
        return {
            "session_date": self.session_date.isoformat(),
            "direction": self.direction,
            "or_high": self.or_high,
            "or_low": self.or_low,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_price": self.entry_price,
            "window_minutes": self.window_minutes,
            "trigger": self.trigger,
        }


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
        ``early_close`` is still present, those rows are kept and the
        flag stays metadata. There is no parameter that drops them.
        Entries on a shortened session stop at that session's close.
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
    # ``signal_bars`` keeps early-close rows and the flag. Calling it
    # when the column is present copies that frame without dropping days.
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
    cutoff = _session_cutoff(day, session["session_date"])
    eligible = _eligible_bars(day, session["window_minutes"], cutoff)
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
            window_minutes=session["window_minutes"],
            trigger=trigger,
        )
    return None


def _session_cutoff(day: pd.DataFrame, session_date: date) -> int:
    index = day.index
    aligned = (index.second == 0) & (index.microsecond == 0) & (index.nanosecond == 0)
    minutes = index.hour * 60 + index.minute
    return entry_cutoff_minute(session_date, np.asarray(minutes[aligned]))


def _eligible_bars(
    day: pd.DataFrame, window_minutes: int, cutoff_minute: int
) -> pd.DataFrame:
    index = day.index
    minute_of_day = index.hour * 60 + index.minute
    # ``[09:30 + W, session close)``. The window itself is ``[09:30, 09:30 + W)``.
    # ``cutoff_minute`` is 16:00 on a full session and the halt on an early close.
    start = RTH_OPEN_MINUTES + window_minutes
    aligned = (index.second == 0) & (index.microsecond == 0) & (index.nanosecond == 0)
    in_session = (minute_of_day >= start) & (minute_of_day < cutoff_minute)
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


def _require_session_date(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("session_date must be a datetime.date")
    return value


def _require_direction(value: object) -> str:
    if not isinstance(value, str) or value not in DIRECTIONS:
        raise ValueError("direction must be 'long' or 'short'")
    return value


def _require_price(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise TypeError(f"{name} must be a float")
    price = float(value)
    if not np.isfinite(price):
        raise ValueError(f"{name} must be finite")
    return price


def _require_entry_ts(value: object) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        stamp = value
    elif isinstance(value, datetime):
        stamp = pd.Timestamp(value)
    else:
        raise TypeError("entry_ts must be a timezone-aware pandas Timestamp")
    if pd.isna(stamp):
        raise ValueError("entry_ts is missing")
    if stamp.tzinfo is None:
        raise ValueError(
            "entry_ts must be timezone-aware America/New_York; "
            "naive timestamps are not treated as ET"
        )
    return pd.Timestamp(stamp).tz_convert(NY_TZ)


def _require_window_minutes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise TypeError("window_minutes must be an int number of minutes")
    minutes = int(value)
    if minutes < 1 or minutes > MAX_WINDOW_MINUTES:
        raise ValueError(
            "window_minutes must be a positive number of minutes "
            f"no greater than {MAX_WINDOW_MINUTES}"
        )
    return minutes


def _require_signal_trigger(value: object) -> str:
    if not isinstance(value, str) or value not in SIGNAL_TRIGGERS:
        raise ValueError("trigger must be 'close', 'high_low', or 'close_5m'")
    return value


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
