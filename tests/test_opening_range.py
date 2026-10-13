"""Unit tests for the RTH opening-range calculation."""

from __future__ import annotations

from datetime import date, time, timedelta

import pandas as pd
import pytest

from orb import (
    DEFAULT_WINDOW_MINUTES,
    load_ohlcv,
    opening_ranges,
    sample_fixture_path,
)

NY = "America/New_York"
RTH_OPEN = 9 * 60 + 30

# Levels printed on the synthetic fixture's 09:30 and 09:31 bars. Bars through
# 09:59 stay inside these levels; the 10:00 bar steps outside on two days.
FIXTURE_RANGES = {
    date(2024, 1, 16): {"range_low": 5000.0, "range_high": 5008.0},
    date(2024, 1, 17): {"range_low": 5050.0, "range_high": 5058.0},
    date(2024, 1, 18): {"range_low": 5100.0, "range_high": 5110.0},
}

RESULT_COLUMNS = [
    "session_date",
    "range_high",
    "range_low",
    "window_minutes",
    "bar_count",
]


def _clock(
    day: str,
    start_minute: int,
    count: int,
    *,
    tz: str = NY,
) -> pd.DatetimeIndex:
    anchor = pd.Timestamp(day, tz=tz) + pd.Timedelta(minutes=start_minute)
    return pd.date_range(anchor, periods=count, freq="min", name="ts_event")


def _ohlcv(
    index: pd.DatetimeIndex,
    high: float | list[float],
    low: float | list[float],
    *,
    symbol: str | list[str] = "MES",
) -> pd.DataFrame:
    count = len(index)
    highs = (
        [float(high)] * count
        if isinstance(high, (int, float))
        else [float(value) for value in high]
    )
    lows = (
        [float(low)] * count
        if isinstance(low, (int, float))
        else [float(value) for value in low]
    )
    if isinstance(symbol, str):
        symbols = [symbol] * count
    else:
        symbols = list(symbol)
    if index.name != "ts_event":
        index = index.rename("ts_event")
    return pd.DataFrame(
        {
            "open": [8.0] * count,
            "high": highs,
            "low": lows,
            "close": [7.0] * count,
            "volume": [1] * count,
            "symbol": symbols,
        },
        index=index,
    )


def _window(
    day: str,
    minutes: int,
    *,
    high: float | list[float] = 10.0,
    low: float | list[float] = 4.0,
    tz: str = NY,
) -> pd.DataFrame:
    return _ohlcv(_clock(day, RTH_OPEN, minutes, tz=tz), high, low)


def test_fixture_ranges_for_configurable_windows() -> None:
    """5, 15, and 30 minute windows share the fixture's labeled high and low.

    The 10:00 ET print steps outside that range on the first two sessions, so
    a window that ran through 10:00 would not match these levels. Bar count
    follows the requested window, not the 390-bar regular session.
    """
    bars = load_ohlcv(
        sample_fixture_path(),
        symbol="MES",
        rth_only=True,
        allow_synthetic=True,
    )
    assert str(bars.index.tz) == NY

    for minutes in (5, 15, 30):
        result = opening_ranges(bars, window_minutes=minutes)
        assert list(result.columns) == RESULT_COLUMNS
        assert result["session_date"].tolist() == list(FIXTURE_RANGES)
        assert result["window_minutes"].tolist() == [minutes, minutes, minutes]
        assert result["bar_count"].tolist() == [minutes, minutes, minutes]
        assert result["range_high"].dtype == "float64"
        assert result["range_low"].dtype == "float64"
        for row in result.itertuples(index=False):
            expected = FIXTURE_RANGES[row.session_date]
            assert isinstance(row.session_date, date)
            assert row.range_high == expected["range_high"]
            assert row.range_low == expected["range_low"]

    fifteen = opening_ranges(bars)
    assert DEFAULT_WINDOW_MINUTES == 15
    pd.testing.assert_frame_equal(fifteen, opening_ranges(bars, window_minutes=15))
    pd.testing.assert_frame_equal(
        fifteen, opening_ranges(bars.iloc[::-1], window_minutes=15)
    )
    trimmed = bars.loc[:, ["open", "high", "low", "close", "volume"]]
    pd.testing.assert_frame_equal(fifteen, opening_ranges(trimmed, window_minutes=15))

    # Non-RTH bars in the same file are outside the window.
    full = load_ohlcv(
        sample_fixture_path(),
        symbol="MES",
        rth_only=False,
        allow_synthetic=True,
    )
    pd.testing.assert_frame_equal(fifteen, opening_ranges(full, window_minutes=15))

    long_day = bars.loc["2024-01-16"]
    short_day = bars.loc["2024-01-17"]
    long_row = fifteen.loc[fifteen["session_date"] == date(2024, 1, 16)].iloc[0]
    short_row = fifteen.loc[fifteen["session_date"] == date(2024, 1, 17)].iloc[0]
    assert long_row["range_high"] < float(long_day["high"].max())
    assert short_row["range_low"] > float(short_day["low"].min())
    assert long_row["bar_count"] == 15
    assert len(long_day) == 390


