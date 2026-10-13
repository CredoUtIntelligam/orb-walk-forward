# src/

Installable package: `orb` in `src/orb/` (src layout).

`src/__init__.py` is only a marker from the original skeleton. It is not the
package setuptools installs. Import the loader with `from orb import load_ohlcv`.

Current modules:

- `orb/loader.py` — read a local Databento-style OHLCV-1m CSV into a
  New York-indexed DataFrame
- `orb/fixtures/` — synthetic test CSV (not real market data)

Opening-range calculation, breakout signals, and walk-forward code are not in
this package yet.
