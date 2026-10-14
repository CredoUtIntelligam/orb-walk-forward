"""Unit tests for the Databento-style OHLCV loader."""

from __future__ import annotations

import csv
import inspect
from datetime import time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from orb import load_ohlcv, rth_gap_report, sample_fixture_path, signal_bars

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


def _utc(ny_stamp: str) -> str:
    """Format a New York wall time as explicit UTC ISO-8601 text."""
    stamp = pd.Timestamp(ny_stamp, tz="America/New_York").tz_convert("UTC")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def _on(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    start = pd.Timestamp(day, tz="America/New_York")
    return frame.loc[(frame.index >= start) & (frame.index < start + pd.Timedelta(days=1))]


def _clocks(frame: pd.DataFrame) -> list[tuple[int, int]]:
    return [(stamp.hour, stamp.minute) for stamp in frame.index]


def _bar(
    ts_event: str,
    *,
    symbol: str = "MES",
    open_: float = 100.0,
    high: float = 1000.0,
    low: float = 1.0,
    close: float = 100.0,
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
    readme = " ".join((path.parent / "README.md").read_text(encoding="utf-8").split())
    assert "SYNTHETIC TEST DATA" in readme
    assert "not a Databento download" in readme

    frame = load_ohlcv(path, allow_synthetic=True)
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
    assert not bool(frame["early_close"].any())
    assert int(frame["rth_gap_before"].max()) == 0
    assert rth_gap_report(frame).empty


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

    fixture = load_ohlcv(sample_fixture_path(), rth_only=True, allow_synthetic=True)
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
    frame = load_ohlcv(
        sample_fixture_path(),
        symbol="MES",
        rth_only=True,
        allow_synthetic=True,
    )
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


def test_synthetic_fixture_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allow_synthetic"):
        load_ohlcv(sample_fixture_path())

    loaded = load_ohlcv(sample_fixture_path(), allow_synthetic=True)
    assert not loaded.empty

    copied = tmp_path / sample_fixture_path().name
    copied.write_bytes(sample_fixture_path().read_bytes())
    with pytest.raises(ValueError, match="allow_synthetic"):
        load_ohlcv(copied)
    assert not load_ohlcv(copied, allow_synthetic=True).empty


def test_synthetic_content_guard_blocks_copy_rename_and_parquet(tmp_path: Path) -> None:
    """Filename and container changes do not bypass the synthetic checksum."""
    source = sample_fixture_path()
    copied = tmp_path / "es_ohlcv.csv"
    copied.write_bytes(source.read_bytes())
    renamed = tmp_path / source.name.lower()
    renamed.write_bytes(source.read_bytes())
    table = pd.read_csv(source)
    table["ts_event"] = pd.to_datetime(table["ts_event"], utc=True, format="ISO8601")
    parquet = tmp_path / "mes_ohlcv.parquet"
    table.to_parquet(parquet, index=False)

    for path in (copied, renamed, parquet):
        with pytest.raises(ValueError, match="allow_synthetic"):
            load_ohlcv(path)
        loaded = load_ohlcv(path, allow_synthetic=True)
        assert len(loaded) == 1179
        assert set(loaded["symbol"].unique()) == {"MES"}


def test_duplicate_symbol_timestamp_raises_unless_dropped(tmp_path: Path) -> None:
    path = tmp_path / "dups.csv"
    stamp = "2024-01-16T14:30:00.000000000Z"
    later = "2024-01-16T14:31:00.000000000Z"
    _write_csv(
        path,
        [
            _bar(stamp, symbol="MES", close=1),
            _bar(stamp, symbol="ES", close=20),
            _bar(stamp, symbol="MES", close=2),
            _bar(later, symbol="MES", close=3),
        ],
    )

    with pytest.raises(ValueError, match="Duplicate bars"):
        load_ohlcv(path)
    with pytest.raises(ValueError, match="Duplicate bars"):
        load_ohlcv(path, symbol="MES")

    other = load_ohlcv(path, symbol="ES")
    assert list(other["close"]) == [20.0]

    kept = load_ohlcv(path, drop_duplicates=True)
    mes = kept[kept["symbol"] == "MES"]
    assert list(mes["close"]) == [1.0, 3.0]
    assert mes.index.is_unique


def test_rth_gaps_are_reported_per_session_and_not_filled(tmp_path: Path) -> None:
    path = tmp_path / "gaps.csv"
    rows = [
        _bar(_utc("2024-01-16 09:30"), symbol="MES", close=1),
        _bar(_utc("2024-01-16 09:31"), symbol="MES", close=2),
        _bar(_utc("2024-01-16 09:33"), symbol="MES", close=3),
        _bar(_utc("2024-01-16 09:30"), symbol="ES", close=4),
        _bar(_utc("2024-01-16 09:31"), symbol="ES", close=5),
        _bar(_utc("2024-01-16 09:32"), symbol="ES", close=6),
        _bar(_utc("2024-01-17 09:30"), symbol="MES", close=7),
    ]
    _write_csv(path, rows)

    frame = load_ohlcv(path, rth_only=True)
    assert len(frame) == len(rows)
    hole = pd.Timestamp("2024-01-16 09:32", tz="America/New_York")
    mes = frame[frame["symbol"] == "MES"]
    assert hole not in mes.index
    mes_jan16 = _on(mes, "2024-01-16")
    assert _clocks(mes_jan16) == [(9, 30), (9, 31), (9, 33)]
    assert list(mes_jan16["rth_gap_before"]) == [0, 0, 1]

    gaps = rth_gap_report(frame)
    mes_hole = gaps[
        (gaps["symbol"] == "MES")
        & (gaps["session"] == "2024-01-16")
        & (gaps["start"] == hole)
    ]
    assert len(mes_hole) == 1
    assert int(mes_hole["missing_minutes"].iloc[0]) == 1
    assert mes_hole["end"].iloc[0] == hole + pd.Timedelta(minutes=1)

    es_hole = gaps[(gaps["symbol"] == "ES") & (gaps["start"] == hole)]
    assert es_hole.empty

    jan17 = gaps[(gaps["symbol"] == "MES") & (gaps["session"] == "2024-01-17")]
    assert jan17["start"].iloc[0] == pd.Timestamp("2024-01-17 09:31", tz="America/New_York")
    assert jan17["end"].iloc[0] == pd.Timestamp("2024-01-17 16:00", tz="America/New_York")
    assert "2024-01-16" not in set(jan17["session"])


def test_rejects_high_below_low(tmp_path: Path) -> None:
    path = tmp_path / "high.csv"
    _write_csv(path, [_bar("2024-01-16T14:30:00.000000000Z", high=4990.0, low=5000.0)])
    with pytest.raises(ValueError, match="high is below low"):
        load_ohlcv(path)


@pytest.mark.parametrize(
    ("open_", "close"),
    [(5002.0, 5000.5), (4998.0, 5000.5)],
)
def test_rejects_open_outside_low_high(tmp_path: Path, open_: float, close: float) -> None:
    path = tmp_path / "open.csv"
    _write_csv(
        path,
        [
            _bar(
                "2024-01-16T14:30:00.000000000Z",
                open_=open_,
                high=5001.0,
                low=4999.0,
                close=close,
            )
        ],
    )
    with pytest.raises(ValueError, match=r"open is outside \[low, high\]"):
        load_ohlcv(path)


@pytest.mark.parametrize(
    ("open_", "close"),
    [(5000.0, 5002.0), (5000.0, 4998.0)],
)
def test_rejects_close_outside_low_high(tmp_path: Path, open_: float, close: float) -> None:
    path = tmp_path / "close.csv"
    _write_csv(
        path,
        [
            _bar(
                "2024-01-16T14:30:00.000000000Z",
                open_=open_,
                high=5001.0,
                low=4999.0,
                close=close,
            )
        ],
    )
    with pytest.raises(ValueError, match=r"close is outside \[low, high\]"):
        load_ohlcv(path)


def test_ohlc_bounds_are_inclusive(tmp_path: Path) -> None:
    path = tmp_path / "bounds.csv"
    _write_csv(
        path,
        [
            _bar(
                "2024-01-16T14:30:00.000000000Z",
                open_=4999.0,
                high=5001.0,
                low=4999.0,
                close=5001.0,
            )
        ],
    )
    frame = load_ohlcv(path)
    assert frame.iloc[0]["open"] == 4999.0
    assert frame.iloc[0]["close"] == 5001.0


def test_rejects_negative_volume(tmp_path: Path) -> None:
    path = tmp_path / "volume.csv"
    _write_csv(path, [_bar("2024-01-16T14:30:00.000000000Z", volume=-1)])
    with pytest.raises(ValueError, match="non-negative"):
        load_ohlcv(path)


def test_rth_uses_cme_calendar_for_holidays_weekends_and_early_halts(
    tmp_path: Path,
) -> None:
    """CME equity sessions, not the NYSE holiday list.

    Christmas 2024 and Saturdays are closed. Good Friday 2024 halts at
    09:15 ET, so the 09:30 bar is outside RTH. July 4 2024 is an early
    close on CME_Equity (the cash equity market is shut that day).
    """
    path = tmp_path / "calendar.csv"
    rows = [
        _bar(_utc("2024-12-25 09:30"), close=1),
        _bar(_utc("2024-12-25 15:59"), close=2),
        _bar(_utc("2024-01-13 09:30"), close=3),
        _bar(_utc("2024-03-29 09:30"), close=4),
        _bar(_utc("2024-07-04 09:30"), close=5),
        _bar(_utc("2024-07-05 09:30"), close=6),
    ]
    _write_csv(path, rows)

    rth = load_ohlcv(path, rth_only=True)
    assert _clocks(rth) == [(9, 30), (9, 30)]
    assert [str(stamp.date()) for stamp in rth.index] == ["2024-07-04", "2024-07-05"]
    assert _on(rth, "2024-12-25").empty
    assert _on(rth, "2024-01-13").empty
    assert _on(rth, "2024-03-29").empty

    raw = load_ohlcv(path)
    good_friday = _on(raw, "2024-03-29")
    assert bool(good_friday["early_close"].all())
    kept = _on(signal_bars(raw), "2024-03-29")
    assert not kept.empty
    assert bool(kept["early_close"].all())
    christmas = _on(raw, "2024-12-25")
    assert not bool(christmas["early_close"].any())


def test_early_close_sessions_are_flagged_and_kept_for_signals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "early.csv"
    rows = [
        _bar(_utc("2024-07-03 09:30"), close=1),
        _bar(_utc("2024-07-03 12:59"), close=2),
        _bar(_utc("2024-07-03 13:00"), close=3),
        _bar(_utc("2024-07-03 15:00"), close=4),
        _bar(_utc("2024-07-04 09:30"), close=5),
        _bar(_utc("2024-07-04 13:00"), close=6),
        _bar(_utc("2024-07-05 09:30"), close=7),
        _bar(_utc("2024-07-05 15:59"), close=8),
        _bar(_utc("2024-07-05 16:00"), close=9),
    ]
    _write_csv(path, rows)

    assert "include_early_close" not in inspect.signature(load_ohlcv).parameters
    assert "exclude_early_close" not in inspect.signature(load_ohlcv).parameters
    assert list(inspect.signature(signal_bars).parameters) == ["bars"]

    rth = load_ohlcv(path, rth_only=True)
    assert _clocks(_on(rth, "2024-07-03")) == [(9, 30), (12, 59)]
    assert bool(_on(rth, "2024-07-03")["early_close"].all())
    assert bool(_on(rth, "2024-07-04")["early_close"].all())
    assert _clocks(_on(rth, "2024-07-04")) == [(9, 30)]
    assert _clocks(_on(rth, "2024-07-05")) == [(9, 30), (15, 59)]
    assert not bool(_on(rth, "2024-07-05")["early_close"].any())

    signals = signal_bars(rth)
    assert _clocks(_on(signals, "2024-07-03")) == [(9, 30), (12, 59)]
    assert bool(_on(signals, "2024-07-03")["early_close"].all())
    assert _clocks(_on(signals, "2024-07-04")) == [(9, 30)]
    assert bool(_on(signals, "2024-07-04")["early_close"].all())
    assert _clocks(_on(signals, "2024-07-05")) == [(9, 30), (15, 59)]
    assert not bool(_on(signals, "2024-07-05")["early_close"].any())

    gaps = rth_gap_report(rth)
    july3 = gaps[gaps["session"] == "2024-07-03"]
    thirteen = pd.Timestamp("2024-07-03 13:00", tz="America/New_York")
    assert (july3["start"] == pd.Timestamp("2024-07-03 09:31", tz="America/New_York")).any()
    assert not bool((july3["start"] >= thirteen).any())
    assert july3["end"].max() <= thirteen


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ohlcv(tmp_path / "missing.csv")


def test_unsupported_file_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "bars.txt"
    path.write_text("ts_event\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file type"):
        load_ohlcv(path)


def test_parquet_roundtrip_and_timestamp_index(tmp_path: Path) -> None:
    rows = [
        _bar("2024-01-16T14:30:00.000000000Z", close=1),
        _bar("2024-07-16T13:30:00.000000000Z", close=2),
    ]
    frame = pd.DataFrame(rows)
    frame["ts_event"] = pd.to_datetime(frame["ts_event"], utc=True, format="ISO8601")
    column_path = tmp_path / "column.parquet"
    frame.to_parquet(column_path, index=False)
    loaded = load_ohlcv(column_path)
    assert loaded.index[0] == pd.Timestamp("2024-01-16 09:30", tz="America/New_York")
    assert loaded.index[1] == pd.Timestamp("2024-07-16 09:30", tz="America/New_York")
    assert list(loaded["close"]) == [1.0, 2.0]
    assert loaded["volume"].dtype == "int64"

    indexed = frame.set_index("ts_event")
    index_path = tmp_path / "index.parquet"
    indexed.to_parquet(index_path)
    from_index = load_ohlcv(index_path)
    assert from_index.index[0] == loaded.index[0]
    assert list(from_index["close"]) == [1.0, 2.0]


def test_parquet_import_error_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bars.parquet"
    path.write_bytes(b"")

    def _boom(*_args, **_kwargs):
        raise ImportError("no engine")

    monkeypatch.setattr("orb.loader.pd.read_parquet", _boom)
    with pytest.raises(ImportError, match="pyarrow"):
        load_ohlcv(path)


@pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
def test_blank_values_raise(tmp_path: Path, column: str) -> None:
    path = tmp_path / "blank.csv"
    row = _bar("2024-01-16T14:30:00.000000000Z")
    row[column] = ""
    _write_csv(path, [row])
    with pytest.raises(ValueError, match="missing values"):
        load_ohlcv(path)


def test_blank_timestamp_raises(tmp_path: Path) -> None:
    path = tmp_path / "blank-ts.csv"
    row = _bar("")
    _write_csv(path, [row])
    with pytest.raises(ValueError, match="missing timestamps"):
        load_ohlcv(path)


def test_fractional_volume_raises(tmp_path: Path) -> None:
    path = tmp_path / "fraction.csv"
    _write_csv(path, [_bar("2024-01-16T14:30:00.000000000Z", volume=1.5)])
    with pytest.raises(ValueError, match="whole number"):
        load_ohlcv(path)


def test_naive_timestamps_raise(tmp_path: Path) -> None:
    path = tmp_path / "naive.csv"
    _write_csv(
        path,
        [
            _bar("2024-01-16T14:30:00"),
            _bar("2024-01-16 14:31:00"),
        ],
    )
    with pytest.raises(ValueError, match="naive"):
        load_ohlcv(path)


def test_non_utc_offset_raises(tmp_path: Path) -> None:
    path = tmp_path / "offset.csv"
    _write_csv(path, [_bar("2024-01-16T09:30:00-05:00")])
    with pytest.raises(ValueError, match="must be UTC"):
        load_ohlcv(path)


def test_mixed_offsets_raise(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    _write_csv(
        path,
        [
            _bar("2024-01-16T14:30:00.000000000Z"),
            _bar("2024-01-16T09:31:00-05:00"),
        ],
    )
    with pytest.raises(ValueError, match="mixes timezone offsets"):
        load_ohlcv(path)


@pytest.mark.parametrize("stamp", ["1705415400000000000.0", "-1", "1e18"])
def test_non_integer_digit_like_timestamps_raise(tmp_path: Path, stamp: str) -> None:
    path = tmp_path / "digits.csv"
    row = _bar(stamp)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=COLUMNS, quoting=csv.QUOTE_NONNUMERIC
        )
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="digit-like"):
        load_ohlcv(path)


def test_integer_nanosecond_text_with_padding_is_utc(tmp_path: Path) -> None:
    winter = pd.Timestamp("2024-01-16T14:30:00Z")
    path = tmp_path / "text-ns.csv"
    _write_csv(path, [_bar(f" {winter.value}")])
    frame = load_ohlcv(path)
    assert frame.index[0] == pd.Timestamp("2024-01-16 09:30", tz="America/New_York")


def test_bad_iso8601_wraps_pandas_error(tmp_path: Path) -> None:
    path = tmp_path / "bad-iso.csv"
    _write_csv(path, [_bar("2024-13-01T00:00:00Z")])
    with pytest.raises(ValueError, match="could not be parsed as UTC timestamps") as raised:
        load_ohlcv(path)
    assert raised.value.__cause__ is not None


def test_float_timestamps_raise(tmp_path: Path) -> None:
    path = tmp_path / "float.parquet"
    frame = pd.DataFrame([_bar("ignored")])
    frame["ts_event"] = pd.Series([1705415400000000000.0], dtype="float64")
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="floats cannot represent nanoseconds"):
        load_ohlcv(path)