def test_fifteen_minute_window_includes_0930_through_0944() -> None:
    """Bar stamps are start-of-minute, so W=15 is [09:30, 09:45)."""
    minutes = list(range(RTH_OPEN, RTH_OPEN + 16))
    highs = [10.0] * len(minutes)
    lows = [5.0] * len(minutes)
    highs[14] = 30.0  # 09:44
    lows[0] = 1.0  # 09:30
    highs[15] = 99.0  # 09:45, first bar after a 15-minute window
    lows[15] = 0.0
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-01-16", tz=NY) + pd.Timedelta(minutes=minute)
            for minute in minutes
        ],
        name="ts_event",
    )
    frame = _ohlcv(index, highs, lows)

    fifteen = opening_ranges(frame, window_minutes=15)
    assert len(fifteen) == 1
    assert fifteen.iloc[0]["range_high"] == 30.0
    assert fifteen.iloc[0]["range_low"] == 1.0
    assert fifteen.iloc[0]["bar_count"] == 15
    assert fifteen.iloc[0]["window_minutes"] == 15

    sixteen = opening_ranges(frame, window_minutes=16)
    assert sixteen.iloc[0]["range_high"] == 99.0
    assert sixteen.iloc[0]["range_low"] == 0.0
    assert sixteen.iloc[0]["bar_count"] == 16

    five_minutes = list(range(RTH_OPEN, RTH_OPEN + 30))
    five_high = [10.0] * len(five_minutes)
    five_low = [6.0] * len(five_minutes)
    five_high[4] = 12.0  # 09:34, inside 5 minutes
    five_low[0] = 3.0
    five_high[5] = 40.0  # 09:35, inside 15 and 30, outside 5
    five_low[14] = 2.0  # 09:44, inside 15
    five_high[29] = 70.0  # 09:59, inside 30 only
    five_low[29] = 1.0
    wider = _ohlcv(
        pd.DatetimeIndex(
            [
                pd.Timestamp("2024-01-17", tz=NY) + pd.Timedelta(minutes=minute)
                for minute in five_minutes
            ],
            name="ts_event",
        ),
        five_high,
        five_low,
    )
    by_window = {
        minutes: opening_ranges(wider, window_minutes=minutes).iloc[0]
        for minutes in (5, 15, 30)
    }
    assert by_window[5]["range_high"] == 12.0
    assert by_window[5]["range_low"] == 3.0
    assert by_window[5]["bar_count"] == 5
    assert by_window[15]["range_high"] == 40.0
    assert by_window[15]["range_low"] == 2.0
    assert by_window[15]["bar_count"] == 15
    assert by_window[30]["range_high"] == 70.0
    assert by_window[30]["range_low"] == 1.0
    assert by_window[30]["bar_count"] == 30


