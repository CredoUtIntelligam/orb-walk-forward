# src/

Installable package: `orb` in `src/orb/` (src layout).

`src/__init__.py` is only a marker from the original skeleton. It is not the
package setuptools installs. Import with
`from orb import load_ohlcv, signal_bars, opening_ranges, breakout_signals, frozen_es_signals`.

Current modules:

- `orb/loader.py` — read a local Databento-style OHLCV-1m CSV or Parquet
  file into a New York-indexed DataFrame (CME equity RTH, gap report,
  `early_close`, and `signal_bars`)
- `orb/opening_range.py` — opening-range high/low for a configurable RTH
  window, skipping sessions that do not contain every window minute
- `orb/breakout.py` — first post-window breakout of each session.
  The default trigger is a close strictly beyond the range; `high_low`
  uses a high or low that trades strictly through the level.
  Rows flagged `early_close` are kept when that column is present.
  The flag is metadata. Entries stop at the session close
- `orb/preset.py` — `frozen_es_signals`, the frozen ES ORB signal
  (30-minute box, 5-minute close, long only). Not the research default.
  The last bucket is the earlier of 13:50-13:55 and the session close
- `orb/cli.py` and `orb/__main__.py` — `orb-signals` and `python -m orb`.
  The default mode is the research scan and is not the frozen spec.
  `--preset frozen-es` selects the spec signal. `--symbol` filters one
  contract. `--trigger` is `close` (the default, close-only) or
  `high_low`. The synthetic fixture needs `--allow-synthetic`.
  Early-close sessions stay included
- `orb/fixtures/` — synthetic test CSV (not real market data). Loading it
  requires `allow_synthetic=True`

Walk-forward code is not in this package yet.