def test_naive_or_non_utc_datetime_dtype_raises(tmp_path: Path) -> None:
    naive_path = tmp_path / "naive.parquet"
    naive = pd.DataFrame([_bar("ignored")])
    naive["ts_event"] = pd.to_datetime(["2024-01-16T14:30:00"])
    naive.to_parquet(naive_path, index=False)
    with pytest.raises(ValueError, match="naive"):
        load_ohlcv(naive_path)

    zoned_path = tmp_path / "zoned.parquet"
    zoned = pd.DataFrame([_bar("ignored")])
    zoned["ts_event"] = pd.to_datetime(["2024-01-16T09:30:00"]).tz_localize(
        "America/New_York"
    )
    zoned.to_parquet(zoned_path, index=False)
    with pytest.raises(ValueError, match="must be UTC"):
        load_ohlcv(zoned_path)


def test_non_numeric_price_raises(tmp_path: Path) -> None:
    path = tmp_path / "text.csv"
    row = _bar("2024-01-16T14:30:00.000000000Z", open_=100, high=110, low=90, close=105)
    row["open"] = "abc"
    _write_csv(path, [row])
    with pytest.raises(ValueError, match="open contains non-numeric values") as raised:
        load_ohlcv(path)
    assert raised.value.__cause__ is not None


