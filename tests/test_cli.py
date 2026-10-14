"""Command-line tests for opening-range breakout signals."""

from __future__ import annotations

import csv
import json
import os
import shutil
import site
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from orb.cli import main
from orb.loader import load_ohlcv, sample_fixture_path, signal_bars

NY = "America/New_York"
REPO = Path(__file__).resolve().parents[1]

# Same fixture expectations as tests/test_breakout.py: 10:00 ET close
# beyond the opening range on the first two sessions, and no signal on
# 2024-01-18. January 2024 is Eastern Standard Time (UTC-5).
EXPECTED = [
    {
        "session_date": "2024-01-16",
        "direction": "long",
        "or_high": 5008.0,
        "or_low": 5000.0,
        "entry_ts": "2024-01-16T10:00:00-05:00",
        "entry_price": 5011.0,
        "window_minutes": 15,
        "trigger": "close",
        "early_close": False,
        "session_close": "16:00",
    },
    {
        "session_date": "2024-01-17",
        "direction": "short",
        "or_high": 5058.0,
        "or_low": 5050.0,
        "entry_ts": "2024-01-17T10:00:00-05:00",
        "entry_price": 5047.0,
        "window_minutes": 15,
        "trigger": "close",
        "early_close": False,
        "session_close": "16:00",
    },
]

COLUMNS = [
    "ts_event",
    "rtype",
    "publisher_id",
    "instrument_id",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "symbol",
]


