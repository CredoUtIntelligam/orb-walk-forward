"""Paper-harness tests. Synthetic bars only; no network and no broker."""

from __future__ import annotations

import csv
import logging
import os
import signal
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from orb_walk_forward.harness.ingest import (
    Bar,
    BarBuffer,
    DatabentoSource,
    IbkrMarketDataStub,
    MarketDataError,
    ReplaySource,
    _load_databento,
)
from orb_walk_forward.harness.logger import RunLogger
from orb_walk_forward.harness.main import main, run_loop
from orb_walk_forward.harness.position import PositionParams, SimBroker, size_contracts
from orb_walk_forward.harness.signal import OpeningRangeTracker, Signal, SignalParams

CT = ZoneInfo("America/Chicago")
SECRET = "supersecret-value"
PAPER = PositionParams(equity=100_000.0, risk_pct=0.006, point_value=5.0)


def ts_at(hour: int, minute: int, day: int = 3) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=CT)


def bar_at(hour: int, minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(ts_at(hour, minute), open_, high, low, close)


def opening_range(n: int = 15, high: float = 100.0, low: float = 90.0) -> list[Bar]:
    start = ts_at(8, 30)
    mid = (high + low) / 2
    return [Bar(start + timedelta(minutes=i), mid, high, low, mid) for i in range(n)]


def long_signal(price: float = 102.0, at: datetime | None = None) -> Signal:
    return Signal(date(2024, 1, 3), "long", at or ts_at(8, 46), 100.0, 90.0, price)


class ListSource:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = list(bars)

    def bars(self):
        yield from self._bars


def run_bars(tmp_path: Path, bars: list[Bar], **kwargs):
    log = RunLogger(tmp_path / "run.sqlite")
    broker = run_loop(
        kwargs.pop("source", ListSource(bars)),
        kwargs.pop("signal_params", SignalParams(15)),
        kwargs.pop("position_params", PAPER),
        log,
        install_shutdown=kwargs.pop("install_shutdown", False),
        heartbeat_every=kwargs.pop("heartbeat_every", 1000),
        seed=kwargs.pop("seed", 3),
        config=kwargs.pop("config", {"or_minutes": 15, "api_key": SECRET}),
        **kwargs,
    )
    assert SECRET.encode() not in Path(log.path).read_bytes()
    conn = sqlite3.connect(log.path)
    conn.row_factory = sqlite3.Row
    return broker, conn


def test_buffer_dedupes_gaps_and_keeps_order(caplog: pytest.LogCaptureFixture) -> None:
    buf = BarBuffer()
    first = Bar(datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc), 1, 1, 1, 1)
    naive = Bar(datetime(2024, 1, 3, 8, 31), 2, 2, 2, 2)
    jumped = bar_at(8, 33, 3, 3, 3, 3)
    late = bar_at(8, 32, 4, 4, 4, 4)
    revised = bar_at(8, 30, 9, 9, 9, 9)

    with caplog.at_level(logging.WARNING):
        status, released, gap = buf.add(first)
        assert status == "released" and gap is None
        assert released.ts == ts_at(8, 30)
        assert buf.add(revised)[0] == "duplicate"
        assert buf.bars[0].close == 1  # first sighting wins
        status, released, gap = buf.add(naive)
        assert status == "released" and gap is None
        assert released.ts.isoformat() == "2024-01-03T08:31:00-06:00"
        status, _, gap = buf.add(jumped)
        assert status == "released"
        assert gap == (ts_at(8, 32), ts_at(8, 33))
        assert "not forward-filled" in caplog.text
        status, _, gap = buf.add(late)
        assert status == "late" and gap is None

    assert [bar.ts for bar in buf.bars] == [ts_at(8, 30), ts_at(8, 31), ts_at(8, 32), ts_at(8, 33)]
    assert len(buf.bars) == 4  # the missing minute was not synthesized


