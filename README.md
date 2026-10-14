# ORB Walk-Forward

Private research repository for Opening Range Breakout (ORB) strategy research and walk-forward analysis, primarily on MES/ES futures.

This repo is designed to be driven by looping Grok Bots (orchestrator + specialized engineer) with Cursor handoffs.

## Goal
Build a robust, reproducible walk-forward framework for ORB variants (range length, entry confirmation, filters, targets/stops, etc.) using Databento data where available. Results should support the existing Vega bot's ORB research.

The code in this repo is landing one piece at a time. Piece 1 is the hardened sample-data loader. Piece 2 is the opening-range calculation. Piece 3 is the breakout signal. Piece 4 is a small CLI (`python -m orb` and `orb-signals`). The default CLI mode is a research scan and is not the frozen ES ORB spec. `--preset frozen-es` is that spec's signal rule. Early-close sessions never produce a signal. A walk-forward optimizer, position sizing, execution, and P&L stay out of scope.

## Package layout

The installable package is `orb`, in `src/orb/` (src layout). `src/__init__.py` is a leftover skeleton marker and is not imported.

```
src/orb/__init__.py
src/orb/__main__.py
src/orb/cli.py
src/orb/loader.py
src/orb/opening_range.py
src/orb/breakout.py
src/orb/preset.py
src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv
src/orb/fixtures/README.md
tests/test_loader.py
tests/test_opening_range.py
tests/test_breakout.py
tests/test_cli.py
tests/test_preset.py
pyproject.toml
```

Python 3.10+. Runtime dependencies are pandas, numpy, pyarrow, tzdata, and pinned `pandas-market-calendars==5.5.0` with `exchange-calendars==4.13.2`. pytest is a dev extra, not a runtime dependency. The test suite does not need the Databento SDK or an API key.

## Sample-data loader

`load_ohlcv` reads a local Databento OHLCV-1m CSV or Parquet file. It does not download anything.

`ts_event` must be UTC: ISO-8601 text with an explicit `Z` or `+00:00` offset, or integer nanoseconds. Naive timestamps, non-UTC offsets, mixed offsets, and non-integer digit-like strings are rejected. The frame is indexed by timezone-aware `America/New_York` timestamps, so daylight saving time follows the IANA timezone. OHLC columns are floats and `volume` is int. Bars with a non-finite or non-positive price, `high < low`, an open or close outside `[low, high]`, or a negative volume are rejected. A repeated symbol and timestamp raises unless `drop_duplicates=True`.

Pass `symbol` to keep one instrument. Pass `rth_only=True` to keep CME equity regular trading hours: `[09:30, 16:00)` ET on a full session, using the `CME_Equity` calendar for weekends, full holidays, and early closes. 16:00 is exclusive because these bars are stamped at the start of the minute; the last full-session bar starts at 15:59. An earlier exchange halt shortens that window. Missing minutes inside a session are reported (`rth_gap_before` and `rth_gap_report`) and are never filled.

Holiday short sessions, when futures still trade a shortened day (July 4, Thanksgiving, MLK Day, and other CME equity halts before 16:00 ET), are early closes. They are flagged on `early_close`, stay in the loaded frame, and stay eligible for the gap report. `signal_bars` always removes them. That exclusion is fixed; there is no switch to include early-close sessions in signals.

```python
from orb import (
    breakout_signals,
    load_ohlcv,
    opening_ranges,
    sample_fixture_path,
    signal_bars,
)

bars = load_ohlcv(
    sample_fixture_path(),
    symbol="MES",
    rth_only=True,
    allow_synthetic=True,
)
eligible = signal_bars(bars)
ranges = opening_ranges(eligible, window_minutes=15)
signals = breakout_signals(eligible, ranges)
```

`src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv` is synthetic test data: three fabricated MES regular-hours sessions shaped so an opening-range check can see a long breakout, a short breakout, and a day with no breakout. It is not real market data. Do not compute performance from it. `load_ohlcv` recognizes that content by a checksum of the normalized rows, not by the file name, and raises unless `allow_synthetic=True`. A renamed copy or a Parquet conversion of the same bars is refused the same way. See `src/orb/fixtures/README.md`.

## Opening range

`opening_ranges` reads that New York 1-minute frame and returns one row per complete session: `session_date`, `range_high`, `range_low`, `window_minutes`, and `bar_count`.

The window starts at 09:30 ET. `window_minutes` defaults to 15 and can be 5, 30, or any other positive length that ends at or before 16:00 ET. Stamps are the start of the minute, so a 15-minute range uses the bars from 09:30 through 09:44 inclusive (`[09:30, 09:45)`). `range_high` is the max high in that window and `range_low` is the min low. Other zones are converted to America/New_York first. Naive timestamps are rejected.

A session is kept only when every minute in the window is present exactly once. A missing minute, a duplicate stamp, or a sub-minute timestamp inside the window drops that day. Bars outside the window, including 09:29 and the 16:00 bar, are ignored. Pass one symbol; a frame that still contains more than one symbol is rejected so two instruments are not blended into one range.

Build ranges from `signal_bars(load_ohlcv(...))` when they will feed breakout signals. `opening_ranges` itself does not drop `early_close` rows, so an unfiltered frame can still produce a range for a holiday short session. Those ranges should not be paired with the unfiltered bars if the goal is a signal.

## Breakout signals

