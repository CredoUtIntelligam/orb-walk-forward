# ORB Walk-Forward

Private research repository for Opening Range Breakout (ORB) strategy research and walk-forward analysis, primarily on MES/ES futures.

This repo is designed to be driven by looping Grok Bots (orchestrator + specialized engineer) with Cursor handoffs.

## Goal
Build a robust, reproducible walk-forward framework for ORB variants (range length, entry confirmation, filters, targets/stops, etc.) using Databento data where available. Results should support the existing Vega bot's ORB research.

The code in this repo is landing one piece at a time. Piece 1 is the sample-data loader. Piece 2, below, is the opening-range calculation. Breakout signals, a small CLI, and their tests are later pieces. A walk-forward optimizer, position sizing, execution, and P&L are out of scope until that core exists.

## Package layout

The installable package is `orb`, in `src/orb/` (src layout). `src/__init__.py` is a leftover skeleton marker and is not imported.

```
src/orb/__init__.py
src/orb/loader.py
src/orb/opening_range.py
src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv
src/orb/fixtures/README.md
tests/test_loader.py
tests/test_opening_range.py
pyproject.toml
```

Python 3.10+. Runtime dependencies are pandas and tzdata. pytest is installed with the package so the test suite runs without the Databento SDK or an API key.

## Sample-data loader

`load_ohlcv` reads a local Databento OHLCV-1m CSV (Parquet only if a local engine such as pyarrow is already installed). It does not download anything.

`ts_event` is UTC, either ISO-8601 text or integer nanoseconds. The frame is indexed by timezone-aware `America/New_York` timestamps, so daylight saving time follows the IANA timezone. OHLC columns are floats and `volume` is int. Pass `symbol` to keep one instrument. Pass `rth_only=True` to keep bars in `[09:30, 16:00)` ET. 16:00 is exclusive because these bars are stamped at the start of the minute; the last regular-hours bar starts at 15:59.

```python
from orb import load_ohlcv, opening_ranges, sample_fixture_path

bars = load_ohlcv(sample_fixture_path(), symbol="MES", rth_only=True)
ranges = opening_ranges(bars, window_minutes=15)
```

`src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv` is synthetic test data: three fabricated MES regular-hours sessions shaped so an opening-range check can see a long breakout, a short breakout, and a day with no breakout. It is not real market data. Do not compute performance from it. See `src/orb/fixtures/README.md`.

## Opening range

`opening_ranges` reads that New York 1-minute frame and returns one row per complete session: `session_date`, `range_high`, `range_low`, `window_minutes`, and `bar_count`.

The window starts at 09:30 ET. `window_minutes` defaults to 15 and can be 5, 30, or any other positive length that ends at or before 16:00 ET. Stamps are the start of the minute, so a 15-minute range uses the bars from 09:30 through 09:44 inclusive (`[09:30, 09:45)`). `range_high` is the max high in that window and `range_low` is the min low. Other zones are converted to America/New_York first. Naive timestamps are rejected.

A session is kept only when every minute in the window is present exactly once. A missing minute, a duplicate stamp, or a sub-minute timestamp inside the window drops that day. Bars outside the window, including 09:29 and the 16:00 bar, are ignored. Pass one symbol; a frame that still contains more than one symbol is rejected so two instruments are not blended into one range.

## Tests

```bash
python -m pip install -e .
pytest
```

## Structure
- `src/orb/` — Installable package. Sample-data loader, opening-range calculation, and synthetic fixture.
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