def test_window_starts_at_rth_open_not_the_first_bar() -> None:
    premarket = _ohlcv(_clock("2024-01-16", 8 * 60, 15), high=500.0, low=0.5)
    opening = _window("2024-01-16", 15, high=20.0, low=11.0)
    later = _ohlcv(_clock("2024-01-16", 10 * 60, 5), high=800.0, low=0.1)
    premarket_only = _ohlcv(_clock("2024-01-18", 8 * 60, 20), high=900.0, low=0.2)
    frame = pd.concat([premarket, opening, later, premarket_only]).sort_index()

    result = opening_ranges(frame, window_minutes=15)
    assert result["session_date"].tolist() == [date(2024, 1, 16)]
    assert result.iloc[0]["range_high"] == 20.0
    assert result.iloc[0]["range_low"] == 11.0
    assert result.iloc[0]["bar_count"] == 15
    assert opening_ranges(premarket_only, window_minutes=15).empty


def test_full_rth_window_excludes_the_1600_bar() -> None:
    index = _clock("2024-01-16", RTH_OPEN, 390)
    assert index[0].time() == time(9, 30)
    assert index[-1].time() == time(15, 59)
    highs = [10.0] * 390
    lows = [5.0] * 390
    highs[-1] = 25.0
    lows[0] = 2.0
    closing = _ohlcv(
        pd.DatetimeIndex(
            [pd.Timestamp("2024-01-16 16:00", tz=NY)], name="ts_event"
        ),
        high=1000.0,
        low=0.0,
    )
    result = opening_ranges(
        pd.concat([_ohlcv(index, highs, lows), closing]),
        window_minutes=390,
    )
    assert len(result) == 1
    assert result.iloc[0]["range_high"] == 25.0
    assert result.iloc[0]["range_low"] == 2.0
    assert result.iloc[0]["bar_count"] == 390
    assert result.iloc[0]["window_minutes"] == 390


def test_incomplete_sessions_are_skipped() -> None:
    good = _window("2024-01-16", 15, high=101.0, low=90.0)
    cases = {
        "missing_open": _window("2024-01-17", 15).iloc[1:],
        "missing_middle": _window("2024-01-17", 15).drop(
            _window("2024-01-17", 15).index[7]
        ),
        "duplicate_open": pd.concat(
            [_window("2024-01-17", 15), _window("2024-01-17", 15).iloc[[0]]]
        ),
        "subminute_stamp": _shift_one_bar(_window("2024-01-17", 15), seconds=30),
    }
    for name, broken in cases.items():
        result = opening_ranges(pd.concat([broken, good]), window_minutes=15)
        assert result["session_date"].tolist() == [date(2024, 1, 16)], name
        assert result.iloc[0]["range_high"] == 101.0
        assert result.iloc[0]["range_low"] == 90.0
        assert result.iloc[0]["bar_count"] == 15

    only_broken = opening_ranges(cases["missing_open"], window_minutes=15)
    assert only_broken.empty
    assert list(only_broken.columns) == RESULT_COLUMNS
    assert only_broken["range_high"].dtype == "float64"
    assert only_broken["bar_count"].dtype == "int64"


