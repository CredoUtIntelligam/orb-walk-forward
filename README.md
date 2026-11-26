# ORB Walk-Forward

Private research repository for Opening Range Breakout (ORB) strategy research and walk-forward analysis, primarily on MES/ES futures.

This repo is designed to be driven by looping Grok Bots (orchestrator + specialized engineer) with Cursor handoffs.

## Goal
Build a robust, reproducible walk-forward framework for ORB variants (range length, entry confirmation, filters, targets/stops, etc.) using Databento data where available. Results should support the existing Vega bot's ORB research.

## Structure
- `src/` — Core Python modules for data loading, ORB signal generation, walk-forward engine, metrics.
- `src/orb_walk_forward/harness/` — Paper/simulation harness for a frozen ORB spec. No live orders.
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

## Paper harness

Research-only skeleton. It replays or reads one-minute bars, builds a 15- or 30-minute RTH opening range, and simulates a break-then-retest bracket. `SimBroker` is the only broker that can accept orders. `IbkrMarketDataStub` raises `NotImplementedError` for orders and is not a live data feed. The optional Databento key is read from `DATABENTO_API_KEY` and is never logged. Replay does not need it.

```bash
PYTHONPATH=src python -m orb_walk_forward.harness.main \
  --replay bars.csv --or-minutes 30 --db run.sqlite
```

CSV columns are `ts,open,high,low,close,volume`. Naive timestamps are America/Chicago. RTH open is 08:30 CT (09:30 ET). The runner flattens open paper positions on the first bar at or after 15:00 CT, at that bar's open, and logs the flatten as an order, a fill, and a `cash_close` event. Shutdown flattening still runs if the process stops earlier. Databento `ts_event` is the bar start. The Databento symbology default is `raw_symbol`.

A missing minute is logged (a warning and a `gap` event) and is not forward-filled. A gap inside the opening window does not invalidate the session; the range uses the bars that arrived. Long and short are independent: at most one signal per side, and both brackets may be open in the same session. When the entry fills, the stop and target are rebuilt from that fill. The original size is kept even if the new stop distance would allow fewer contracts. Assumptions and parameters are in the harness module docstrings.

```bash
python -m pytest
```

## Notes
- Keep data out of git (use Databento, local caches, or secure storage).
- Prefer clear, testable code over one-off notebooks for the core engine.
- Boundaries: no live trading, no credential exposure, explicit approval for any external actions beyond this repo.