def test_out_of_range_integer_nanoseconds_raise(tmp_path: Path) -> None:
    path = tmp_path / "overflow.csv"
    _write_csv(path, [_bar("9" * 40)])
    with pytest.raises(ValueError, match="not valid nanosecond timestamps") as raised:
        load_ohlcv(path)
    assert raised.value.__cause__ is not None


def test_integer_nanoseconds_mixed_with_timestamp_text_raise(tmp_path: Path) -> None:
    winter = pd.Timestamp("2024-01-16T14:30:00Z")
    path = tmp_path / "mixed-repr.csv"
    _write_csv(
        path,
        [
            _bar(str(winter.value)),
            _bar("2024-01-16T14:31:00.000000000Z"),
        ],
    )
    with pytest.raises(ValueError, match="mixes integer nanoseconds"):
        load_ohlcv(path)


def test_signal_bars_requires_early_close_column() -> None:
    with pytest.raises(ValueError, match="early_close"):
        signal_bars(pd.DataFrame({"close": [1.0]}))


def test_parquet_ts_event_index_is_reset(tmp_path: Path) -> None:
    frame = pd.DataFrame([_bar("2024-01-16T14:30:00.000000000Z", close=100)])
    frame["ts_event"] = pd.to_datetime(frame["ts_event"], utc=True, format="ISO8601")
    indexed = frame.set_index("ts_event")
    path = tmp_path / "indexed.parquet"
    indexed.to_parquet(path)
    raw = pd.read_parquet(path)
    assert "ts_event" not in raw.columns
    assert raw.index.name == "ts_event"
    loaded = load_ohlcv(path)
    assert loaded.index.name == "ts_event"
    assert loaded.index[0] == pd.Timestamp("2024-01-16 09:30", tz="America/New_York")
    assert "ts_event" not in loaded.columns
    assert float(loaded.iloc[0]["close"]) == 100.0