`breakout_signals` takes those bars and that range table and returns a `list[Signal]`. Each `Signal` has `session_date`, `direction` (`"long"` or `"short"`), `or_high`, `or_low`, `entry_ts` (start of the triggering minute, America/New_York), `entry_price`, `window_minutes`, and `trigger` (`"close"` or `"high_low"`). `to_dict()` returns those fields with dates and timestamps as ISO-8601 strings. The record is frozen, and `__post_init__` checks the direction and the field types. At most one signal is returned per session date. This research function is not the frozen ES ORB spec.

A session emits at most one signal: the first eligible bar that breaks the range. Later bars that day are ignored, including a break of the other side. A session with no break is omitted. Eligible bars are minute-aligned stamps in `[09:30 + window_minutes, 16:00)` ET. Bars inside the opening-range window, and bars at or after 16:00, do not signal. `window_minutes` comes from each range row.

The default trigger is close-only: `"close"`. Long means the bar closes strictly above the opening-range high. Short means it closes strictly below the opening-range low. A wick through the level with the close back inside the range is not a breakout. `entry_price` is that close. `high_low` is opt-in.

`trigger="high_low"` fires when the bar's high trades strictly above the opening-range high, or its low trades strictly below the opening-range low. `entry_price` is the broken level (`or_high` or `or_low`), not the wick. A bar that trades through both levels is skipped, and scanning continues. Tagging a level exactly is not a break under either rule.

`entry_price` is a reference on the signal, not a fill. The function does not size a position or compute P&L.

Early-close sessions are excluded from signals. The recommended path is `load_ohlcv`, then `signal_bars`, then `opening_ranges`, then `breakout_signals`. When the bar frame still has an `early_close` column, `breakout_signals` drops rows where that flag is true before it scans. There is no parameter that puts those sessions back.

## CLI

`python -m orb` and the `orb-signals` console script (from `[project.scripts]`, available after install) print the `list[Signal]`.

The default mode is the research scan above. It is **not** the frozen ES ORB spec. `--window` is the opening-range length in minutes (default 15) and the session must contain every minute. `--trigger` is `close` or `high_low`. The default is close-only (`close`): the first 1-minute close strictly beyond the range. `high_low` is opt-in.

`--preset frozen-es` is the spec signal, via `frozen_es_signals`. The box is `[09:30, 10:00)` America/New_York. A missing minute after 09:30 still produces a partial range (max high, min low of the bars present). A day is skipped only when the 09:30 bar is absent or the box is empty. The trigger is the first clock-aligned 5-minute close strictly above the box high, long only. Buckets run 10:00, 10:05, ..., 13:50. Each bucket is `[t, t+5)` and the close is the 1-minute bar at `t+4`, so the 10:00 bucket is stamped 10:04. The 13:55 bucket is not eligible. `--window` and `--trigger` do not apply under the preset. The preset does not place a stop, choose a target, or compute P&L.

`--symbol` keeps one symbol, which is how a multi-contract Databento file is narrowed to one outright. The default input is the bundled synthetic fixture. `load_ohlcv` refuses that content unless `--allow-synthetic` is set, which is passed through as `allow_synthetic=True`. `--data` selects another local CSV or Parquet file. A renamed copy of the fixture is still refused without the flag, because the check is the loader's content checksum, not the file name.

Early-close sessions stay excluded; the CLI always calls `signal_bars` and has no flag that puts them back. Stdout is a readable table or JSON (`--format table` or `--format json`). `--out` writes a file instead of stdout, CSV or JSON by the extension.

Loader failures, including a missing path and a refused synthetic file, print a one-line error and exit nonzero.

```bash
python -m orb --allow-synthetic
python -m orb --allow-synthetic --preset frozen-es --format json
orb-signals --data path/to/es.csv --symbol ESH6 --preset frozen-es --out signals.csv
orb-signals --data path/to/ohlcv.csv --window 15 --trigger close --out signals.csv
```

```python
from orb import frozen_es_signals, load_ohlcv, sample_fixture_path, signal_bars

bars = signal_bars(
    load_ohlcv(sample_fixture_path(), allow_synthetic=True, symbol="MES")
)
signals = frozen_es_signals(bars)
```

## Tests

```bash
python -m pip install -e ".[dev]"
pytest
```

## Structure
- `src/orb/` — Installable package. Sample-data loader, opening-range calculation, breakout signals, the frozen ES signal preset, a small CLI, and synthetic fixture.
- `notebooks/` — Exploratory analysis and result visualization.
- `results/` — Walk-forward outputs (CSV, plots, summaries). Gitignored large files.
- `templates/` — Bot descriptions, prompt examples, and orchestration notes.
- `docs/` — Additional methodology notes.

## Looping Grok Bots Workflow
- **Orchestrator**: Bob (Chief of Staff)
- **Specialist**: Engineer bot focused on quant research / ORB walk-forward (to be created)

Typical loop:
1. Orchestrator instructs Engineer to work on a concrete next step in this repo (every N minutes).
2. Engineer implements, commits, or opens a PR (often via Cursor cloud agent handoff).
3. Orchestrator reviews progress periodically and suggests one improvement to keep the work on track and self-improving.

See `templates/` for bot description drafts and example prompts.

## Notes
- Keep real market data out of git (use Databento, local caches, or secure storage). The only tracked CSV is the synthetic fixture under `src/orb/fixtures/`.
- Prefer clear, testable code over one-off notebooks for the core engine.
- Boundaries: no live trading, no credential exposure, explicit approval for any external actions beyond this repo.
