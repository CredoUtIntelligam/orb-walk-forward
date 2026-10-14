"""Unit tests for opening-range breakout signals."""

from __future__ import annotations

import csv
import inspect
import json
from dataclasses import FrozenInstanceError, fields
from datetime import date, time, timedelta

import pandas as pd
import pytest

from orb import (
    DEFAULT_TRIGGER,
    Signal,
    breakout_signals,
    load_ohlcv,
    opening_ranges,
    sample_fixture_path,
    signal_bars,
)
from orb.breakout import LONG, SHORT, TRIGGER_CLOSE, TRIGGER_HIGH_LOW

NY = "America/New_York"
RTH_OPEN = 9 * 60 + 30

# Levels printed on the synthetic fixture. The 10:00 ET bar is the first
# break, and its close is the default entry reference.
FIXTURE_SIGNALS = {
    date(2024, 1, 16): {
        "direction": LONG,
        "entry_price": 5011.0,
        "level_price": 5008.0,
        "or_high": 5008.0,
        "or_low": 5000.0,
    },
    date(2024, 1, 17): {
        "direction": SHORT,
        "entry_price": 5047.0,
        "level_price": 5050.0,
        "or_high": 5058.0,
        "or_low": 5050.0,
    },
}


def _clock(
    day: str,
    start_minute: int,
    count: int,
    *,
    tz: str = NY,
) -> pd.DatetimeIndex:
    anchor = pd.Timestamp(day, tz=tz) + pd.Timedelta(minutes=start_minute)
    return pd.date_range(anchor, periods=count, freq="min", name="ts_event")


def _levels(value: float | list[float], count: int) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    return [float(value)] * count


def _session(
    day: str,
    start_minute: int,
    count: int,
    *,
    high: float | list[float] = 10.0,
    low: float | list[float] = 6.0,
    close: float | list[float] = 8.0,
    symbol: str | list[str] = "MES",
    tz: str = NY,
) -> pd.DataFrame:
    index = _clock(day, start_minute, count, tz=tz)
    symbols = [symbol] * count if isinstance(symbol, str) else list(symbol)
    return pd.DataFrame(
        {
            "open": [8.0] * count,
            "high": _levels(high, count),
            "low": _levels(low, count),
            "close": _levels(close, count),
            "volume": [1] * count,
            "symbol": symbols,
        },
        index=index,
    )


def _ranges(rows: list[tuple[date, float, float, int]]) -> pd.DataFrame:
    # Keep ``session_date`` as ``datetime.date``, matching ``opening_ranges``.
    return pd.DataFrame(
        {
            "session_date": pd.Series([row[0] for row in rows], dtype="object"),
            "range_high": pd.array([row[1] for row in rows], dtype="float64"),
            "range_low": pd.array([row[2] for row in rows], dtype="float64"),
            "window_minutes": pd.array([row[3] for row in rows], dtype="int64"),
            "bar_count": pd.array([row[3] for row in rows], dtype="int64"),
        }
    )


def _signal(
    day: date,
    direction: str,
    clock: str,
    entry_price: float,
    *,
    or_high: float = 10.0,
    or_low: float = 6.0,
    window_minutes: int = 15,
    trigger: str = TRIGGER_CLOSE,
) -> Signal:
    return Signal(
        session_date=day,
        direction=direction,
        or_high=or_high,
        or_low=or_low,
        entry_ts=pd.Timestamp(f"{day.isoformat()} {clock}", tz=NY),
        entry_price=entry_price,
        window_minutes=window_minutes,
        trigger=trigger,
    )


