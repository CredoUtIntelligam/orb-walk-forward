"""Opening-range high and low for each regular-hours session.

Bars are 1-minute OHLCV with a timezone-aware index, as returned by
``load_ohlcv``. The window starts at the RTH open, 09:30 America/New_York.
Stamps are the start of the minute, so a window of W minutes is the
half-open interval [09:30, 09:30 + W minutes). A 15-minute opening range
uses the 15 bars from 09:30 through 09:44 inclusive.

Incomplete sessions are skipped. A session is complete only when that
interval contains exactly one minute-aligned bar for every minute. A missing
minute, a duplicate stamp, or a sub-minute timestamp inside the window drops
the session. Bars outside the window are ignored. They do not move the high
or low, and they do not make an otherwise complete session incomplete.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from orb.loader import NY_TZ, RTH_CLOSE_MINUTES, RTH_OPEN_MINUTES

DEFAULT_WINDOW_MINUTES = 15
MAX_WINDOW_MINUTES = RTH_CLOSE_MINUTES - RTH_OPEN_MINUTES

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")
RESULT_COLUMNS = (
    "session_date",
    "range_high",
    "range_low",
    "window_minutes",
    "bar_count",
)

__all__ = ["DEFAULT_WINDOW_MINUTES", "opening_ranges"]


def opening_ranges(
    bars: pd.DataFrame,
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> pd.DataFrame:
    """Compute the opening-range high and low for each complete session.

    Parameters
    ----------
    bars:
        1-minute OHLCV from ``load_ohlcv``. The index must be a
        timezone-aware ``DatetimeIndex``. ``America/New_York`` is used
        as-is; any other zone is converted to New York before the clock
        window is applied. Naive timestamps are rejected rather than
        assumed to be Eastern. Columns ``open``, ``high``, ``low``,
        ``close``, and ``volume`` are required. When ``symbol`` is
        present it must name a single instrument.
    window_minutes:
        Length of the opening range in minutes. The default is 15. The
        window is ``[09:30, 09:30 + window_minutes)`` ET and must end at
        or before 16:00 ET, so the maximum is 390 minutes (09:30 through
        the 15:59 bar).

    Returns
    -------
    pandas.DataFrame
        One row per complete New York session, sorted by date. Columns
        are ``session_date`` (a ``datetime.date``), ``range_high`` (max
        high in the window), ``range_low`` (min low in the window),
        ``window_minutes``, and ``bar_count``. Sessions that do not have
        every window minute exactly once are omitted. An input with no
        complete session returns an empty frame with those columns.
    """
    window_minutes = _validate_window(window_minutes)
    frame = _prepare(bars)
    if frame.empty:
        return _result_frame([])
    return _result_frame(_complete_sessions(frame, window_minutes))


def _validate_window(window_minutes: int) -> int:
    # ``bool`` is an ``int`` subclass; reject it so ``True`` is not 1 minute.
    if type(window_minutes) is not int:
        raise TypeError("window_minutes must be an int number of minutes")
    if window_minutes < 1:
        raise ValueError("window_minutes must be a positive number of minutes")
    if window_minutes > MAX_WINDOW_MINUTES:
        raise ValueError(
            "window_minutes extends past the 16:00 ET RTH close; "
            f"maximum is {MAX_WINDOW_MINUTES}"
        )
    return window_minutes


def _prepare(bars: pd.DataFrame) -> pd.DataFrame:
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
    if frame.empty:
        return frame

    for column in ("high", "low"):
        if not pd.api.types.is_numeric_dtype(frame[column]):
            raise ValueError(f"{column} must be numeric")
        if frame[column].isna().any():
            raise ValueError(f"{column} contains missing values")

    if "symbol" in frame.columns:
        symbols = frame["symbol"].astype("string").str.strip()
        present = symbols.dropna()
        present = present[present != ""]
        distinct = sorted(set(present.unique()))
        if len(distinct) > 1:
            preview = ", ".join(distinct[:5])
            raise ValueError(
                f"bars contain multiple symbols ({preview}); "
                "filter to one symbol before computing the opening range"
            )
    return frame


def _complete_sessions(
    frame: pd.DataFrame, window_minutes: int
) -> list[dict[str, object]]:
    start = RTH_OPEN_MINUTES
    end = start + window_minutes
    index = frame.index
    minute_of_day = index.hour * 60 + index.minute
    # ``nanosecond`` is only the 0-999 remainder; sub-minute fractions also
    # sit in ``microsecond``.
    aligned = (
        (index.second == 0) & (index.microsecond == 0) & (index.nanosecond == 0)
    )
    in_window = (minute_of_day >= start) & (minute_of_day < end)
    disqualified = {_as_date(value) for value in index.date[in_window & ~aligned]}

    selected = frame.loc[in_window & aligned]
    rows: list[dict[str, object]] = []
    if selected.empty:
        return rows

    for raw_date, group in selected.groupby(selected.index.date, sort=True):
        session_date = _as_date(raw_date)
        if session_date in disqualified:
            continue
        minutes = group.index.hour * 60 + group.index.minute
        # Every selected stamp is already inside [start, end), and that
        # half-open minute range has exactly ``window_minutes`` values.
        # Matching that count with no duplicate stamp means each minute
        # is present once.
        if len(group) != window_minutes or len(pd.unique(minutes)) != window_minutes:
            continue
        rows.append(
            {
                "session_date": session_date,
                "range_high": float(group["high"].max()),
                "range_low": float(group["low"].min()),
                "window_minutes": window_minutes,
                "bar_count": int(len(group)),
            }
        )
    rows.sort(key=lambda row: row["session_date"])
    return rows


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise ValueError("bars index contains a missing session date")
    return parsed.date()


def _result_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    # Keep ``session_date`` as ``datetime.date``. A default constructor may
    # infer datetimes and attach a midnight timezone interpretation.
    return pd.DataFrame(
        {
            "session_date": pd.Series(
                [row["session_date"] for row in rows], dtype="object"
            ),
            "range_high": pd.array(
                [row["range_high"] for row in rows], dtype="float64"
            ),
            "range_low": pd.array([row["range_low"] for row in rows], dtype="float64"),
            "window_minutes": pd.array(
                [row["window_minutes"] for row in rows], dtype="int64"
            ),
            "bar_count": pd.array([row["bar_count"] for row in rows], dtype="int64"),
        }
    )
