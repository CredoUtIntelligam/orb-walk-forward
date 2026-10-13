"""Opening-range breakout research package.

Sample-data loading, opening-range calculation, and breakout signals.
Feed ``signal_bars(load_ohlcv(...))`` into the range and signal functions.
"""

from orb.breakout import DEFAULT_TRIGGER, Signal, breakout_signals
from orb.loader import load_ohlcv, rth_gap_report, sample_fixture_path, signal_bars
from orb.opening_range import DEFAULT_WINDOW_MINUTES, opening_ranges

__all__ = [
    "DEFAULT_TRIGGER",
    "DEFAULT_WINDOW_MINUTES",
    "Signal",
    "breakout_signals",
    "load_ohlcv",
    "opening_ranges",
    "rth_gap_report",
    "sample_fixture_path",
    "signal_bars",
]
