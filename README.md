# ORB Walk-Forward

Private research repository for Opening Range Breakout (ORB) strategy research and walk-forward analysis, primarily on MES/ES futures.

This repo is designed to be driven by looping Grok Bots (orchestrator + specialized engineer) with Cursor handoffs.

## Goal
Build a robust, reproducible walk-forward framework for ORB variants (range length, entry confirmation, filters, targets/stops, etc.) using Databento data where available. Results should support the existing Vega bot's ORB research.

The code in this repo is landing one piece at a time. Piece 1 is the sample-data loader below. Opening-range calculation, breakout signals, a small CLI, and their tests are later pieces. A walk-forward optimizer, position sizing, execution, and P&L are out of scope until that core exists.

## Package layout

The installable package is `orb`, in `src/orb/` (src layout). `src/__init__.py` is a leftover skeleton marker and is not imported.

```
src/orb/__init__.py
src/orb/loader.py
src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv
src/orb/fixtures/README.md
tests/test_loader.py
pyproject.toml
```

Python 3.10+. Runtime dependencies are pandas, numpy, pyarrow, tzdata, and pinned `pandas-market-calendars==5.5.0` with `exchange-calendars==4.13.2`. pytest is a dev extra, not a runtime dependency. The test suite does not need the Databento SDK or an API key.

## Sample-data loader

`load_ohlcv` reads a local Databento OHLCV-1m CSV or Parquet file. It does not download anything.

`ts_event` must be UTC: ISO-8601 text with an explicit `Z` or `+00:00` offset, or integer nanoseconds. Naive timestamps, non-UTC offsets, mixed offsets, and non-integer digit-like strings are rejected. The frame is indexed by timezone-aware `America/New_York` timestamps, so daylight saving time follows the IANA timezone. OHLC columns are floats and `volume` is int. Bars with a non-finite or non-positive price, `high < low`, an open or close outside `[low, high]`, or a negative volume are rejected. A repeated symbol and timestamp raises unless `drop_duplicates=True`.

Pass `symbol` to keep one instrument. Pass `rth_only=True` to keep CME equity regular trading hours: `[09:30, 16:00)` ET on a full session, using the `CME_Equity` calendar for weekends, full holidays, and early closes. 16:00 is exclusive because these bars are stamped at the start of the minute; the last full-session bar starts at 15:59. An earlier exchange halt shortens that window. Missing minutes inside a session are reported (`rth_gap_before` and `rth_gap_report`) and are never filled.

Holiday short sessions, when futures still trade a shortened day (July 4, Thanksgiving, MLK Day, and other CME equity halts before 16:00 ET), are early closes. They are flagged on `early_close`, stay in the loaded frame, and stay eligible for the gap report. `signal_bars` always removes them. That exclusion is fixed; there is no switch to include early-close sessions in signals.

```python
from orb import load_ohlcv, sample_fixture_path, signal_bars

bars = load_ohlcv(
    sample_fixture_path(),
    symbol="MES",
    rth_only=True,
    allow_synthetic=True,
)
eligible = signal_bars(bars)
```

`src/orb/fixtures/SYNTHETIC_TEST_DATA_mes_ohlcv-1m.csv` is synthetic test data: three fabricated MES regular-hours sessions shaped so a later opening-range check can see a long breakout, a short breakout, and a day with no breakout. It is not real market data. Do not compute performance from it. `load_ohlcv` recognizes that content by a checksum of the normalized rows, not by the file name, and raises unless `allow_synthetic=True`. A renamed copy or a Parquet conversion of the same bars is refused the same way. See `src/orb/fixtures/README.md`.

## Tests

```bash
python -m pip install -e ".[dev]"
pytest
```

## Structure
- `src/orb/` — Installable package. Currently the sample-data loader and synthetic fixture.
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
