"""Derived signal clocks. They do not change which bar fires or Signal equality."""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

import pandas as pd
import pytest

from orb import (
    Signal,
    breakout_signals,
    frozen_es_signals,
    load_ohlcv,
    opening_ranges,
    sample_fixture_path,
    signal_bars,
)
from orb.breakout import LONG, TRIGGER_CLOSE, TRIGGER_HIGH_LOW
from orb.cli import main

NY = "America/New_York"


def _decision(**overrides: object) -> Signal:
    fields: dict[str, object] = {
        "session_date": date(2024, 1, 16),
        "direction": LONG,
        "or_high": 10.0,
        "or_low": 6.0,
        "entry_ts": pd.Timestamp("2024-01-16 10:04", tz=NY),
        "entry_price": 11.0,
        "window_minutes": 30,
        "trigger": "close_5m",
        "early_close": False,
        "session_close": time(16, 0),
    }
    fields.update(overrides)
    return Signal(**fields)  # type: ignore[arg-type]


def test_close_5m_confirmation_is_the_bucket_end() -> None:
    """The 10:04 bar is known at the 10:05 bucket end, when the next bar opens."""
    signal = _decision()
    assert signal.entry_ts == pd.Timestamp("2024-01-16 10:04", tz=NY)
    assert signal.bar_timestamp == signal.entry_ts
    assert signal.confirmation_available_at == pd.Timestamp("2024-01-16 10:05", tz=NY)
    assert signal.earliest_actionable_at == pd.Timestamp("2024-01-16 10:05", tz=NY)

    bars = signal_bars(
        load_ohlcv(sample_fixture_path(), symbol="MES", allow_synthetic=True)
    )
    frozen = frozen_es_signals(bars)
    assert len(frozen) == 1
    assert frozen[0].entry_price == 5011.0
    assert frozen[0].bar_timestamp == frozen[0].entry_ts
    assert frozen[0].confirmation_available_at == pd.Timestamp("2024-01-16 10:05", tz=NY)
    assert frozen[0].earliest_actionable_at == frozen[0].confirmation_available_at
    payload = frozen[0].to_dict()
    assert json.loads(json.dumps(payload))["confirmation_available_at"] == (
        "2024-01-16T10:05:00-05:00"
    )
    assert payload["bar_timestamp"] == "2024-01-16T10:04:00-05:00"
    assert payload["earliest_actionable_at"] == "2024-01-16T10:05:00-05:00"


def test_research_close_uses_the_confirming_bar_and_the_next_open() -> None:
    """A 1-minute close is confirmed when that bar ends, one minute later."""
    bars = signal_bars(
        load_ohlcv(sample_fixture_path(), symbol="MES", allow_synthetic=True)
    )
    signals = breakout_signals(bars, opening_ranges(bars))
    assert [signal.entry_ts for signal in signals] == [
        pd.Timestamp("2024-01-16 10:00", tz=NY),
        pd.Timestamp("2024-01-17 10:00", tz=NY),
    ]
    assert [signal.entry_price for signal in signals] == [5011.0, 5047.0]
    first = signals[0]
    assert first.bar_timestamp == first.entry_ts
    assert first.confirmation_available_at == pd.Timestamp("2024-01-16 10:01", tz=NY)
    assert first.earliest_actionable_at == first.confirmation_available_at
    assert first.to_dict()["bar_timestamp"] == "2024-01-16T10:00:00-05:00"
    assert first.to_dict()["confirmation_available_at"] == "2024-01-16T10:01:00-05:00"


def test_timing_fields_do_not_change_signal_equality() -> None:
    """Supplied clocks are replaced by the derivation, so equality is unchanged."""
    plain = _decision()
    supplied = _decision(
        bar_timestamp=pd.Timestamp("1999-01-01", tz=NY),
        confirmation_available_at=pd.Timestamp("1999-01-01 00:05", tz=NY),
        earliest_actionable_at=pd.Timestamp("1999-01-01 00:06", tz=NY),
    )
    assert plain == supplied
    assert supplied.bar_timestamp == plain.entry_ts
    assert supplied.confirmation_available_at == plain.confirmation_available_at
    assert supplied.earliest_actionable_at == plain.earliest_actionable_at
    assert plain.direction == LONG
    assert plain.trigger == "close_5m"


def test_high_low_uses_the_confirming_bar_open() -> None:
    """A wick signal still stamps the bar that traded through the level."""
    index = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-16 09:45", tz=NY)], name="ts_event"
    )
    bars = pd.DataFrame(
        {"open": [8.0], "high": [12.0], "low": [7.0], "close": [9.0], "volume": [1]},
        index=index,
    )
    ranges = pd.DataFrame(
        {
            "session_date": pd.Series([date(2024, 1, 16)], dtype="object"),
            "range_high": pd.array([10.0], dtype="float64"),
            "range_low": pd.array([6.0], dtype="float64"),
            "window_minutes": pd.array([15], dtype="int64"),
        }
    )
    pierced = breakout_signals(bars, ranges, trigger=TRIGGER_HIGH_LOW)
    closed = breakout_signals(bars, ranges, trigger=TRIGGER_CLOSE)
    assert len(pierced) == 1
    assert closed == []
    signal = pierced[0]
    assert signal.entry_price == 10.0
    assert signal.entry_ts == pd.Timestamp("2024-01-16 09:45", tz=NY)
    assert signal.bar_timestamp == signal.entry_ts
    assert signal.confirmation_available_at == pd.Timestamp("2024-01-16 09:46", tz=NY)
    assert signal.earliest_actionable_at == signal.confirmation_available_at
    assert signal == Signal(
        session_date=date(2024, 1, 16),
        direction=LONG,
        or_high=10.0,
        or_low=6.0,
        entry_ts=signal.entry_ts,
        entry_price=10.0,
        window_minutes=15,
        trigger=TRIGGER_HIGH_LOW,
        early_close=False,
        session_close=time(16, 0),
    )


def test_cli_carries_timing_fields(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """JSON and CSV include the three clocks after the existing columns."""
    assert main(["--allow-synthetic", "--preset", "frozen-es", "--format", "json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["entry_ts"] == "2024-01-16T10:04:00-05:00"
    assert row["bar_timestamp"] == "2024-01-16T10:04:00-05:00"
    assert row["confirmation_available_at"] == "2024-01-16T10:05:00-05:00"
    assert row["earliest_actionable_at"] == "2024-01-16T10:05:00-05:00"

    out = tmp_path / "signals.csv"
    assert main(["--allow-synthetic", "--out", str(out)]) == 0
    capsys.readouterr()
    header = out.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header[-3:] == [
        "bar_timestamp",
        "confirmation_available_at",
        "earliest_actionable_at",
    ]