def test_replay_normalizes_csv_and_frame(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    path.write_text(
        "ts,open,high,low,close,volume\n"
        "2024-01-03T14:30:00Z,1,2,0.5,1.5,3\n"
        "2024-01-03T08:31:00,1,2,0.5,1.5,\n"
    )
    bars = list(ReplaySource(path=path).bars())
    assert bars[0].ts.isoformat() == "2024-01-03T08:30:00-06:00"
    assert bars[1].ts.isoformat() == "2024-01-03T08:31:00-06:00"
    assert bars[1].volume == 0
    frame = [{"ts_event": "2024-01-03T08:30:00-06:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
    assert list(ReplaySource(frame=frame).bars())[0].close == 1.5
    with pytest.raises(ValueError):
        ReplaySource()
    with pytest.raises(ValueError):
        ReplaySource(path=path, frame=frame)
    with pytest.raises(ValueError):
        list(ReplaySource(frame=[{"ts": "2024-01-03T08:30:00-06:00", "open": 1, "high": 1, "low": 2, "close": 1}]).bars())


def test_no_trigger_on_the_first_break() -> None:
    tracker = OpeningRangeTracker(SignalParams(15))
    premarket = bar_at(8, 29, 95, 150, 50, 95)
    assert tracker.on_bar(premarket) is None
    for bar in opening_range():
        assert tracker.on_bar(bar) is None
    # Trades back through the OR high on the same bar. Still only a break.
    assert tracker.on_bar(bar_at(8, 45, 100, 104, 99, 103)) is None
    signal = tracker.on_bar(bar_at(8, 46, 103, 104, 100, 102))
    assert signal is not None
    assert signal.side == "long"
    assert signal.price == 102
    assert signal.or_high == 100
    assert signal.or_low == 90
    assert signal.session == date(2024, 1, 3)


def test_duplicate_timestamp_does_not_change_the_range() -> None:
    bars = opening_range()
    bars[-1] = bar_at(8, 44, 100, 110, 90, 108)
    tracker = OpeningRangeTracker(SignalParams(15))
    assert all(tracker.on_bar(bar) is None for bar in (bars[0], *bars))
    assert tracker.on_bar(bar_at(8, 45, 108, 112, 107, 111)) is None
    signal = tracker.on_bar(bar_at(8, 46, 111, 112, 110, 111))
    assert signal is not None
    assert signal.or_high == 110
    assert signal.price == 111


def test_long_and_short_retests_once_per_side() -> None:
    tracker = OpeningRangeTracker(SignalParams(15))
    assert all(tracker.on_bar(bar) is None for bar in opening_range())
    assert tracker.on_bar(bar_at(8, 45, 100, 104, 99, 103)) is None
    long_sig = tracker.on_bar(bar_at(8, 46, 103, 104, 100, 102))
    assert long_sig is not None and long_sig.side == "long"
    assert tracker.on_bar(bar_at(8, 47, 102, 103, 100, 101)) is None
    assert tracker.on_bar(bar_at(8, 48, 90, 91, 86, 88)) is None
    short_sig = tracker.on_bar(bar_at(8, 49, 88, 90, 87, 89))
    assert short_sig is not None
    assert short_sig.side == "short"
    assert short_sig.price == 89
    assert short_sig.or_high == 100 and short_sig.or_low == 90
    assert tracker.on_bar(bar_at(8, 50, 89, 90, 86, 88)) is None


def test_thirty_minute_window_and_rejected_lengths() -> None:
    with pytest.raises(ValueError):
        SignalParams(5)
    with pytest.raises(ValueError):
        SignalParams(True)  # type: ignore[arg-type]
    tracker = OpeningRangeTracker(SignalParams(30))
    assert all(tracker.on_bar(bar) is None for bar in opening_range(30, high=2, low=0))
    assert tracker.on_bar(bar_at(9, 0, 2, 3, 1.5, 2.5)) is None
    signal = tracker.on_bar(bar_at(9, 1, 2.5, 2.6, 2.0, 2.4))
    assert signal is not None and signal.side == "long"
    assert signal.or_high == 2 and signal.or_low == 0
    empty = OpeningRangeTracker(SignalParams(15))
    assert empty.on_bar(bar_at(8, 45, 1, 3, 0, 2)) is None


def test_sizing_floors_and_skips() -> None:
    assert size_contracts(100_000, 0.01, 10, 5) == 20
    assert size_contracts(100_000, 0.005, 8, 5) == 12
    assert size_contracts(100_000, 0.005, 12, 5) == 8
    assert size_contracts(1_000, 0.001, 10, 50) == 0
    assert size_contracts(0, 0.01, 10, 5) == 0
    assert size_contracts(100_000, 0.01, 0, 5) == 0
    broker = SimBroker()
    assert broker.submit_bracket(long_signal(), PositionParams(1_000, 0.001, 50)) == []
    assert broker.position_qty() == 0


def test_bracket_fills_track_intended_qty_and_stop_first() -> None:
    broker = SimBroker()
    signal = long_signal()
    entry, stop, target = broker.submit_bracket(signal, PAPER)
    assert entry.intended_qty == 10 and entry.filled_qty == 0
    assert stop.price == 90 and target.price == 114
    assert broker.on_bar(bar_at(8, 46, 102, 104, 100, 102)) == []
    fills = broker.on_bar(bar_at(8, 47, 102, 103, 101, 102.5))
    assert [fill.kind for fill in fills] == ["entry"]
    assert fills[0].price == 102 and fills[0].qty == 10
    assert entry.filled_qty == entry.intended_qty == 10
    assert broker.position_qty() == 10
    assert stop.status == "working" and target.status == "working"

    both = SimBroker()
    both.submit_bracket(signal, PAPER)
    both.on_bar(bar_at(8, 47, 102, 103, 101, 102.5))
    fills = both.on_bar(bar_at(8, 48, 100, 120, 80, 100))
    assert [fill.kind for fill in fills] == ["stop"]
    assert fills[0].price == 90 and fills[0].qty == 10
    assert both.brackets[0].target.status == "cancelled"
    assert both.position_qty() == 0

    target_broker = SimBroker()
    target_broker.submit_bracket(signal, PAPER)
    target_broker.on_bar(bar_at(8, 47, 102, 103, 101, 102.5))
    fills = target_broker.on_bar(bar_at(8, 48, 103, 114, 102, 110))
    assert [fill.kind for fill in fills] == ["target"]
    assert fills[0].price == 114
    assert target_broker.position_qty() == 0

    gap = SimBroker()
    gap.submit_bracket(signal, PAPER)
    fills = gap.on_bar(bar_at(8, 47, 80, 81, 80, 80))
    assert [fill.kind for fill in fills] == ["entry", "stop"]
    assert fills[0].price == 80 and fills[1].price == 80
    assert gap.position_qty() == 0


def test_fixed_stop_and_short_bracket() -> None:
    broker = SimBroker()
    orders = broker.submit_bracket(
        long_signal(),
        PositionParams(100_000, 0.006, 5, stop_mode="fixed", stop_points=4, target_r=1),
    )
    assert [order.price for order in orders] == [None, 98, 106]
    assert orders[0].intended_qty == 30
    short = Signal(date(2024, 1, 3), "short", ts_at(8, 46), 100.0, 90.0, 89.0)
    short_broker = SimBroker()
    short_orders = short_broker.submit_bracket(short, PAPER)
    assert short_orders[0].side == "sell"
    assert short_orders[1].price == 100
    assert short_orders[2].price == 78
    assert short_orders[0].intended_qty == 10
    assert short_broker.on_bar(bar_at(8, 46, 89, 90, 88, 89)) == []
    short_fills = short_broker.on_bar(bar_at(8, 47, 88, 89, 77, 80))
    assert [fill.kind for fill in short_fills] == ["entry", "target"]
    assert short_fills[1].price == 78
    assert short_broker.position_qty() == 0
    with pytest.raises(ValueError):
        PositionParams(1, 0.01, 5, stop_mode="trail")


def test_flatten_all_cancels_pending_and_closes_open() -> None:
    pending = SimBroker()
    pending.submit_bracket(long_signal(), PAPER)
    assert pending.flatten_all() == []
    assert {order.status for order in pending.all_orders()} == {"cancelled"}
    assert pending.flatten_all() == []

    broker = SimBroker()
    broker.submit_bracket(long_signal(), PAPER)
    broker.on_bar(bar_at(8, 47, 102, 103, 101, 102.5))
    assert broker.position_qty() == 10
    fills = broker.flatten_all()
    assert [(fill.kind, fill.qty, fill.price) for fill in fills] == [("flatten", 10, 102.5)]
    assert broker.position_qty() == 0
    assert broker.brackets[0].stop.status == "cancelled"
    assert broker.brackets[0].target.status == "cancelled"
    assert broker.flatten_all() == []


def test_shutdown_flattens_and_logger_writes_every_row(tmp_path: Path) -> None:
    bars = opening_range() + [
        bar_at(8, 45, 100, 104, 99, 103),
        bar_at(8, 46, 103, 104, 100, 102),
        bar_at(8, 47, 102, 103, 101, 102.5),
        bar_at(8, 46, 103, 104, 100, 102),  # replay of the signal bar
    ]

    class InterruptAfter(ListSource):
        def bars(self):
            yield from self._bars
            raise KeyboardInterrupt

    broker, conn = run_bars(
        tmp_path,
        bars,
        source=InterruptAfter(bars),
        heartbeat_every=1,
        seed=7,
    )
    assert broker.position_qty() == 0
    assert conn.execute("select count(*) from bars").fetchone()[0] == 18
    signal = conn.execute("select side, price, or_high, or_low from signals").fetchone()
    assert tuple(signal) == ("long", 102.0, 100.0, 90.0)
    fills = conn.execute("select kind, qty, price from fills order by id").fetchall()
    assert [tuple(row) for row in fills] == [("entry", 10, 102.0), ("flatten", 10, 102.5)]
    latest: dict[int, sqlite3.Row] = {}
    for row in conn.execute("select * from orders order by id"):
        latest[row["order_id"]] = row
    by_kind = {row["kind"]: row for row in latest.values()}
    assert by_kind["entry"]["filled_qty"] == by_kind["entry"]["intended_qty"] == 10
    assert by_kind["stop"]["status"] == "cancelled"
    assert by_kind["target"]["status"] == "cancelled"
    assert by_kind["flatten"]["status"] == "filled"
    assert conn.execute("select count(*) from heartbeats").fetchone()[0] == 18
    kinds = {row["kind"] for row in conn.execute("select kind from events")}
    assert kinds == {"config", "shutdown"}
    config = conn.execute("select detail from events where kind='config'").fetchone()[0]
    assert '"seed": 7' in config
    assert "api_key" not in config
    assert SECRET not in config
    assert "git_sha" in config


def test_gap_row_and_duplicate_replay_do_not_invent_bars(tmp_path: Path) -> None:
    start = ts_at(8, 30)
    bars = [Bar(start, 1, 1, 1, 1), Bar(start + timedelta(minutes=2), 1, 1, 1, 1)]
    _, conn = run_bars(tmp_path, bars, heartbeat_every=0, heartbeat_seconds=None)
    assert conn.execute("select count(*) from bars").fetchone()[0] == 2
    gap = conn.execute("select detail from events where kind='gap'").fetchone()[0]
    assert "08:31:00" in gap and "08:32:00" in gap
    assert conn.execute("select count(*) from signals").fetchone()[0] == 0


def test_wall_clock_heartbeat(tmp_path: Path) -> None:
    class Pause:
        def bars(self):
            yield bar_at(8, 30, 1, 1, 1, 1)
            time.sleep(0.05)
            yield bar_at(8, 31, 1, 1, 1, 1)

    _, conn = run_bars(
        tmp_path,
        [],
        source=Pause(),
        heartbeat_every=0,
        heartbeat_seconds=0.01,
    )
    assert conn.execute("select count(*) from heartbeats").fetchone()[0] >= 1


def test_sigterm_flattens_open_position(tmp_path: Path) -> None:
    bars = opening_range() + [
        bar_at(8, 45, 100, 104, 99, 103),
        bar_at(8, 46, 103, 104, 100, 102),
        bar_at(8, 47, 102, 103, 101, 102.5),
    ]

    class Wait:
        def bars(self):
            yield from bars
            threading.Thread(
                target=lambda: (time.sleep(0.1), os.kill(os.getpid(), signal.SIGTERM)),
                daemon=True,
            ).start()
            time.sleep(5)
            raise AssertionError("SIGTERM did not stop the loop")

    broker, conn = run_bars(tmp_path, bars, source=Wait(), install_shutdown=True)
    assert broker.position_qty() == 0
    kinds = [row[0] for row in conn.execute("select kind from fills order by id")]
    assert kinds == ["entry", "flatten"]
    assert conn.execute("select count(*) from events where kind='shutdown'").fetchone()[0] == 1


def test_reconnect_resumes_without_leaking_the_key(caplog: pytest.LogCaptureFixture) -> None:
    starts: list[datetime | None] = []

    def factory(start: datetime | None):
        starts.append(start)
        if len(starts) == 1:
            def generate():
                yield {"ts": "2024-01-03T08:30:00-06:00", "open": 1, "high": 1, "low": 1, "close": 1}
                raise ConnectionError(f"socket closed {SECRET}")

            return generate()
        return [
            {"ts": "2024-01-03T08:30:00-06:00", "open": 1, "high": 1, "low": 1, "close": 9},
            {"ts": "2024-01-03T08:31:00-06:00", "open": 2, "high": 2, "low": 2, "close": 2},
        ]

    source = DatabentoSource("MES", client_factory=factory, sleep=lambda _seconds: None, max_retries=3)
    with caplog.at_level(logging.WARNING):
        bars = list(source.bars())
    assert [bar.close for bar in bars] == [1.0, 2.0]
    assert starts[1] == bars[0].ts
    assert SECRET not in caplog.text
    assert "ConnectionError" in caplog.text


def test_databento_errors_do_not_include_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setenv("DATABENTO_API_KEY", SECRET)
    if importlib.util.find_spec("databento") is None:
        with pytest.raises(MarketDataError) as real:
            _load_databento()
        assert SECRET not in str(real.value)
        assert "not installed" in str(real.value)

    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    with pytest.raises(MarketDataError) as missing_key:
        list(DatabentoSource("MES", start=ts_at(8, 30)).bars())
    assert SECRET not in str(missing_key.value)
    assert "DATABENTO_API_KEY" in str(missing_key.value)

    def unavailable() -> None:
        raise MarketDataError("databento package is not installed")

    monkeypatch.setenv("DATABENTO_API_KEY", SECRET)
    monkeypatch.setattr("orb_walk_forward.harness.ingest._load_databento", unavailable)
    with pytest.raises(MarketDataError) as missing_sdk:
        list(DatabentoSource("MES", start=ts_at(8, 30)).bars())
    assert SECRET not in str(missing_sdk.value)
    assert "not installed" in str(missing_sdk.value)


def test_ibkr_stub_refuses_orders_and_data() -> None:
    stub = IbkrMarketDataStub()
    with pytest.raises(NotImplementedError):
        stub.bars()
    with pytest.raises(NotImplementedError):
        stub.place_order("buy", 1)
    with pytest.raises(NotImplementedError):
        stub.submit_bracket(long_signal(), PAPER)
    with pytest.raises(NotImplementedError):
        stub.flatten_all()


def _write_csv(path: Path, bars: list[Bar]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow([bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])


def test_cli_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABENTO_API_KEY", SECRET)
    csv_path = tmp_path / "bars.csv"
    db_path = tmp_path / "run.sqlite"
    bars = opening_range() + [
        bar_at(8, 45, 100, 104, 99, 103),
        bar_at(8, 46, 103, 104, 100, 102),
        bar_at(8, 47, 102, 103, 101, 102.5),
    ]
    _write_csv(csv_path, bars)
    code = main(
        [
            "--replay",
            str(csv_path),
            "--or-minutes",
            "15",
            "--db",
            str(db_path),
            "--heartbeat-every",
            "9",
            "--seed",
            "11",
            "--symbol",
            "MES",
        ]
    )
    assert code == 0
    blob = db_path.read_bytes()
    assert SECRET.encode() not in blob
    conn = sqlite3.connect(db_path)
    assert conn.execute("select count(*) from bars").fetchone()[0] == len(bars)
    assert conn.execute("select count(*) from signals").fetchone()[0] == 1
    assert conn.execute("select count(*) from heartbeats").fetchone()[0] == 2
    detail = conn.execute("select detail from events where kind='config'").fetchone()[0]
    assert '"seed": 11' in detail
    assert '"point_value": 5.0' in detail or '"point_value": 5' in detail
    assert conn.execute("select count(*) from events where kind='shutdown'").fetchone()[0] == 1
    with pytest.raises(SystemExit) as bad_window:
        main(["--replay", str(csv_path), "--or-minutes", "10", "--db", str(tmp_path / "bad.sqlite")])
    assert bad_window.value.code == 2
    with pytest.raises(SystemExit):
        main(["--live", "--symbol", "MES"])
    with pytest.raises(SystemExit):
        main(["--replay", str(csv_path), "--symbol", "NQ", "--db", str(tmp_path / "nq.sqlite")])