def test_utc_timestamps_follow_new_york_rth_across_dst() -> None:
    """09:30 ET is 14:30 UTC in January and 13:30 UTC in July.

    A July 14:30 UTC bar is 10:30 EDT, outside a 15-minute opening range.
    Treating UTC as a fixed EST offset would take that bar as 09:30.
    """
    winter = pd.date_range(
        "2024-01-16 14:30", periods=15, freq="min", tz="UTC", name="ts_event"
    )
    summer = pd.date_range(
        "2024-07-16 13:30", periods=15, freq="min", tz="UTC", name="ts_event"
    )
    winter_trap = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-16 14:29", tz="UTC")], name="ts_event"
    )
    summer_trap = pd.DatetimeIndex(
        [pd.Timestamp("2024-07-16 14:30", tz="UTC")], name="ts_event"
    )
    frame = pd.concat(
        [
            _ohlcv(winter_trap, high=999.0, low=0.0),
            _ohlcv(winter, high=50.0, low=40.0),
            _ohlcv(summer, high=80.0, low=70.0),
            _ohlcv(summer_trap, high=999.0, low=0.0),
        ]
    )
    result = opening_ranges(frame, window_minutes=15).set_index("session_date")
    assert list(result.index) == [date(2024, 1, 16), date(2024, 7, 16)]
    assert result.loc[date(2024, 1, 16), "range_high"] == 50.0
    assert result.loc[date(2024, 1, 16), "range_low"] == 40.0
    assert result.loc[date(2024, 7, 16), "range_high"] == 80.0
    assert result.loc[date(2024, 7, 16), "range_low"] == 70.0
    assert result["bar_count"].tolist() == [15, 15]

    winter_ny = winter.tz_convert(NY)
    summer_ny = summer.tz_convert(NY)
    assert winter_ny[0].utcoffset() == timedelta(hours=-5)
    assert summer_ny[0].utcoffset() == timedelta(hours=-4)
    assert winter_ny[0].time() == time(9, 30)
    assert summer_ny[0].time() == time(9, 30)
    direct = opening_ranges(
        pd.concat(
            [
                _ohlcv(winter_ny, high=50.0, low=40.0),
                _ohlcv(summer_ny, high=80.0, low=70.0),
            ]
        ),
        window_minutes=15,
    ).set_index("session_date")
    pd.testing.assert_frame_equal(result, direct)


def test_naive_index_and_bad_inputs_are_rejected() -> None:
    naive = _ohlcv(
        pd.date_range("2024-01-16 09:30", periods=15, freq="min", name="ts_event"),
        high=2.0,
        low=1.0,
    )
    assert naive.index.tz is None
    with pytest.raises(ValueError, match="naive"):
        opening_ranges(naive)

    with pytest.raises(ValueError, match="high"):
        opening_ranges(naive.tz_localize(NY).drop(columns=["high"]))

    strings = naive.tz_localize(NY).copy()
    strings["high"] = "10"
    with pytest.raises(ValueError, match="numeric"):
        opening_ranges(strings)

    mixed = pd.concat(
        [
            _window("2024-01-16", 15).assign(symbol="MES"),
            _window("2024-01-16", 15).assign(symbol="ES"),
        ]
    )
    with pytest.raises(ValueError, match="multiple symbols"):
        opening_ranges(mixed)

    with pytest.raises(TypeError, match="DataFrame"):
        opening_ranges([1, 2, 3])  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [True, False, 15.0, "15", None])
def test_window_minutes_rejects_non_ints(window: object) -> None:
    frame = _window("2024-01-16", 15)
    with pytest.raises(TypeError, match="int"):
        opening_ranges(frame, window_minutes=window)  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [0, -1, 391])
def test_window_minutes_must_lie_inside_rth(window: int) -> None:
    frame = _window("2024-01-16", 1)
    with pytest.raises(ValueError, match="window_minutes"):
        opening_ranges(frame, window_minutes=window)


def test_empty_frame_and_input_are_unchanged() -> None:
    empty = pd.DataFrame(
        {
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="int64"),
            "symbol": pd.Series(dtype="string"),
        },
        index=pd.DatetimeIndex([], tz=NY, name="ts_event"),
    )
    result = opening_ranges(empty)
    assert result.empty
    assert list(result.columns) == RESULT_COLUMNS

    bars = load_ohlcv(sample_fixture_path(), symbol="MES", allow_synthetic=True)
    before = bars.copy()
    opening_ranges(bars, window_minutes=5)
    pd.testing.assert_frame_equal(bars, before)


def _shift_one_bar(frame: pd.DataFrame, *, seconds: int) -> pd.DataFrame:
    shifted = frame.copy()
    stamps = list(frame.index)
    stamps[4] = stamps[4] + pd.Timedelta(seconds=seconds)
    shifted.index = pd.DatetimeIndex(stamps, name=frame.index.name)
    return shifted