def test_holiday_short_sessions_are_early_closes_and_keep_gap_reports(
    tmp_path: Path,
) -> None:
    """July 4, Thanksgiving, and MLK trade a shortened futures session.

    Those days are early closes: flagged, kept for signals, and still
    present in the RTH gap report out to the early halt.
    """
    path = tmp_path / "short-holidays.csv"
    days = ("2024-01-15", "2024-07-04", "2024-11-28")
    rows = []
    for day in days:
        rows.append(_bar(_utc(f"{day} 09:30"), close=100))
        rows.append(_bar(_utc(f"{day} 13:00"), close=100))
    _write_csv(path, rows)

    rth = load_ohlcv(path, rth_only=True)
    kept = signal_bars(rth)
    assert len(kept) == len(rth)
    gaps = rth_gap_report(rth)
    for day in days:
        session = _on(rth, day)
        assert _clocks(session) == [(9, 30)]
        assert bool(session["early_close"].all())
        halt = pd.Timestamp(f"{day} 13:00", tz="America/New_York")
        reported = gaps[gaps["session"] == day]
        assert not reported.empty
        assert reported["end"].max() == halt
        assert not bool((reported["start"] >= halt).any())
        kept_day = _on(kept, day)
        assert _clocks(kept_day) == [(9, 30)]
        assert bool(kept_day["early_close"].all())


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad", ["inf", "-inf"])
def test_infinite_prices_raise(tmp_path: Path, column: str, bad: str) -> None:
    path = tmp_path / "inf.csv"
    row = _bar("2024-01-16T14:30:00.000000000Z", open_=100, high=110, low=90, close=105)
    row[column] = bad
    _write_csv(path, [row])
    with pytest.raises(ValueError, match=f"{column} must be finite"):
        load_ohlcv(path)


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("bad", ["0", "-1"])
def test_non_positive_prices_raise(tmp_path: Path, column: str, bad: str) -> None:
    path = tmp_path / "nonpositive.csv"
    row = _bar("2024-01-16T14:30:00.000000000Z", open_=100, high=110, low=90, close=105)
    row[column] = bad
    _write_csv(path, [row])
    with pytest.raises(ValueError, match=f"{column} must be positive"):
        load_ohlcv(path)

