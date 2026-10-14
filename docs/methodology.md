# Methodology notes

## ORB basics
- Define opening range (e.g. first 5/15/30 minutes of RTH).
- Signal on break of OR high/low (with optional confirmation: close beyond, volume, etc.).
- `breakout_signals` is the research check, and it is not the frozen ES ORB spec. The default trigger is close-only: the first regular-hours close strictly beyond the opening-range high or low after the window closes. `trigger="high_low"` uses the first high or low that trades strictly through the level. At most one signal per session. It does not size, fill, or score the trade.
- `frozen_es_signals` / `--preset frozen-es` is the spec signal: 30-minute box `[09:30, 10:00)`, partial when a later minute is missing, first clock-aligned 5-minute close strictly above the box high, long only, last bucket 13:50-13:55, `entry_ts` at the bucket close minute. Stop, target, and flatten from that spec are not in this package.
- `python -m orb` / `orb-signals` prints those signals. The default mode is the research scan. It does not size, exit, or score them either.
- Early-close sessions, including holiday short sessions, are flagged `early_close` by the loader and are excluded from signals. Pass `signal_bars(load_ohlcv(...))` into `opening_ranges` and `breakout_signals`. `breakout_signals` also drops `early_close=True` rows when that column is present. There is no flag to include them.
- Stops / targets relative to range width or structure.

## Walk-forward
- Rolling or expanding train/test windows.
- Optimize parameters in-sample, evaluate out-of-sample.
- Track efficiency, stability, and degradation over time.

Align with Vega's existing MES/ES ORB research and frozen specs where applicable.
