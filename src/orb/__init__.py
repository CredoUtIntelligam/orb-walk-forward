"""Opening-range breakout research package.

Sample-data loading, opening-range calculation, and breakout signals.
Feed ``signal_bars(load_ohlcv(...))`` into the range and signal functions.
``python -m orb`` and the ``orb-signals`` console script run that path.

The default scan is a research rule and is not the frozen ES ORB spec.
``frozen_es_signals`` (CLI ``--preset frozen-es``) is that spec's signal.
"""

from orb.breakout import DEFAULT_TRIGGER, Signal, breakout_signals
from orb.loader import load_ohlcv, rth_gap_report, sample_fixture_path, signal_bars
from orb.opening_range import DEFAULT_WINDOW_MINUTES, opening_ranges
from orb.preset import FROZEN_ES_PRESET, frozen_es_signals

__all__ = [
    "DEFAULT_TRIGGER",
    "DEFAULT_WINDOW_MINUTES",
    "FROZEN_ES_PRESET",
    "Signal",
    "breakout_signals",
    "frozen_es_signals",
    "load_ohlcv",
    "opening_ranges",
    "rth_gap_report",
    "sample_fixture_path",
    "signal_bars",
]
