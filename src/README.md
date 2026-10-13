# src/

Installable package: `orb` in `src/orb/` (src layout).

`src/__init__.py` is only a marker from the original skeleton. It is not the
package setuptools installs. Import with `from orb import load_ohlcv, opening_ranges`.

Current modules:

- `orb/loader.py` — read a local Databento-style OHLCV-1m CSV into a
  New York-indexed DataFrame
- `orb/opening_range.py` — opening-range high/low for a configurable RTH
  window, skipping sessions that do not contain every window minute
- `orb/fixtures/` — synthetic test CSV (not real market data)

Breakout signals and walk-forward code are not in this package yet.
