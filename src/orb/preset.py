"""Frozen ES ORB signal preset.

``frozen_es_signals`` is the library form of ``--preset frozen-es``. It
follows the frozen spec's signal rule and nothing past that signal:

- Box from 1-minute bars in ``[09:30, 10:00)`` America/New_York (09:30
  through 09:59). ``or_high`` is the max high and ``or_low`` is the min
  low of the bars that are present.
- A missing minute inside the box still leaves a partial range. A day is
  skipped only when the 09:30 bar is absent or the box has no bars.
- Trigger: the first clock-aligned 5-minute close strictly above
  ``or_high``. Buckets are ``[t, t+5)`` for ``t`` in 10:00, 10:05, ...,
  13:50. The 5-minute close is the 1-minute close at ``t+4`` (the 10:00
  bucket is stamped 10:04). The 13:55 bucket is not eligible.
- Long only. One signal per session. ``entry_price`` is that close.
- The last bucket is the earlier of the 13:50-13:55 spec bucket and the
  session close. A 5-minute bucket that would extend past that close
  does not trigger. Early-close sessions are included. ``early_close``
  is metadata and is not a reason to drop the day.

This function does not place a stop, choose a target, flatten, size a
position, or compute P&L.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

from orb.breakout import LONG, TRIGGER_CLOSE_5M, Signal, _session_facts
from orb.loader import NY_TZ, entry_cutoff_minute, signal_bars

FROZEN_ES_PRESET = "frozen-es"
FROZEN_ES_WINDOW_MINUTES = 30

# [09:30, 10:00). The 09:30 print is required; later gaps stay in the box.
BOX_OPEN_MINUTES = 9 * 60 + 30
BOX_END_MINUTES = 10 * 60

# 10:00, 10:05, ..., 13:50. Close minute is t+4. 13:55 is excluded.
FIRST_BUCKET_MINUTES = 10 * 60
LAST_BUCKET_MINUTES = 13 * 60 + 50
BUCKET_WIDTH_MINUTES = 5
CLOSE_OFFSET_MINUTES = 4

_BUCKET_STARTS = range(
    FIRST_BUCKET_MINUTES,
    LAST_BUCKET_MINUTES + 1,
    BUCKET_WIDTH_MINUTES,
)

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("high", "low", "close")

__all__ = [
    "FROZEN_ES_PRESET",
    "FROZEN_ES_WINDOW_MINUTES",
    "frozen_es_signals",
]


def frozen_es_signals(bars: pd.DataFrame) -> list[Signal]:
    """Return the frozen ES ORB signal for each session that has one.

    Parameters
    ----------
    bars:
        1-minute OHLCV. The index must be a timezone-aware
        ``DatetimeIndex``. America/New_York is used as-is; any other zone
        is converted before the clock is applied. Naive timestamps are
        rejected. Pass ``signal_bars(load_ohlcv(...))``. If
        ``early_close`` is still present, those rows are kept and the
        flag stays metadata. There is no switch that drops them. The
        entry cutoff is the earlier of 13:55 ET and that session's
        close, and a bucket that would extend past the close is skipped.
        A frame without the column is scanned as given.

    Returns
    -------
    list[Signal]
        Long signals in session-date order. ``window_minutes`` is 30 and
        ``trigger`` is ``"close_5m"``. ``entry_ts`` is the bucket close
        minute. Days with no 09:30 bar, an empty box, or no eligible
        5-minute close strictly above the box high are omitted. An
        early-close session can still emit a signal.
    """
    frame = _prepare_bars(bars)
    if frame.empty:
        return []
    signals: list[Signal] = []
    for raw_date, day in frame.groupby(frame.index.date, sort=True):
        signal = _signal_for_day(day, _as_date(raw_date))
        if signal is not None:
            signals.append(signal)
    return signals


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
    # Keep early-close rows. ``signal_bars`` copies the frame and leaves
    # the flag in place.
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
            "filter to one symbol before computing frozen ES signals"
        )


def _signal_for_day(day: pd.DataFrame, session_date: date) -> Signal | None:
    index = day.index
    aligned = (index.second == 0) & (index.microsecond == 0) & (index.nanosecond == 0)
    selected = day.loc[aligned]
    if selected.empty:
        return None
    selected_minutes = selected.index.hour * 60 + selected.index.minute
    in_box = (selected_minutes >= BOX_OPEN_MINUTES) & (selected_minutes < BOX_END_MINUTES)
    has_open = bool((selected_minutes == BOX_OPEN_MINUTES).any())
    box = selected.loc[in_box]
    # Spec: skip only when the 09:30 bar is missing or the box is empty.
    # A gap after 09:30 still uses the bars that printed.
    if not has_open or box.empty:
        return None
    or_high = float(box["high"].max())
    or_low = float(box["low"].min())
    close_at = {
        int(minute): (stamp, float(price))
        for stamp, minute, price in zip(
            selected.index,
            selected_minutes,
            selected["close"].tolist(),
            strict=True,
        )
    }
    # Spec last bucket ends at 13:55. A shortened session ends sooner.
    # ``end > cutoff`` rejects a bucket that would extend past the close.
    session_close = entry_cutoff_minute(session_date, np.asarray(selected_minutes))
    cutoff = min(LAST_BUCKET_MINUTES + BUCKET_WIDTH_MINUTES, session_close)
    for start in _BUCKET_STARTS:
        if start + BUCKET_WIDTH_MINUTES > cutoff:
            break
        found = close_at.get(start + CLOSE_OFFSET_MINUTES)
        if found is None:
            continue
        stamp, price = found
        if price > or_high:
            early_close, session_close = _session_facts(day, session_date)
            return Signal(
                session_date=session_date,
                direction=LONG,
                or_high=or_high,
                or_low=or_low,
                entry_ts=pd.Timestamp(stamp),
                entry_price=price,
                window_minutes=FROZEN_ES_WINDOW_MINUTES,
                trigger=TRIGGER_CLOSE_5M,
                early_close=early_close,
                session_close=session_close,
            )
    return None


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError("bars index contains a missing session date")
    return parsed.date()