def test_signal_fields_are_frozen_and_one_per_day() -> None:
    """The public record is ``Signal``, frozen, with one row per session."""
    assert [field.name for field in fields(Signal)] == [
        "session_date",
        "direction",
        "or_high",
        "or_low",
        "entry_ts",
        "entry_price",
        "window_minutes",
        "trigger",
    ]
    bars = pd.concat(
        [
            _session(
                "2024-01-16",
                RTH_OPEN + 15,
                2,
                high=[13.0, 8.0],
                low=[9.0, 0.0],
                close=[12.0, 1.0],
            ),
            _session(
                "2024-01-17",
                RTH_OPEN + 15,
                1,
                high=9.0,
                low=8.0,
                close=8.5,
            ),
        ]
    )
    ranges = _ranges(
        [
            (date(2024, 1, 16), 10.0, 6.0, 15),
            (date(2024, 1, 17), 10.0, 6.0, 15),
        ]
    )
    signals = breakout_signals(bars, ranges)
    assert isinstance(signals, list)
    assert len(signals) == 1
    signal = signals[0]
    assert isinstance(signal, Signal)
    assert signal.direction in (LONG, SHORT)
    assert signal.session_date == date(2024, 1, 16)
    assert signal.or_high == 10.0
    assert signal.or_low == 6.0
    assert signal.entry_ts == pd.Timestamp("2024-01-16 09:45", tz=NY)
    assert signal.entry_price == 12.0
    assert signal.window_minutes == 15
    assert signal.trigger == TRIGGER_CLOSE
    payload = signal.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["session_date"] == "2024-01-16"
    assert payload["entry_ts"] == "2024-01-16T09:45:00-05:00"
    assert payload["window_minutes"] == 15
    assert payload["trigger"] == "close"
    with pytest.raises(FrozenInstanceError):
        signal.entry_price = 0.0  # type: ignore[misc]


def test_long_breakout_uses_the_first_close_above_the_high() -> None:
    """A wick through the high is not the default signal. The first close is.

    The 09:46 close is the signal. A later, higher close does not replace
    it, and ``high_low`` instead signals on the earlier wick at the range
    high rather than at the wick price.
    """
    bars = _session(
        "2024-01-16",
        RTH_OPEN + 15,
        3,
        high=[13.0, 11.0, 15.0],
        low=[7.0, 9.0, 11.0],
        close=[10.0, 10.5, 14.0],
    )
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])

    signals = breakout_signals(bars, ranges)
    assert DEFAULT_TRIGGER == TRIGGER_CLOSE
    assert signals == breakout_signals(bars, ranges, trigger="close")
    assert signals == [_signal(date(2024, 1, 16), LONG, "09:46", 10.5)]
    assert signals[0].entry_ts.time() == time(9, 46)

    pierced = breakout_signals(bars, ranges, trigger=TRIGGER_HIGH_LOW)
    assert pierced == [
        _signal(date(2024, 1, 16), LONG, "09:45", 10.0, trigger=TRIGGER_HIGH_LOW)
    ]
    assert float(bars.iloc[0]["high"]) == 13.0


def test_short_breakout_uses_the_first_close_below_the_low() -> None:
    """Tagging the low is not a short. The first close below it is."""
    bars = _session(
        "2024-01-17",
        RTH_OPEN + 15,
        3,
        high=[9.0, 7.0, 6.5],
        low=[4.0, 5.0, 3.0],
        close=[6.0, 5.75, 4.0],
    )
    ranges = _ranges([(date(2024, 1, 17), 10.0, 6.0, 15)])

    assert breakout_signals(bars, ranges) == [
        _signal(date(2024, 1, 17), SHORT, "09:46", 5.75)
    ]


def test_no_breakout_day_emits_no_signal() -> None:
    """Equality with the level is inside the range. A flat day adds nothing."""
    flat = _session(
        "2024-01-18",
        RTH_OPEN,
        20,
        high=10.0,
        low=6.0,
        close=[10.0, 6.0] * 10,
    )
    ranges = _ranges(
        [
            (date(2024, 1, 18), 10.0, 6.0, 15),
            (date(2024, 1, 19), 10.0, 6.0, 15),
        ]
    )
    assert breakout_signals(flat, ranges) == []
    assert breakout_signals(flat, ranges, trigger="high_low") == []

    later = _session(
        "2024-01-19",
        RTH_OPEN + 15,
        1,
        high=12.0,
        low=8.0,
        close=11.0,
    )
    mixed = breakout_signals(pd.concat([flat, later]), ranges)
    assert [signal.session_date for signal in mixed] == [date(2024, 1, 19)]


