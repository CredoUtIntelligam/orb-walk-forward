# Methodology notes

## ORB basics
- Define opening range (e.g. first 5/15/30 minutes of RTH).
- Signal on break of OR high/low (with optional confirmation: close beyond, volume, etc.).
- `breakout_signals` is that check. The default is the first regular-hours close strictly beyond the opening-range high or low after the window closes. `trigger="high_low"` uses the first high or low that trades strictly through the level. At most one signal per session. It does not size, fill, or score the trade.
- Early-close sessions, including holiday short sessions, are flagged `early_close` by the loader and are excluded from signals. Pass `signal_bars(load_ohlcv(...))` into `opening_ranges` and `breakout_signals`. `breakout_signals` also drops `early_close=True` rows when that column is present. There is no flag to include them.
- Stops / targets relative to range width or structure.

## Walk-forward
- Rolling or expanding train/test windows.
- Optimize parameters in-sample, evaluate out-of-sample.
- Track efficiency, stability, and degradation over time.

Align with Vega's existing MES/ES ORB research and frozen specs where applicable.
