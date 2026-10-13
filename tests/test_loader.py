"""Unit tests for the Databento-style OHLCV loader."""

from __future__ import annotations

import csv
from datetime import time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from orb import load_ohlcv, sample_fixture_path

COLUMNS = [
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

# Opening-range extremes printed on the 09:30 and 09:31 bars.
SCENARIOS = {
    "2024-01-16": {"side": "long", "or_low": 5000.0, "or_high": 5008.0},
    "2024-01-17": {"side": "short", "or_low": 5050.0, "or_high": 5058.0},
    "2024-01-18": {"side": "none", "or_low": 5100.0, "or_high": 5110.0},
}


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _bar(
    ts_event: str,
    *,
    symbol: str = "MES",
    open_: float = 5000.0,
    high: float = 5001.0,
    low: float = 4999.0,
    close: float = 5000.5,
    volume: int = 10,
) -> dict[str, object]:
    return {
        "ts_event": ts_event,
        "rtype": 33,
        "publisher_id": 1,
        "instrument_id": 9000001,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "symbol": symbol,
    }


def test_fixture_columns_and_dtypes() -> None:
    path = sample_fixture_path()
    assert path.is_file()
    assert "SYNTHETIC_TEST_DATA" in path.name
    readme = (path.parent / "README.md").read_text(encoding="utf-8")
    assert "SYNTHETIC TEST DATA" in readme
    assert "not a Databento download" in readme

    frame = load_ohlcv(path)
    assert list(frame.columns[:6]) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "symbol",
    ]
    assert {"rtype", "publisher_id", "instrument_id"} <= set(frame.columns)
    assert frame.index.name == "ts_event"
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "America/New_York"
    assert frame["open"].dtype == "float64"
    assert frame["high"].dtype == "float64"
    assert frame["low"].dtype == "float64"
    assert frame["close"].dtype == "float64"
    assert frame["volume"].dtype == "int64"
    assert frame["symbol"].dtype == "string"
    assert frame.index.is_monotonic_increasing
    assert set(frame["symbol"].unique()) == {"MES"}

    opening = pd.Timestamp("2024-01-16 09:30", tz="America/New_York")
    assert frame.index.min() < opening
    assert opening in frame.index
    assert int(frame.loc[[opening], "volume"].iloc[0]) > 0


def test_timezone_conversion_handles_dst(tmp_path: Path) -> None:
    """09:30 New York is 14:30 UTC in EST and 13:30 UTC in EDT."""
    path = tmp_path / "dst.csv"
    # (utc timestamp, expected offset hours)
    cases = [
        ("2024-01-16T14:30:00.000000000Z", -5),  # mid-winter EST
        ("2024-03-08T14:30:00.000000000Z", -5),  # Friday before spring forward
        ("2024-03-10T13:30:00.000000000Z", -4),  # spring-forward Sunday, EDT
        ("2024-03-11T13:30:00+00:00", -4),  # Monday after spring forward
        ("2024-07-16T13:30:00.000000000Z", -4),  # mid-summer EDT
        ("2024-11-01T13:30:00.000000000Z", -4),  # Friday before fall back
        ("2024-11-04T14:30:00.000000000Z", -5),  # Monday after fall back
    ]
    _write_csv(path, [_bar(ts) for ts, _offset in cases])

    frame = load_ohlcv(path)
    assert len(frame) == len(cases)
    for timestamp, (_raw, offset) in zip(frame.index, cases, strict=True):
        assert timestamp.tzinfo is not None
        assert str(timestamp.tzinfo) == "America/New_York"
        assert timestamp.hour == 9
        assert timestamp.minute == 30
        assert timestamp.utcoffset() == timedelta(hours=offset)


def test_integer_nanosecond_timestamps_convert_to_new_york(tmp_path: Path) -> None:
    summer = pd.Timestamp("2024-07-16T13:30:00Z")
    winter = pd.Timestamp("2024-01-16T14:30:00Z")
    path = tmp_path / "nanos.csv"
    _write_csv(
        path,
        [
            _bar(str(winter.value), close=1.0),
            _bar(str(summer.value), close=2.0),
        ],
    )

    frame = load_ohlcv(path)
    assert frame.index[0] == pd.Timestamp("2024-01-16 09:30", tz="America/New_York")
    assert frame.index[0].utcoffset() == timedelta(hours=-5)
    assert frame.index[1] == pd.Timestamp("2024-07-16 09:30", tz="America/New_York")
    assert frame.index[1].utcoffset() == timedelta(hours=-4)
    assert list(frame["close"]) == [1.0, 2.0]