def test_only_the_first_breakout_signals_when_both_sides_break() -> None:
    """The first side wins. The opposite break later that day is ignored."""
    long_then_short = _session(
        "2024-01-16",
        RTH_OPEN + 15,
        2,
        high=[13.0, 8.0],
        low=[9.0, 0.0],
        close=[12.0, 1.0],
    )
    short_then_long = _session(
        "2024-01-17",
        RTH_OPEN + 15,
        2,
        high=[8.0, 13.0],
        low=[0.0, 9.0],
        close=[1.0, 12.0],
    )
    # Ranges and bars are intentionally out of date order.
    ranges = _ranges(
        [
            (date(2024, 1, 17), 10.0, 6.0, 15),
            (date(2024, 1, 16), 10.0, 6.0, 15),
        ]
    )
    signals = breakout_signals(pd.concat([short_then_long, long_then_short]), ranges)
    assert isinstance(signals, list)
    assert len(signals) == len({signal.session_date for signal in signals}) == 2
    assert signals == [
        _signal(date(2024, 1, 16), LONG, "09:45", 12.0),
        _signal(date(2024, 1, 17), SHORT, "09:45", 1.0),
    ]


def test_bars_inside_the_range_window_do_not_signal() -> None:
    """09:44 is inside a 15-minute window and eligible when the window is 14.

    The supplied range is tighter than the 09:44 print, so this fails if
    window bars are scanned. A 09:29 print outside the range is also ignored.
    """
    count = 16  # 09:30 through 09:45
    highs = [10.0] * count
    lows = [6.0] * count
    closes = [8.0] * count
    highs[14] = 13.0
    closes[14] = 12.0
    opening = _session(
        "2024-01-16",
        RTH_OPEN,
        count,
        high=highs,
        low=lows,
        close=closes,
    )
    premarket = _session(
        "2024-01-16",
        9 * 60 + 29,
        1,
        high=50.0,
        low=1.0,
        close=40.0,
    )
    bars = pd.concat([premarket, opening])
    fifteen = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    fourteen = _ranges([(date(2024, 1, 16), 10.0, 6.0, 14)])

    assert breakout_signals(bars, fifteen) == []
    assert breakout_signals(bars, fifteen, trigger="high_low") == []
    assert breakout_signals(bars, fourteen) == [
        _signal(date(2024, 1, 16), LONG, "09:44", 12.0, window_minutes=14)
    ]


def test_high_low_skips_a_bar_that_breaks_both_sides() -> None:
    """One bar through both levels does not pick a side.

    Under ``high_low`` that bar is skipped and a later one-sided break can
    still signal. Under ``close`` the same bar is a normal one-sided signal
    when its close is beyond only one level.
    """
    both = _session(
        "2024-01-16",
        RTH_OPEN + 15,
        1,
        high=13.0,
        low=1.0,
        close=12.0,
    )
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    assert breakout_signals(both, ranges, trigger="high_low") == []
    assert breakout_signals(both, ranges, trigger="close") == [
        _signal(date(2024, 1, 16), LONG, "09:45", 12.0)
    ]

    followed = pd.concat(
        [
            both,
            _session(
                "2024-01-16",
                RTH_OPEN + 16,
                1,
                high=9.0,
                low=5.0,
                close=5.5,
            ),
        ]
    )
    assert breakout_signals(followed, ranges, trigger="high_low") == [
        _signal(date(2024, 1, 16), SHORT, "09:46", 6.0, trigger=TRIGGER_HIGH_LOW)
    ]


