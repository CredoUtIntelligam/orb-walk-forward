"""Command-line tests for opening-range breakout signals."""

from __future__ import annotations

import csv
import json
import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from orb.cli import main
from orb.loader import sample_fixture_path

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
    },
    {
        "session_date": "2024-01-17",
        "direction": "short",
        "or_high": 5058.0,
        "or_low": 5050.0,
        "entry_ts": "2024-01-17T10:00:00-05:00",
        "entry_price": 5047.0,
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


def test_early_close_sessions_are_excluded(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The CLI runs signal_bars, so a holiday short session never signals.

    2024-07-03 is a CME equity early close with a complete opening range
    and a later close beyond it. 2024-07-05 is a full session of the same
    shape. Only the full session is printed. There is no flag to put the
    early close back.
    """
    path = tmp_path / "early-close.csv"
    _write_break_session(path, ["2024-07-03", "2024-07-05"])
    assert main(["--data", str(path), "--format", "json"]) == 0
    signals = json.loads(capsys.readouterr().out)
    assert [row["session_date"] for row in signals] == ["2024-07-05"]
    assert signals[0]["direction"] == "long"
    assert signals[0]["entry_price"] == 11.0
    assert signals[0]["entry_ts"] == "2024-07-05T09:45:00-04:00"

    with pytest.raises(SystemExit) as exc:
        main(["--data", str(path), "--include-early-close"])
    assert exc.value.code != 0


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