def test_rth_filter_uses_new_york_clock(tmp_path: Path) -> None:
    path = tmp_path / "rth.csv"
    rows = [
        _bar("2024-01-16T14:29:00.000000000Z", close=1),  # 09:29 EST out
        _bar("2024-01-16T14:30:00.000000000Z", close=2),  # 09:30 EST in
        _bar("2024-01-16T20:59:00.000000000Z", close=3),  # 15:59 EST in
        _bar("2024-01-16T21:00:00.000000000Z", close=4),  # 16:00 EST out
        _bar("2024-01-16T13:30:00.000000000Z", close=5),  # 08:30 EST out
        _bar("2024-07-16T13:29:00.000000000Z", close=6),  # 09:29 EDT out
        _bar("2024-07-16T13:30:00.000000000Z", close=7),  # 09:30 EDT in
        _bar("2024-07-16T14:30:00.000000000Z", close=8),  # 10:30 EDT in
        _bar("2024-07-16T19:59:00.000000000Z", close=9),  # 15:59 EDT in
        _bar("2024-07-16T20:00:00.000000000Z", close=10),  # 16:00 EDT out
    ]
    _write_csv(path, rows)

    kept = load_ohlcv(path, rth_only=True)
    assert list(kept["close"]) == [2.0, 3.0, 7.0, 8.0, 9.0]
    minutes = kept.index.hour * 60 + kept.index.minute
    assert minutes.min() >= 9 * 60 + 30
    assert minutes.max() < 16 * 60

    full = load_ohlcv(path, rth_only=False)
    assert len(full) == len(rows)

    fixture = load_ohlcv(sample_fixture_path(), rth_only=True)
    assert pd.Timestamp("2024-01-16 09:29", tz="America/New_York") not in fixture.index
    assert pd.Timestamp("2024-01-16 09:30", tz="America/New_York") in fixture.index
    assert pd.Timestamp("2024-01-16 15:59", tz="America/New_York") in fixture.index
    assert pd.Timestamp("2024-01-16 16:00", tz="America/New_York") not in fixture.index
    assert pd.Timestamp("2024-01-16 16:15", tz="America/New_York") not in fixture.index
    by_day = fixture.groupby(fixture.index.date).size()
    assert list(by_day) == [390, 390, 390]


def test_symbol_filter(tmp_path: Path) -> None:
    path = tmp_path / "symbols.csv"
    stamp = "2024-01-16T14:30:00.000000000Z"
    _write_csv(
        path,
        [
            _bar(stamp, symbol="MES", close=10),
            _bar(stamp, symbol="ES", close=20),
            _bar("2024-01-16T21:30:00.000000000Z", symbol="MES", close=30),
            _bar("2024-07-16T13:30:00.000000000Z", symbol=" MES ", close=40),
        ],
    )

    both = load_ohlcv(path)
    assert set(both["symbol"]) == {"MES", "ES"}
    assert len(both) == 4

    mes = load_ohlcv(path, symbol="MES", rth_only=True)
    assert list(mes["symbol"].unique()) == ["MES"]
    assert list(mes["close"]) == [10.0, 40.0]
    assert mes.index.is_unique

    es = load_ohlcv(path, symbol="ES")
    assert len(es) == 1
    assert es.iloc[0]["close"] == 20.0

    empty = load_ohlcv(path, symbol="NQ")
    assert empty.empty
    assert str(empty.index.tz) == "America/New_York"
    assert empty["close"].dtype == "float64"
    assert empty["volume"].dtype == "int64"


def test_missing_column_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("ts_event,open,high,low,close,volume\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symbol"):
        load_ohlcv(path)


def test_fixture_covers_long_short_and_no_breakout_days() -> None:
    """Shape only: the synthetic sessions can host a later opening-range check."""
    frame = load_ohlcv(sample_fixture_path(), symbol="MES", rth_only=True)
    assert [str(day) for day in sorted(set(frame.index.date))] == list(SCENARIOS)

    for day, spec in SCENARIOS.items():
        session = frame.loc[day]
        assert session.index[0].time() == time(9, 30)
        for minutes in (5, 15, 30):
            window_end = session.index[0] + pd.Timedelta(minutes=minutes)
            window = session[session.index < window_end]
            after = session[session.index >= window_end]
            assert window["low"].min() == spec["or_low"]
            assert window["high"].max() == spec["or_high"]
            broke_up = bool((after["high"] > spec["or_high"]).any())
            broke_down = bool((after["low"] < spec["or_low"]).any())
            if spec["side"] == "long":
                assert broke_up and not broke_down
            elif spec["side"] == "short":
                assert broke_down and not broke_up
            else:
                assert not broke_up and not broke_down

        if spec["side"] == "none":
            continue
        minute_of_day = session.index.hour * 60 + session.index.minute
        after_ten = session[minute_of_day >= 10 * 60]
        before_ten = session[minute_of_day < 10 * 60]
        assert before_ten["high"].max() == spec["or_high"]
        assert before_ten["low"].min() == spec["or_low"]
        if spec["side"] == "long":
            assert after_ten["high"].max() > spec["or_high"]
            assert after_ten["low"].min() > spec["or_low"]
        else:
            assert after_ten["low"].min() < spec["or_low"]
            assert after_ten["high"].max() < spec["or_high"]