def test_subminute_and_post_rth_bars_do_not_signal() -> None:
    """Only minute-aligned stamps in ``[window end, 16:00)`` can signal."""
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    late = _session("2024-01-16", 15 * 60 + 59, 1, high=12.0, low=11.0, close=12.0)
    at_close = _session("2024-01-16", 16 * 60, 1, high=12.0, low=11.0, close=12.0)
    after_close = _session(
        "2024-01-16", 16 * 60 + 15, 1, high=12.0, low=11.0, close=12.0
    )
    assert breakout_signals(late, ranges) == [
        _signal(date(2024, 1, 16), LONG, "15:59", 12.0)
    ]
    assert breakout_signals(pd.concat([at_close, after_close]), ranges) == []

    fractional = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-16 09:45:30", tz=NY)],
        name="ts_event",
    )
    poked = _session("2024-01-16", RTH_OPEN + 15, 1, high=12.0, low=11.0, close=12.0)
    poked.index = fractional
    aligned = _session(
        "2024-01-16", RTH_OPEN + 16, 1, high=12.0, low=11.0, close=11.5
    )
    signals = breakout_signals(pd.concat([poked, aligned]), ranges)
    assert signals == [_signal(date(2024, 1, 16), LONG, "09:46", 11.5)]


def test_utc_timestamps_signal_on_the_new_york_clock() -> None:
    """09:45 ET is 14:45 UTC in January and 13:45 UTC in July.

    The bar one minute earlier is still inside a 15-minute window. A fixed
    EST offset would treat the July 13:45 UTC bar as 08:45 and drop it.
    """
    stamps = [
        "2024-01-16 14:44",
        "2024-01-16 14:45",
        "2024-07-16 13:44",
        "2024-07-16 13:45",
    ]
    index = pd.DatetimeIndex(
        [pd.Timestamp(stamp, tz="UTC") for stamp in stamps],
        name="ts_event",
    )
    bars = pd.DataFrame(
        {
            "open": [8.0, 8.0, 8.0, 8.0],
            "high": [21.0, 22.0, 9.0, 9.0],
            "low": [7.0, 7.0, 1.0, 2.0],
            "close": [20.0, 21.0, 1.5, 2.0],
            "volume": [1, 1, 1, 1],
            "symbol": ["MES"] * 4,
        },
        index=index,
    )
    ranges = _ranges(
        [
            (date(2024, 7, 16), 10.0, 6.0, 15),
            (date(2024, 1, 16), 10.0, 6.0, 15),
        ]
    )
    signals = breakout_signals(bars, ranges)
    assert signals == [
        _signal(date(2024, 1, 16), LONG, "09:45", 21.0),
        _signal(date(2024, 7, 16), SHORT, "09:45", 2.0),
    ]
    assert signals[0].entry_ts.utcoffset() == timedelta(hours=-5)
    assert signals[1].entry_ts.utcoffset() == timedelta(hours=-4)
    assert str(bars.index.tz) == "UTC"


