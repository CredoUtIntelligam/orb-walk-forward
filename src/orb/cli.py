"""Command-line entry for opening-range breakout signals.

``python -m orb`` and the ``orb-signals`` console script load a local
OHLCV-1m file, drop early-close sessions, and print signals.

The default mode is a research scan and is NOT the frozen ES ORB spec.
It uses a complete opening range (default 15 minutes) and the first
1-minute break, long or short, through 16:00 ET. The default trigger is
close-only: a 1-minute close strictly beyond the range. ``--trigger
high_low`` is opt-in.

``--preset frozen-es`` selects the spec signal: 30-minute box
``[09:30, 10:00)``, partial when minutes are missing, first clock-aligned
5-minute close strictly above the box high, long only, no entry after the
13:50-13:55 bucket. ``entry_ts`` is the bucket close minute (10:04 for
the 10:00 bucket).

The default file is the bundled synthetic fixture, which ``load_ohlcv``
refuses unless ``--allow-synthetic`` is passed through as
``allow_synthetic=True``.

This command does not size a position, choose an exit, or compute P&L.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from orb.breakout import DEFAULT_TRIGGER, Signal, breakout_signals
from orb.loader import load_ohlcv, sample_fixture_path, signal_bars
from orb.opening_range import DEFAULT_WINDOW_MINUTES, opening_ranges
from orb.preset import FROZEN_ES_PRESET, frozen_es_signals

COLUMNS = (
    "session_date",
    "direction",
    "or_high",
    "or_low",
    "entry_ts",
    "entry_price",
    "window_minutes",
    "trigger",
)

_FORMATS = ("table", "json")
_RESEARCH_TRIGGERS = ("close", "high_low")


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
            symbol=args.symbol,
            window_minutes=args.window,
            trigger=args.trigger,
            preset=args.preset,
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
            "Load OHLCV-1m bars and print opening-range breakout signals. "
            "Early-close sessions are always excluded. "
            "This command does not size positions, choose exits, or compute P&L. "
            "The default mode is a research scan and is NOT the frozen ES ORB spec: "
            "a complete opening range (default 15 minutes) and the first 1-minute "
            "break, long or short. The default trigger is close-only. "
            "--preset frozen-es is the spec signal."
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
        "--symbol",
        default=None,
        help=(
            "Keep one symbol, passed to load_ohlcv. "
            "Use this to select a contract from a multi-contract file."
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
        "--preset",
        choices=(FROZEN_ES_PRESET,),
        default=None,
        help=(
            "frozen-es: the frozen ES ORB signal (NOT the research default). "
            "30-minute box [09:30, 10:00), partial when a later minute is "
            "missing, skip a day only when the 09:30 bar is absent or the "
            "box is empty, first clock-aligned 5-minute close strictly above "
            "the box high, long only, buckets 10:00 through 13:50, entry_ts "
            "at the bucket close minute (10:04 for the 10:00 bucket). "
            "Replaces --window and --trigger."
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help=(
            "Research mode only (NOT the frozen spec). Opening-range length "
            f"in minutes (default: {DEFAULT_WINDOW_MINUTES} when --preset "
            "is omitted). The window is [09:30, 09:30 + window) "
            "America/New_York and must be complete."
        ),
    )
    parser.add_argument(
        "--trigger",
        choices=_RESEARCH_TRIGGERS,
        default=None,
        help=(
            "Research mode only (NOT the frozen spec). "
            "close (the default, close-only): the first 1-minute close "
            "strictly beyond the range, long or short. "
            "high_low: the first 1-minute high or low that trades strictly "
            "through the level. The default is close-only; high_low is opt-in."
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


def _signals(
    path: Path,
    *,
    allow_synthetic: bool,
    symbol: str | None,
    window_minutes: int | None,
    trigger: str | None,
    preset: str | None,
) -> list[Signal]:
    """Load bars and return one breakout per eligible session.

    ``signal_bars`` drops early-close sessions and has no switch that puts
    them back. The research path then calls ``opening_ranges`` and
    ``breakout_signals``. ``--preset frozen-es`` calls ``frozen_es_signals``
    instead.
    """
    if preset == FROZEN_ES_PRESET:
        if window_minutes is not None:
            raise ValueError(
                "--window does not apply with --preset frozen-es; "
                "the spec opening range is 30 minutes [09:30, 10:00)"
            )
        if trigger is not None:
            raise ValueError(
                "--trigger does not apply with --preset frozen-es; "
                "the spec trigger is the first clock-aligned 5-minute close "
                "strictly above the opening-range high"
            )
    bars = signal_bars(
        load_ohlcv(path, symbol=symbol, allow_synthetic=allow_synthetic)
    )
    if preset == FROZEN_ES_PRESET:
        return frozen_es_signals(bars)
    window = DEFAULT_WINDOW_MINUTES if window_minutes is None else window_minutes
    rule = DEFAULT_TRIGGER if trigger is None else trigger
    ranges = opening_ranges(bars, window_minutes=window)
    return breakout_signals(bars, ranges, trigger=rule)


def _emit(signals: list[Signal], *, fmt: str, out: Path | None) -> None:
    records = [signal.to_dict() for signal in signals]
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
