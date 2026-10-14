"""Frozen ES ORB signal preset. Stops, targets, and P&L stay out of scope."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd

from orb import frozen_es_signals, load_ohlcv, opening_ranges, sample_fixture_path, signal_bars
from orb.breakout import TRIGGER_CLOSE_5M
from orb.preset import FROZEN_ES_PRESET, FROZEN_ES_WINDOW_MINUTES

NY = "America/New_York"
OPEN = 9 * 60 + 30


def test_fixture_preset_emits_the_single_long_at_1004() -> None:
    """The synthetic sessions produce one frozen-ES signal.

    2024-01-16's 10:00 bucket closes at 10:04 ET, at 5011, above the
    30-minute box. 2024-01-17 trades below its high, and 2024-01-18
    stays inside. Long only, so the short day is omitted.
    """
    bars = signal_bars(
        load_ohlcv(sample_fixture_path(), symbol="MES", rth_only=True, allow_synthetic=True)
    )
    signals = frozen_es_signals(bars)
    assert FROZEN_ES_PRESET == "frozen-es"
    assert FROZEN_ES_WINDOW_MINUTES == 30
    assert len(signals) == 1
    signal = signals[0]
    assert signal.session_date == date(2024, 1, 16)
    assert signal.direction == "long"
    assert signal.or_high == 5008.0
    assert signal.or_low == 5000.0
    assert signal.entry_price == 5011.0
    assert signal.entry_ts == pd.Timestamp("2024-01-16 10:04", tz=NY)
    assert signal.window_minutes == 30
    assert signal.trigger == TRIGGER_CLOSE_5M
    payload = signal.to_dict()
    assert json.loads(json.dumps(payload)) == {
        "session_date": "2024-01-16",
        "direction": "long",
        "or_high": 5008.0,
        "or_low": 5000.0,
        "entry_ts": "2024-01-16T10:04:00-05:00",
        "entry_price": 5011.0,
        "window_minutes": 30,
        "trigger": "close_5m",
    }
    assert date(2024, 1, 17) not in [item.session_date for item in signals]
    assert date(2024, 1, 18) not in [item.session_date for item in signals]


def test_gaps_keep_a_partial_box_and_skip_only_a_missing_open_or_empty_box() -> None:
    """A hole after 09:30 still forms the box. No 09:30, or no box, does not.

    2024-01-16 has no 09:30 print. 2024-01-17 has no bar in the box.
    2024-01-18 has 09:30 and 09:40 and is missing the rest of the half hour.
    Only 2024-01-18 signals, from the bars that printed. The complete-session
    research range drops that same day.
    """
    bars = _frame(
        [
            ("2024-01-16", OPEN + 1, 14.0, 7.0, 11.0),
            ("2024-01-16", 10 * 60 + 4, 16.0, 12.0, 15.0),
            ("2024-01-17", 11 * 60, 16.0, 12.0, 15.0),
            ("2024-01-18", OPEN, 10.0, 8.0, 9.0),
            ("2024-01-18", OPEN + 10, 14.0, 7.0, 11.0),
            ("2024-01-18", 10 * 60 + 4, 16.0, 12.0, 15.0),
        ]
    )
    signals = frozen_es_signals(bars)
    assert [signal.session_date for signal in signals] == [date(2024, 1, 18)]
    signal = signals[0]
    assert signal.or_high == 14.0
    assert signal.or_low == 7.0
    assert signal.entry_price == 15.0
    assert signal.entry_ts == pd.Timestamp("2024-01-18 10:04", tz=NY)
    assert opening_ranges(bars, window_minutes=30).empty


def test_bucket_close_minute_is_the_signal_and_1355_is_not_eligible() -> None:
    """The 10:00 bucket stamps 10:04. A missing close minute waits.

    A 10:00 close above the box does not fire when 10:04 is back inside
    or missing. 13:54 can still fire. 13:59 belongs to the 13:55 bucket
    and does not.
    """
    inside = _frame(
        [
            ("2024-01-16", OPEN, 10.0, 6.0, 8.0),
            ("2024-01-16", 10 * 60, 13.0, 9.0, 12.0),
            ("2024-01-16", 10 * 60 + 4, 9.5, 8.0, 9.0),
        ]
    )
    assert frozen_es_signals(inside) == []

    skipped_close = _frame(
        [
            ("2024-01-16", OPEN, 10.0, 6.0, 8.0),
            ("2024-01-16", 10 * 60, 13.0, 9.0, 12.0),
            ("2024-01-16", 10 * 60 + 9, 14.0, 11.0, 13.0),
        ]
    )
    waited = frozen_es_signals(skipped_close)
    assert len(waited) == 1
    assert waited[0].entry_ts == pd.Timestamp("2024-01-16 10:09", tz=NY)
    assert waited[0].entry_price == 13.0

    last_bucket = _frame(
        [
            ("2024-07-16", OPEN, 10.0, 6.0, 8.0),
            ("2024-07-16", 13 * 60 + 54, 12.0, 11.0, 11.5),
        ]
    )
    fired = frozen_es_signals(last_bucket)
    assert len(fired) == 1
    assert fired[0].entry_ts == pd.Timestamp("2024-07-16 13:54", tz=NY)
    assert fired[0].entry_ts.utcoffset() == timedelta(hours=-4)
    assert fired[0].direction == "long"

    too_late = _frame(
        [
            ("2024-01-16", OPEN, 10.0, 6.0, 8.0),
            ("2024-01-16", 13 * 60 + 59, 12.0, 11.0, 11.5),
        ]
    )
    assert frozen_es_signals(too_late) == []


def test_close_below_the_low_does_not_signal() -> None:
    """The preset is long only. A break of the box low is not a signal."""
    bars = _frame(
        [
            ("2024-01-17", OPEN, 10.0, 6.0, 8.0),
            ("2024-01-17", 10 * 60 + 4, 7.0, 4.0, 5.0),
        ]
    )
    assert frozen_es_signals(bars) == []


def test_early_close_sessions_stay_excluded() -> None:
    """A flagged early-close day does not signal under the preset either."""
    early = _frame(
        [
            ("2024-07-03", OPEN, 10.0, 6.0, 8.0),
            ("2024-07-03", 10 * 60 + 4, 12.0, 11.0, 11.5),
        ],
        early_close=True,
    )
    regular = _frame(
        [
            ("2024-07-05", OPEN, 10.0, 6.0, 8.0),
            ("2024-07-05", 10 * 60 + 4, 12.0, 11.0, 11.5),
        ],
        early_close=False,
    )
    signals = frozen_es_signals(pd.concat([early, regular]))
    assert [signal.session_date for signal in signals] == [date(2024, 7, 5)]


def _frame(
    rows: list[tuple[str, int, float, float, float]],
    *,
    early_close: bool | None = None,
    symbol: str = "ES",
) -> pd.DataFrame:
    index = [
        pd.Timestamp(day, tz=NY) + pd.Timedelta(minutes=minute)
        for day, minute, _high, _low, _close in rows
    ]
    frame = pd.DataFrame(
        {
            "open": [8.0] * len(rows),
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
            "volume": [1] * len(rows),
            "symbol": [symbol] * len(rows),
        },
        index=pd.DatetimeIndex(index, name="ts_event"),
    )
    if early_close is not None:
        frame["early_close"] = early_close
    return frame