def test_fixture_emits_long_short_and_no_breakout() -> None:
    """The bundled sessions signal at 10:00 ET, or not at all.

    2024-01-16 closes above its range, 2024-01-17 closes below its range,
    and 2024-01-18 stays inside. The 09:31 bar that prints the range extreme
    is inside the window, so it is not the signal. Non-RTH bars and input
    order do not change the result. The load uses ``allow_synthetic=True``
    and the recommended path ``signal_bars`` then ``opening_ranges``.
    """
    loaded = load_ohlcv(
        sample_fixture_path(),
        symbol="MES",
        rth_only=True,
        allow_synthetic=True,
    )
    bars = signal_bars(loaded)
    assert not bool(bars["early_close"].any())
    ranges = opening_ranges(bars)
    signals = breakout_signals(bars, ranges)
    assert signals == [
        Signal(
            session_date=day,
            direction=spec["direction"],
            or_high=spec["or_high"],
            or_low=spec["or_low"],
            entry_ts=pd.Timestamp(f"{day.isoformat()} 10:00", tz=NY),
            entry_price=spec["entry_price"],
            window_minutes=15,
            trigger=TRIGGER_CLOSE,
        )
        for day, spec in FIXTURE_SIGNALS.items()
    ]
    assert date(2024, 1, 18) not in [signal.session_date for signal in signals]
    for signal in signals:
        assert isinstance(signal, Signal)
        assert isinstance(signal.session_date, date)
        assert isinstance(signal.entry_price, float)
        assert signal.entry_ts.time() == time(10, 0)
        assert str(signal.entry_ts.tz) == NY

    touched = breakout_signals(bars, ranges, trigger="high_low")
    assert [signal.entry_ts for signal in touched] == [
        signal.entry_ts for signal in signals
    ]
    assert [signal.direction for signal in touched] == [LONG, SHORT]
    assert [signal.trigger for signal in touched] == [TRIGGER_HIGH_LOW, TRIGGER_HIGH_LOW]
    assert [signal.entry_price for signal in touched] == [
        spec["level_price"] for spec in FIXTURE_SIGNALS.values()
    ]

    full = load_ohlcv(
        sample_fixture_path(),
        symbol="MES",
        rth_only=False,
        allow_synthetic=True,
    )
    eligible = signal_bars(full)
    assert breakout_signals(eligible, opening_ranges(eligible)) == signals
    assert breakout_signals(full, opening_ranges(full)) == signals
    assert breakout_signals(bars.iloc[::-1], ranges) == signals


def test_empty_inputs_emit_nothing_and_frames_are_unchanged() -> None:
    bars = _session("2024-01-16", RTH_OPEN, 16, close=12.0, high=12.0, low=7.0)
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    before_bars = bars.copy()
    before_ranges = ranges.copy()

    assert breakout_signals(bars.iloc[0:0], ranges) == []
    assert breakout_signals(bars, ranges.iloc[0:0]) == []
    breakout_signals(bars, ranges, trigger="high_low")
    pd.testing.assert_frame_equal(bars, before_bars)
    pd.testing.assert_frame_equal(ranges, before_ranges)

    empty_ranges = pd.DataFrame(columns=list(ranges.columns))
    assert breakout_signals(bars, empty_ranges) == []


def test_bad_bars_and_ranges_are_rejected() -> None:
    bars = _session("2024-01-16", RTH_OPEN + 15, 1, close=12.0, high=12.0, low=7.0)
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])

    naive = bars.tz_localize(None)
    assert naive.index.tz is None
    with pytest.raises(ValueError, match="naive"):
        breakout_signals(naive, ranges)

    with pytest.raises(ValueError, match="close"):
        breakout_signals(bars.drop(columns=["close"]), ranges)

    es = bars.copy()
    es.index = es.index + pd.Timedelta(minutes=1)
    es["symbol"] = "ES"
    with pytest.raises(ValueError, match="multiple symbols"):
        breakout_signals(pd.concat([bars, es]), ranges)

    with pytest.raises(ValueError, match="duplicate"):
        breakout_signals(pd.concat([bars, bars]), ranges)

    with pytest.raises(TypeError, match="DataFrame"):
        breakout_signals(bars, [])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DataFrame"):
        breakout_signals([], ranges)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="range_low"):
        breakout_signals(bars, ranges.drop(columns=["range_low"]))

    inverted = _ranges([(date(2024, 1, 16), 6.0, 10.0, 15)])
    with pytest.raises(ValueError, match="range_high"):
        breakout_signals(bars, inverted)

    with pytest.raises(ValueError, match="duplicate session_date"):
        breakout_signals(bars, pd.concat([ranges, ranges], ignore_index=True))


@pytest.mark.parametrize("trigger", [None, 1, True, "wick", "CLOSE", "touch"])
def test_trigger_must_be_close_or_high_low(trigger: object) -> None:
    bars = _session("2024-01-16", RTH_OPEN + 15, 1, close=12.0, high=12.0, low=7.0)
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    with pytest.raises((TypeError, ValueError), match="high_low"):
        breakout_signals(bars, ranges, trigger=trigger)  # type: ignore[arg-type]


