"""Command-line entry for opening-range breakout signals.

``python -m orb`` and the ``orb-signals`` console script load a local
OHLCV-1m file, drop early-close sessions, and print the first breakout
of each remaining session. The default file is the bundled synthetic
fixture, which ``load_ohlcv`` refuses unless ``--allow-synthetic`` is
passed through as ``allow_synthetic=True``.

This command does not size a position, choose an exit, or compute P&L.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

import pandas as pd

from orb.breakout import Signal, breakout_signals
from orb.loader import load_ohlcv, sample_fixture_path, signal_bars
from orb.opening_range import DEFAULT_WINDOW_MINUTES, opening_ranges

COLUMNS = (
    "session_date",
    "direction",
    "or_high",
    "or_low",
    "entry_ts",
    "entry_price",
)

_FORMATS = ("table", "json")


def main(argv: list[str] | None = None) -> int:
    """Run the signal pipeline and print or write the resulting signals.

    Returns 0 on success and 1 when loading or writing fails. Argument
    errors use the parser's own nonzero status.
    """
    args = _parser().parse_args(argv)
    path = sample_fixture_path() if args.data is None else Path(args.data)
    try:
        signals = _signals(
            path,
            allow_synthetic=args.allow_synthetic,
            window_minutes=args.window,
        )
    except FileNotFoundError as exc:
        return _fail(f"data file not found: {_location(exc, path)}")
    except (ValueError, TypeError, ImportError, OSError) as exc:
        return _fail(_loader_message(exc))
    try:
        _emit(signals, fmt=args.format, out=None if args.out is None else Path(args.out))
    except FileNotFoundError as exc:
        return _fail(f"cannot write output file: {_location(exc, Path(args.out))}")
    except OSError as exc:
        return _fail(f"cannot write output file: {exc}")
    except ValueError as exc:
        return _fail(str(exc))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orb-signals",
        description=(
            "Load OHLCV-1m bars and print the first opening-range breakout "
            "of each session. Early-close sessions are always excluded. "
            "This command does not size positions or compute P&L."
        ),
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help=(
            "Local OHLCV-1m .csv or .parquet file. "
            "Default: the bundled synthetic fixture "
            f"({sample_fixture_path().name}), which requires --allow-synthetic."
        ),
    )
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help=(
            "Pass allow_synthetic=True to load_ohlcv so bars that match the "
            "bundled synthetic fixture can be loaded."
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_MINUTES,
        help=(
            "Opening-range length in minutes, passed to opening_ranges "
            f"(default: {DEFAULT_WINDOW_MINUTES}). "
            "The window is [09:30, 09:30 + window) America/New_York."
        ),
    )
    parser.add_argument(
        "--format",
        choices=_FORMATS,
        default="table",
        help="Stdout layout when --out is omitted (default: table).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Write signals to this file instead of stdout. "
            ".csv and .json are selected by the file extension."
        ),
    )
    return parser


def _signals(path: Path, *, allow_synthetic: bool, window_minutes: int) -> list[Signal]:
    """Load bars and return the first breakout of each eligible session.

    ``signal_bars`` drops early-close sessions and has no switch that puts
    them back. The same frame feeds ``opening_ranges`` and
    ``breakout_signals``.
    """
    bars = signal_bars(load_ohlcv(path, allow_synthetic=allow_synthetic))
    ranges = opening_ranges(bars, window_minutes=window_minutes)
    return breakout_signals(bars, ranges)


def _emit(signals: list[Signal], *, fmt: str, out: Path | None) -> None:
    records = [_record(signal) for signal in signals]
    if out is not None:
        _write(out, records)
        return
    if fmt == "json":
        sys.stdout.write(_json_text(records))
        return
    sys.stdout.write(_table_text(records))


def _write(path: Path, records: list[dict[str, object]]) -> None:
    suffix = path.suffix.lower()
    if suffix == ".json":
        text = _json_text(records)
    elif suffix == ".csv":
        text = _csv_text(records)
    else:
        raise ValueError(f"output path must end in .csv or .json; got {path.name!r}")
    path.write_text(text, encoding="utf-8")


def _record(signal: Signal) -> dict[str, object]:
    return {
        "session_date": signal.session_date.isoformat(),
        "direction": signal.direction,
        "or_high": float(signal.or_high),
        "or_low": float(signal.or_low),
        "entry_ts": pd.Timestamp(signal.entry_ts).isoformat(),
        "entry_price": float(signal.entry_price),
    }


def _json_text(records: list[dict[str, object]]) -> str:
    return json.dumps(records, indent=2) + "\n"


def _csv_text(records: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({name: _cell(record, name) for name in COLUMNS})
    return buffer.getvalue()


def _table_text(records: list[dict[str, object]]) -> str:
    rows = [COLUMNS]
    rows.extend(tuple(_cell(record, name) for name in COLUMNS) for record in records)
    widths = [max(len(row[index]) for row in rows) for index in range(len(COLUMNS))]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    lines = [line(rows[0]), line(tuple("-" * width for width in widths))]
    lines.extend(line(row) for row in rows[1:])
    return "\n".join(lines) + "\n"


def _cell(record: dict[str, object], name: str) -> str:
    value = record[name]
    if isinstance(value, float):
        return _format_float(value)
    return str(value)


def _format_float(value: float) -> str:
    text = format(value, ".10f").rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _loader_message(exc: BaseException) -> str:
    text = str(exc)
    if "allow_synthetic" in text:
        return (
            "Refusing to load synthetic fixture content; "
            "pass --allow-synthetic to load fabricated test data"
        )
    return text


def _location(exc: FileNotFoundError, fallback: Path) -> str:
    filename = exc.filename
    if isinstance(filename, (str, Path)) and str(filename):
        return str(filename)
    text = str(exc).strip()
    return text or str(fallback)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