def test_fixture_signals_match_breakout_expectations(capsys: pytest.CaptureFixture[str]) -> None:
    """``--allow-synthetic`` prints the two fixture breakouts and skips the third day.

    The default window is 15 minutes. ``--window 5`` sees the same range
    on this fixture because the extremes print at 09:30 and 09:31 and the
    first close beyond the range is still 10:00 ET.
    """
    assert main(["--allow-synthetic", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == EXPECTED

    assert main(["--allow-synthetic", "--window", "5"]) == 0
    table = capsys.readouterr().out
    assert table.startswith("session_date")
    for row in EXPECTED:
        assert row["session_date"] in table
        assert row["direction"] in table
        assert row["entry_ts"] in table
        assert f"{row['entry_price']:.1f}" in table
        assert f"{row['or_high']:.1f}" in table
        assert f"{row['or_low']:.1f}" in table
    assert "2024-01-18" not in table


def test_refuses_synthetic_content_without_the_flag(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The bundled fixture and a renamed copy both fail closed.

    Refusal follows the loader's content check. The file name is not what
    blocks the load, and there is no CLI switch that skips that check
    other than ``--allow-synthetic``.
    """
    assert main([]) == 1
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "Traceback" not in refused.err
    assert "--allow-synthetic" in refused.err
    assert "synthetic" in refused.err

    copy = tmp_path / "renamed-bars.csv"
    copy.write_bytes(sample_fixture_path().read_bytes())
    assert main(["--data", str(copy)]) == 1
    copied = capsys.readouterr()
    assert copied.out == ""
    assert "--allow-synthetic" in copied.err

    assert main(["--data", str(copy), "--allow-synthetic", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == EXPECTED


def test_window_selects_the_opening_range(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--window`` is the opening-range length, not a display option.

    Sixteen 1-minute bars start at 09:30. A 15-minute window leaves the
    09:45 close as the break. A 16-minute window consumes that bar and
    emits nothing. A non-positive window is a loader-path error.
    """
    path = tmp_path / "one-session.csv"
    _write_break_session(path, ["2024-01-16"])

    assert main(["--data", str(path), "--window", "15", "--format", "json"]) == 0
    signals = json.loads(capsys.readouterr().out)
    assert signals == [
        {
            "session_date": "2024-01-16",
            "direction": "long",
            "or_high": 10.0,
            "or_low": 6.0,
            "entry_ts": "2024-01-16T09:45:00-05:00",
            "entry_price": 11.0,
            "window_minutes": 15,
            "trigger": "close",
            "early_close": False,
            "session_close": "16:00",
        }
    ]

    assert main(["--data", str(path), "--window", "16", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert main(["--data", str(path), "--window", "0"]) == 1
    failed = capsys.readouterr()
    assert failed.out == ""
    assert "window_minutes" in failed.err
    assert "Traceback" not in failed.err


def test_out_writes_csv_and_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """``--out`` selects CSV or JSON from the extension and stays off stdout."""
    csv_path = tmp_path / "signals.csv"
    json_path = tmp_path / "signals.json"
    args = ["--allow-synthetic", "--out"]

    assert main([*args, str(csv_path)]) == 0
    assert capsys.readouterr().out == ""
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["session_date"] for row in rows] == ["2024-01-16", "2024-01-17"]
    assert [row["direction"] for row in rows] == ["long", "short"]
    assert [float(row["entry_price"]) for row in rows] == [5011.0, 5047.0]
    assert [float(row["or_high"]) for row in rows] == [5008.0, 5058.0]
    assert [float(row["or_low"]) for row in rows] == [5000.0, 5050.0]
    assert [row["entry_ts"] for row in rows] == [
        "2024-01-16T10:00:00-05:00",
        "2024-01-17T10:00:00-05:00",
    ]
    assert [row["early_close"] for row in rows] == ["False", "False"]
    assert [row["session_close"] for row in rows] == ["16:00", "16:00"]
    assert list(rows[0]) == [
        "session_date",
        "direction",
        "or_high",
        "or_low",
        "entry_ts",
        "entry_price",
        "window_minutes",
        "trigger",
        "early_close",
        "session_close",
    ]
    assert "2024-01-18" not in csv_path.read_text()

    assert main([*args, str(json_path), "--format", "table"]) == 0
    assert capsys.readouterr().out == ""
    assert json.loads(json_path.read_text()) == EXPECTED


def test_missing_data_exits_nonzero(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """A path the loader cannot read exits 1 with a message and no traceback."""
    missing = tmp_path / "no-such-bars.csv"
    assert main(["--data", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "not found" in captured.err
    assert missing.name in captured.err


def test_early_close_sessions_are_included(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The CLI keeps a holiday short session and preserves the flag.

    2024-07-03 is a CME equity early close with a complete opening range
    and a later close beyond it, still before the halt. 2024-07-05 is a
    full session of the same shape. Both are printed. There is no switch
    that drops the early close.
    """
    path = tmp_path / "early-close.csv"
    _write_break_session(path, ["2024-07-03", "2024-07-05"])
    loaded = load_ohlcv(path, symbol="MES")
    kept = signal_bars(loaded)
    assert bool(kept.loc[kept.index.date == date(2024, 7, 3), "early_close"].all())
    assert not bool(kept.loc[kept.index.date == date(2024, 7, 5), "early_close"].any())

    assert main(["--data", str(path), "--format", "json"]) == 0
    signals = json.loads(capsys.readouterr().out)
    assert [row["session_date"] for row in signals] == ["2024-07-03", "2024-07-05"]
    assert [row["direction"] for row in signals] == ["long", "long"]
    assert [row["entry_price"] for row in signals] == [11.0, 11.0]
    assert signals[0]["entry_ts"] == "2024-07-03T09:45:00-04:00"
    assert signals[1]["entry_ts"] == "2024-07-05T09:45:00-04:00"
    assert signals[0]["early_close"] is True
    assert signals[1]["early_close"] is False
    assert signals[0]["session_close"] == "13:00"
    assert signals[1]["session_close"] == "16:00"

    with pytest.raises(SystemExit) as exc:
        main(["--data", str(path), "--include-early-close"])
    assert exc.value.code != 0


def test_frozen_es_preset_on_the_fixture_is_one_long_at_1004(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--preset frozen-es`` is the spec signal, not the research default.

    The fixture yields one long: 2024-01-16, entry 5011.0 at 10:04 ET.
    ``--window`` and ``--trigger`` do not override that rule.
    """
    assert main(["--allow-synthetic", "--preset", "frozen-es", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == [
        {
            "session_date": "2024-01-16",
            "direction": "long",
            "or_high": 5008.0,
            "or_low": 5000.0,
            "entry_ts": "2024-01-16T10:04:00-05:00",
            "entry_price": 5011.0,
            "window_minutes": 30,
            "trigger": "close_5m",
            "early_close": False,
            "session_close": "16:00",
        }
    ]

    assert main(["--allow-synthetic", "--preset", "frozen-es", "--window", "15"]) == 1
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "--window" in refused.err
    assert "Traceback" not in refused.err


def test_help_labels_the_research_default_as_not_the_spec(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Help text says the default scan is not the frozen spec and is close-only."""
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "NOT the frozen ES ORB spec" in text
    assert "close-only" in text
    assert "frozen-es" in text
    assert "--trigger" in text


def test_symbol_filters_a_multi_contract_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--symbol`` keeps one contract from a file that holds two."""
    path = tmp_path / "es-quarters.csv"
    _write_two_contracts(path)
    assert main(["--data", str(path), "--preset", "frozen-es"]) == 1
    mixed = capsys.readouterr()
    assert mixed.out == ""
    assert "multiple symbols" in mixed.err

    assert main(
        ["--data", str(path), "--preset", "frozen-es", "--symbol", "ESH6", "--format", "json"]
    ) == 0
    esh = json.loads(capsys.readouterr().out)
    assert len(esh) == 1
    assert esh[0]["entry_price"] == 5011.0
    assert esh[0]["or_high"] == 5008.0
    assert esh[0]["direction"] == "long"

    assert main(
        ["--data", str(path), "--preset", "frozen-es", "--symbol", "ESM6", "--format", "json"]
    ) == 0
    esm = json.loads(capsys.readouterr().out)
    assert len(esm) == 1
    assert esm[0]["entry_price"] == 5111.0
    assert esm[0]["or_high"] == 5108.0


def test_trigger_defaults_to_close_and_high_low_is_opt_in(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The research default is the close rule. ``--trigger high_low`` is separate."""
    path = tmp_path / "one-session.csv"
    _write_break_session(path, ["2024-01-16"])

    assert main(["--data", str(path), "--format", "json"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default[0]["trigger"] == "close"
    assert default[0]["entry_price"] == 11.0
    assert default[0]["entry_ts"] == "2024-01-16T09:45:00-05:00"

    assert main(["--data", str(path), "--trigger", "high_low", "--format", "json"]) == 0
    pierced = json.loads(capsys.readouterr().out)
    assert pierced[0]["trigger"] == "high_low"
    assert pierced[0]["entry_price"] == 10.0
    assert pierced[0]["entry_ts"] == "2024-01-16T09:45:00-05:00"
    assert pierced[0]["direction"] == "long"


def test_module_and_console_script_print_the_fixture() -> None:
    """``python -m orb`` and the ``orb-signals`` script share this pipeline."""
    module = _run([sys.executable, "-m", "orb", "--allow-synthetic", "--format", "json"])
    assert module.returncode == 0, module.stderr
    assert json.loads(module.stdout) == EXPECTED

    script = _console_script()
    via_script = _run([str(script), "--allow-synthetic", "--format", "json"])
    assert via_script.returncode == 0, via_script.stderr
    assert via_script.stdout == module.stdout


def _console_script() -> Path:
    """Return the installed ``orb-signals`` script.

    A user install places it under the user base ``bin`` directory, which
    is not always on ``PATH``. An editable install in a virtualenv places
    it next to the interpreter.
    """
    found = shutil.which("orb-signals")
    if found:
        return Path(found)
    candidates = [
        Path(sys.executable).resolve().parent / "orb-signals",
        Path(site.getuserbase()) / "bin" / "orb-signals",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AssertionError(
        "orb-signals is not installed; run python -m pip install -e '.[dev]'"
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    src = str(REPO / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not current else src + os.pathsep + current
    return subprocess.run(
        argv,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_two_contracts(path: Path) -> None:
    """Two ES quarterlies, each with a 09:30 box bar and a 10:04 close."""
    rows: list[dict[str, object]] = []
    specs = (
        ("ESH6", 5000.0, 5008.0, 5011.0),
        ("ESM6", 5100.0, 5108.0, 5111.0),
    )
    for symbol, low, high, break_close in specs:
        for minute, bar_high, bar_low, close in (
            (0, high, low, (high + low) / 2),
            (34, break_close + 1, break_close - 1, break_close),
        ):
            stamp = pd.Timestamp("2024-01-16 09:30", tz=NY) + pd.Timedelta(minutes=minute)
            utc = stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
            rows.append(
                {
                    "ts_event": utc,
                    "rtype": 33,
                    "publisher_id": 1,
                    "instrument_id": 9000001,
                    "open": close,
                    "high": bar_high,
                    "low": bar_low,
                    "close": close,
                    "volume": 10,
                    "symbol": symbol,
                }
            )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_break_session(path: Path, days: list[str]) -> None:
    """16 RTH minutes: a 15-minute range, then a close above that high."""
    rows: list[dict[str, object]] = []
    for day in days:
        start = pd.Timestamp(f"{day} 09:30", tz=NY)
        for offset in range(16):
            stamp = start + pd.Timedelta(minutes=offset)
            utc = stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
            if offset < 15:
                high, low, close = 10.0, 6.0, 8.0
            else:
                high, low, close = 12.0, 8.0, 11.0
            rows.append(
                {
                    "ts_event": utc,
                    "rtype": 33,
                    "publisher_id": 1,
                    "instrument_id": 9000001,
                    "open": 8.0,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 10,
                    "symbol": "MES",
                }
            )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
