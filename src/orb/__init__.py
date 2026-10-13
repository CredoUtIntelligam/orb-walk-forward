"""Opening-range breakout research package.

Sample-data loading, opening-range calculation, and breakout signals.
"""

from orb.breakout import DEFAULT_TRIGGER, Signal, breakout_signals
from orb.loader import load_ohlcv, sample_fixture_path
from orb.opening_range import DEFAULT_WINDOW_MINUTES, opening_ranges

__all__ = [
    "DEFAULT_TRIGGER",
    "DEFAULT_WINDOW_MINUTES",
    "Signal",
    "breakout_signals",
    "load_ohlcv",
    "opening_ranges",
    "sample_fixture_path",
]
