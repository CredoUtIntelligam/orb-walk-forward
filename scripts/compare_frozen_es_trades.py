"""Compare ``frozen_es_signals`` with a frozen ES trade list.

The bars file is front-month ES regular-hours 1-minute data whose clock
column is ``timestamp`` (America/New_York, timezone-aware ISO). This
script rewrites that file to the loader's UTC ``ts_event`` schema in a
temporary directory and calls ``load_ohlcv``, so the loader's validations
still run. The trade file is not loaded by the package.

Comparison, one row per session date:

- ``box_high`` vs ``or_high``
- ``box_low`` vs ``or_low``
- ``signal_time`` vs ``entry_ts``
- ``signal_close`` vs ``entry_price``

A match agrees on the date and all four fields. A field mismatch shares
the date and differs on at least one field. A missing signal is a trade
date with no frozen signal. An extra signal is a frozen signal whose
date is not in the trade file.

Early-close sessions are the trade dates whose CME equity halt is before
16:00 ET. The script reports how many of those dates match.

Usage::

    python scripts/compare_frozen_es_trades.py \\
        --bars path/to/es_rth_1m.csv.gz \\
        --trades path/to/trades.csv
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

from orb.loader import (
    NY_TZ,
    RTH_CLOSE_MINUTES,
    calendar_rth_close_minute,
    load_ohlcv,
    signal_bars,
)
from orb.preset import frozen_es_signals

PRICE_ATOL = 1e-6
SYMBOL = "ES"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bars_path = Path(args.bars)
    trades_path = Path(args.trades)
    if not bars_path.is_file():
        return _fail(f"bars file not found: {bars_path}")
    if not trades_path.is_file():
        return _fail(f"trades file not found: {trades_path}")
    try:
        report = compare(bars_path, trades_path, symbol=args.symbol)
    except (OSError, ValueError, TypeError) as exc:
        return _fail(str(exc))
    sys.stdout.write(format_report(report))
    return 0


def compare(bars_path: Path, trades_path: Path, *, symbol: str = SYMBOL) -> dict:
    """Load both files and return the match counts.

    The bars file is adapted to ``ts_event`` UTC plus ``symbol`` and then
    passed through ``load_ohlcv`` with ``rth_only=False``, so a 13:00-13:14
    print on a calendar 13:00 halt stays available to the cutoff.
    """
    trades = _read_trades(trades_path)
    with tempfile.TemporaryDirectory(prefix="orb-frozen-es-") as tmp:
        adapted = Path(tmp) / "es_rth_ts_event.csv"
        _write_loader_csv(bars_path, adapted, symbol=symbol)
        loaded = load_ohlcv(adapted, symbol=symbol, rth_only=False)
    signals = frozen_es_signals(signal_bars(loaded))
    return _score(trades, signals)


def format_report(report: dict) -> str:
    lines = [
        f"trades: {report['trades']}",
        f"signals: {report['signals']}",
        f"matches: {report['matches']} / {report['trades']}",
        f"missing signals: {report['missing']}",
        f"extra signals: {report['extra']}",
        f"field mismatches: {report['field_mismatches']}",
        (
            "early-close sessions in the trade list: "
            f"{report['early_close_trades']}"
        ),
        (
            "early-close matches: "
            f"{report['early_close_matches']} / {report['early_close_trades']}"
        ),
    ]
    if report["missing_dates"]:
        lines.append("missing dates: " + ", ".join(report["missing_dates"][:20]))
    if report["extra_dates"]:
        lines.append("extra dates: " + ", ".join(report["extra_dates"][:20]))
    for row in report["mismatch_examples"]:
        lines.append(
            f"mismatch {row['date']}: " + ", ".join(row["fields"])
        )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare frozen_es_signals on ET-timestamp ES bars "
            "with a frozen trade list."
        )
    )
    parser.add_argument("--bars", required=True, help="ES RTH 1-minute CSV or .csv.gz")
    parser.add_argument("--trades", required=True, help="Frozen trade CSV")
    parser.add_argument(
        "--symbol",
        default=SYMBOL,
        help="Symbol written into the adapted loader file (default ES)",
    )
    return parser


def _write_loader_csv(source: Path, dest: Path, *, symbol: str) -> None:
    raw = pd.read_csv(source)
    required = ("timestamp", "open", "high", "low", "close", "volume")
    missing = [name for name in required if name not in raw.columns]
    if missing:
        raise ValueError(f"bars file is missing columns: {missing}")
    stamps = pd.to_datetime(raw["timestamp"], utc=True)
    if stamps.isna().any():
        raise ValueError("timestamp contains values that are not ISO-8601")
    out = pd.DataFrame(
        {
            "ts_event": stamps.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "symbol": symbol,
            "open": raw["open"],
            "high": raw["high"],
            "low": raw["low"],
            "close": raw["close"],
            "volume": raw["volume"],
        }
    )
    out.to_csv(dest, index=False)


def _read_trades(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = ("date", "box_high", "box_low", "signal_time", "signal_close")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"trades file is missing columns: {missing}")
    dates = pd.to_datetime(frame["date"]).dt.date
    if dates.duplicated().any():
        raise ValueError("trades file has more than one row for a session date")
    times = pd.to_datetime(frame["signal_time"], utc=True).dt.tz_convert(NY_TZ)
    if times.isna().any():
        raise ValueError("signal_time contains values that are not timestamps")
    out = pd.DataFrame(
        {
            "date": dates,
            "box_high": frame["box_high"].astype("float64"),
            "box_low": frame["box_low"].astype("float64"),
            "signal_time": times,
            "signal_close": frame["signal_close"].astype("float64"),
        }
    )
    return out


def _score(trades: pd.DataFrame, signals: list) -> dict:
    by_date = {signal.session_date: signal for signal in signals}
    if len(by_date) != len(signals):
        raise ValueError("frozen_es_signals returned more than one row for a date")
    trade_dates = set(trades["date"])
    matches = 0
    missing_dates: list[str] = []
    mismatch_examples: list[dict] = []
    field_mismatches = 0
    early_trades = 0
    early_matches = 0
    for row in trades.itertuples(index=False):
        session = row.date
        early = _is_early_close(session)
        if early:
            early_trades += 1
        signal = by_date.get(session)
        if signal is None:
            missing_dates.append(session.isoformat())
            continue
        fields = _field_diffs(row, signal)
        if fields:
            field_mismatches += 1
            if len(mismatch_examples) < 15:
                mismatch_examples.append({"date": session.isoformat(), "fields": fields})
            continue
        matches += 1
        if early:
            early_matches += 1
    extra_dates = sorted(
        session.isoformat()
        for session in by_date
        if session not in trade_dates
    )
    return {
        "trades": int(len(trades)),
        "signals": int(len(signals)),
        "matches": matches,
        "missing": len(missing_dates),
        "extra": len(extra_dates),
        "field_mismatches": field_mismatches,
        "early_close_trades": early_trades,
        "early_close_matches": early_matches,
        "missing_dates": missing_dates,
        "extra_dates": extra_dates,
        "mismatch_examples": mismatch_examples,
    }


def _is_early_close(session: date) -> bool:
    close = calendar_rth_close_minute(session)
    return close is not None and close < RTH_CLOSE_MINUTES


def _field_diffs(row, signal) -> list[str]:
    diffs: list[str] = []
    if not _price_equal(row.box_high, signal.or_high):
        diffs.append(f"box_high {row.box_high} vs or_high {signal.or_high}")
    if not _price_equal(row.box_low, signal.or_low):
        diffs.append(f"box_low {row.box_low} vs or_low {signal.or_low}")
    entry = pd.Timestamp(signal.entry_ts).tz_convert(NY_TZ)
    if entry != row.signal_time:
        diffs.append(f"signal_time {row.signal_time.isoformat()} vs entry_ts {entry.isoformat()}")
    if not _price_equal(row.signal_close, signal.entry_price):
        diffs.append(
            f"signal_close {row.signal_close} vs entry_price {signal.entry_price}"
        )
    return diffs


def _price_equal(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=PRICE_ATOL)


def _fail(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