@pytest.mark.parametrize("window", [0, -1, 391])
def test_window_minutes_must_lie_inside_rth(window: int) -> None:
    bars = _session("2024-01-16", RTH_OPEN + 15, 1, close=12.0, high=12.0, low=7.0)
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    ranges["window_minutes"] = window
    with pytest.raises(ValueError, match="window_minutes"):
        breakout_signals(bars, ranges)


def test_window_minutes_rejects_non_integers() -> None:
    bars = _session("2024-01-16", RTH_OPEN + 15, 1, close=12.0, high=12.0, low=7.0)
    ranges = _ranges([(date(2024, 1, 16), 10.0, 6.0, 15)])
    ranges["window_minutes"] = 15.0
    with pytest.raises(TypeError, match="int"):
        breakout_signals(bars, ranges)


def test_bundled_fixture_requires_allow_synthetic() -> None:
    """The synthetic fixture is refused unless the caller opts in."""
    with pytest.raises(ValueError, match="allow_synthetic"):
        load_ohlcv(sample_fixture_path(), symbol="MES", rth_only=True)


def test_breakout_keeps_rows_flagged_early_close() -> None:
    """A flagged early-close day still signals. The flag stays on the frame.

    ``early_close`` is metadata. ``breakout_signals`` does not drop those
    rows, and there is no parameter that removes them.
    """
    assert "include_early_close" not in inspect.signature(breakout_signals).parameters
    assert "exclude_early_close" not in inspect.signature(breakout_signals).parameters
    assert list(inspect.signature(signal_bars).parameters) == ["bars"]

    early = _breakout_day("2024-01-16")
    early["early_close"] = True
    regular = _breakout_day("2024-01-17")
    regular["early_close"] = False
    bars = pd.concat([early, regular])
    before = bars.copy()
    ranges = opening_ranges(bars)
    assert ranges["session_date"].tolist() == [date(2024, 1, 16), date(2024, 1, 17)]

    signals = breakout_signals(bars, ranges)
    assert signals == [
        _signal(date(2024, 1, 16), LONG, "09:45", 11.0),
        _signal(date(2024, 1, 17), LONG, "09:45", 11.0),
    ]
    pd.testing.assert_frame_equal(bars, before)
    assert bool(bars.loc[bars.index.date == date(2024, 1, 16), "early_close"].all())


def test_recommended_path_includes_early_close_sessions(tmp_path) -> None:
    """load → signal_bars → opening_ranges → breakout_signals keeps the halt.

    2024-07-03 is a CME equity early close (halt before 16:00 ET) with a
    complete 15-minute opening range and a later close beyond it, still
    before the halt. 2024-07-05 is a full session with the same shape.
    Both sessions signal, and the early-close flag stays true.
    """
    path = tmp_path / "early-close-breakout.csv"
    _write_breakout_csv(
        path,
        [_breakout_row(day, minute) for day in ("2024-07-03", "2024-07-05") for minute in range(16)],
    )
    loaded = load_ohlcv(path, symbol="MES", rth_only=True)
    july3 = _on_day(loaded, "2024-07-03")
    july5 = _on_day(loaded, "2024-07-05")
    assert not july3.empty
    assert bool(july3["early_close"].all())
    assert not bool(july5["early_close"].any())

    eligible = signal_bars(loaded)
    july3_signals = _on_day(eligible, "2024-07-03")
    assert not july3_signals.empty
    assert bool(july3_signals["early_close"].all())
    ranges = opening_ranges(eligible)
    assert ranges["session_date"].tolist() == [date(2024, 7, 3), date(2024, 7, 5)]
    signals = breakout_signals(eligible, ranges)
    assert signals == [
        _signal(date(2024, 7, 3), LONG, "09:45", 11.0),
        _signal(date(2024, 7, 5), LONG, "09:45", 11.0),
    ]

    raw_ranges = opening_ranges(loaded)
    assert date(2024, 7, 3) in set(raw_ranges["session_date"])
    assert [signal.session_date for signal in breakout_signals(loaded, raw_ranges)] == [
        date(2024, 7, 3),
        date(2024, 7, 5),
    ]


