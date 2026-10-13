# src/

Installable package: `orb` in `src/orb/` (src layout).

`src/__init__.py` is only a marker from the original skeleton. It is not the
package setuptools installs. Import with `from orb import load_ohlcv, opening_ranges, breakout_signals`.

Current modules:

- `orb/loader.py` — read a local Databento-style OHLCV-1m CSV into a
  New York-indexed DataFrame
- `orb/opening_range.py` — opening-range high/low for a configurable RTH
  window, skipping sessions that do not contain every window minute
- `orb/breakout.py` — first post-window breakout of each session.
  The default trigger is a close strictly beyond the range; `high_low`
  uses a high or low that trades strictly through the level
- `orb/fixtures/` — synthetic test CSV (not real market data)

Walk-forward code is not in this package yet.