def test_early_close_entry_stops_at_the_session_halt() -> None:
    """A shortened session does not signal at or after its close.

    2024-07-03 halts at 13:00 ET. A 12:59 close can be the entry. A 13:30
    close is past the halt, so that day emits nothing when it is the only
    break.
    """
    before_halt = _session(
        "2024-07-03",
        RTH_OPEN,
        1,
        high=10.0,
        low=6.0,
        close=8.0,
    )
    at_1259 = _session("2024-07-03", 12 * 60 + 59, 1, high=12.0, low=11.0, close=11.5)
    past_halt = _session("2024-07-03", 13 * 60 + 30, 1, high=14.0, low=13.0, close=13.5)
    ranges = _ranges([(date(2024, 7, 3), 10.0, 6.0, 15)])

    signaled = breakout_signals(pd.concat([before_halt, at_1259, past_halt]), ranges)
    assert signaled == [_signal(date(2024, 7, 3), LONG, "12:59", 11.5)]

    only_late = breakout_signals(pd.concat([before_halt, past_halt]), ranges)
    assert only_late == []


def test_signal_rejects_bad_direction_and_field_types() -> None:
    """Direction is only long or short, and each field has to match its type."""
    good = _signal(date(2024, 1, 16), LONG, "09:45", 11.0)
    assert good.to_dict()["direction"] == "long"

    with pytest.raises(ValueError, match="direction"):
        _signal(date(2024, 1, 16), "buy", "09:45", 11.0)
    with pytest.raises(TypeError, match="session_date"):
        Signal(
            session_date=pd.Timestamp("2024-01-16 09:45", tz=NY),  # type: ignore[arg-type]
            direction=LONG,
            or_high=10.0,
            or_low=6.0,
            entry_ts=pd.Timestamp("2024-01-16 09:45", tz=NY),
            entry_price=11.0,
            window_minutes=15,
            trigger=TRIGGER_CLOSE,
        )
    with pytest.raises(TypeError, match="entry_price"):
        _signal(date(2024, 1, 16), LONG, "09:45", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="naive"):
        Signal(
            session_date=date(2024, 1, 16),
            direction=SHORT,
            or_high=10.0,
            or_low=6.0,
            entry_ts=pd.Timestamp("2024-01-16 09:45"),
            entry_price=11.0,
            window_minutes=15,
            trigger=TRIGGER_CLOSE,
        )
    with pytest.raises(TypeError, match="window_minutes"):
        _signal(date(2024, 1, 16), LONG, "09:45", 11.0, window_minutes=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="close_5m"):
        _signal(date(2024, 1, 16), LONG, "09:45", 11.0, trigger="wick")


def _breakout_day(day: str) -> pd.DataFrame:
    """16 RTH minutes: a 15-minute range, then a close above that high."""
    return _session(
        day,
        RTH_OPEN,
        16,
        high=[10.0] * 15 + [12.0],
        low=[6.0] * 15 + [8.0],
        close=[8.0] * 15 + [11.0],
    )


def _breakout_row(day: str, offset: int) -> dict[str, object]:
    stamp = pd.Timestamp(f"{day} 09:30", tz=NY) + pd.Timedelta(minutes=offset)
    utc = stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
    if offset < 15:
        high, low, close = 10.0, 6.0, 8.0
    else:
        high, low, close = 12.0, 8.0, 11.0
    return {
        "ts_event": utc,
        "rtype": 33,
        "publisher_id": 1,
        "instrument_id": 9000001,
        "open": 8.0,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10,
        "symbol": "MES",
    }


def _write_breakout_csv(path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "ts_event",
        "rtype",
        "publisher_id",
        "instrument_id",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "symbol",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _on_day(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    start = pd.Timestamp(day, tz=NY)
    return frame.loc[(frame.index >= start) & (frame.index < start + pd.Timedelta(days=1))]
