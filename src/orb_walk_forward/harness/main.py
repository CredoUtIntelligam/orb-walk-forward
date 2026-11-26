"""Paper runner. Ingest, buffer, signal, size, and log. No live orders.

Loop, per released bar: log the bar, let the sim broker work, then evaluate
the signal and submit a bracket if one fires. A bracket never fills on its
own signal bar (the broker waits for a later start).

A heartbeat is an info log plus a ``heartbeats`` row every ``heartbeat_every``
released bars, and whenever ``heartbeat_seconds`` of wall time has passed at
a bar boundary. A silent socket does not heartbeat until the next bar or a
disconnect warning.

Shutdown is idempotent. SIGINT and SIGTERM raise ``KeyboardInterrupt`` so
``finally`` can run; ``atexit`` is the backstop. Both call ``flatten_all``
and close the database, so a paper position is not left working.

Example::

    PYTHONPATH=src python -m orb_walk_forward.harness.main \\
        --replay bars.csv --or-minutes 30 --db run.sqlite
"""

from __future__ import annotations

import atexit
import logging
import signal as os_signal
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .ingest import CHICAGO, BarSource, BarBuffer, DatabentoSource, ReplaySource, parse_ts
from .logger import RunLogger
from .position import PositionParams, SimBroker
from .signal import OpeningRangeTracker, SignalParams

_LOG = logging.getLogger(__name__)


def infer_point_value(symbol: str) -> float:
    """MES is 5, ES is 50. Anything else must pass ``--point-value``."""

    root = symbol.upper().split(".")[0]
    if root.startswith("MES"):
        return 5.0
    if root.startswith("ES"):
        return 50.0
    raise ValueError(f"pass --point-value for {symbol} (MES=5, ES=50)")


def run_loop(
    source: BarSource,
    signal_params: SignalParams,
    position_params: PositionParams,
    run_log: RunLogger,
    broker: SimBroker | None = None,
    *,
    heartbeat_every: int = 60,
    heartbeat_seconds: float | None = None,
    install_shutdown: bool = True,
    config: Mapping[str, Any] | None = None,
    seed: int | None = None,
    clock: Callable[[], float] | None = None,
) -> SimBroker:
    """Run until the source ends or the process is asked to stop."""

    broker = broker or SimBroker()
    buffer = BarBuffer()
    tracker = OpeningRangeTracker(signal_params)
    clock = clock or time.monotonic
    done = {"flag": False}
    previous: dict[int, Any] = {}
    run_log.log_config(dict(config or {}), seed)
    last_hb = clock()
    count = 0

    def shutdown() -> None:
        if done["flag"]:
            return
        done["flag"] = True
        try:
            for fill in broker.flatten_all():
                run_log.log_fill(fill)
                run_log.log_order(broker.order(fill.order_id))
            for order in broker.all_orders():
                run_log.log_order(order)
            stamp = broker.last_ts or datetime.now(CHICAGO)
            run_log.event(stamp, "shutdown", "flatten_all")
            _LOG.info("shutdown flatten_all")
        finally:
            run_log.close()

    if install_shutdown and threading.current_thread() is threading.main_thread():

        def _handle(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        for signum in (os_signal.SIGINT, os_signal.SIGTERM):
            previous[signum] = os_signal.getsignal(signum)
            os_signal.signal(signum, _handle)
        atexit.register(shutdown)

    try:
        try:
            for raw in source.bars():
                status, fresh, gap = buffer.add(raw)
                if status == "duplicate":
                    continue
                if gap is not None:
                    run_log.log_gap(fresh.ts, gap[0], gap[1])
                run_log.log_bar(fresh)
                if status != "released":
                    continue
                seen_updates = len(broker.level_updates)
                for fill in broker.on_bar(fresh):
                    run_log.log_fill(fill)
                    run_log.log_order(broker.order(fill.order_id))
                for update in broker.level_updates[seen_updates:]:
                    run_log.log_level_update(update)
                    run_log.log_order(broker.order(update.stop_order_id))
                    run_log.log_order(broker.order(update.target_order_id))
                signal = tracker.on_bar(fresh)
                if signal is not None:
                    _LOG.info("signal %s %s", signal.side, signal.ts.isoformat())
                    run_log.log_signal(signal)
                    for order in broker.submit_bracket(signal, position_params):
                        run_log.log_order(order)
                count += 1
                now = clock()
                bar_due = heartbeat_every > 0 and count % heartbeat_every == 0
                time_due = heartbeat_seconds is not None and now - last_hb >= heartbeat_seconds
                if bar_due or time_due:
                    _LOG.info("heartbeat bars=%s", count)
                    run_log.heartbeat(fresh.ts, count)
                    last_hb = now
        except KeyboardInterrupt:
            return broker
    finally:
        try:
            shutdown()
        finally:
            if previous:
                atexit.unregister(shutdown)
                for signum, handler in previous.items():
                    os_signal.signal(signum, handler)
    return broker


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Replay is the default path; Databento stays paper-only."""

    import argparse

    parser = argparse.ArgumentParser(
        description="Paper ORB harness. Simulation only; no live orders."
    )
    parser.add_argument("--replay", type=str, default=None, help="CSV of 1-minute bars")
    parser.add_argument("--databento", action="store_true", help="Read ohlcv-1m (still paper)")
    parser.add_argument("--live", action="store_true", help="Databento live data; orders stay simulated")
    parser.add_argument("--symbol", default="MES")
    parser.add_argument("--dataset", default="GLBX.MDP3")
    parser.add_argument("--stype-in", default="raw_symbol")
    parser.add_argument("--start", default=None, help="ISO start for a Databento historical read")
    parser.add_argument("--or-minutes", type=int, default=30)
    parser.add_argument("--db", default="run.sqlite")
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.005)
    parser.add_argument("--point-value", type=float, default=None)
    parser.add_argument("--stop-mode", choices=("or_opposite", "fixed"), default="or_opposite")
    parser.add_argument("--stop-points", type=float, default=None)
    parser.add_argument("--target-r", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--heartbeat-every", type=int, default=60)
    parser.add_argument("--heartbeat-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    if args.replay and args.databento:
        parser.error("pass only one of --replay and --databento")
    if args.live and not args.databento:
        parser.error("--live requires --databento")
    if not args.replay and not args.databento:
        parser.error("pass --replay FILE or --databento")
    try:
        signal_params = SignalParams(or_minutes=args.or_minutes)
        point_value = (
            args.point_value if args.point_value is not None else infer_point_value(args.symbol)
        )
        position_params = PositionParams(
            equity=args.equity,
            risk_pct=args.risk_pct,
            point_value=point_value,
            stop_mode=args.stop_mode,
            stop_points=args.stop_points,
            target_r=args.target_r,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.replay:
        source: BarSource = ReplaySource(path=args.replay)
    else:
        source = DatabentoSource(
            args.symbol,
            dataset=args.dataset,
            stype_in=args.stype_in,
            start=parse_ts(args.start) if args.start else None,
            live=args.live,
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = {
        "symbol": args.symbol,
        "or_minutes": args.or_minutes,
        "equity": args.equity,
        "risk_pct": args.risk_pct,
        "point_value": point_value,
        "stop_mode": args.stop_mode,
        "stop_points": args.stop_points,
        "target_r": args.target_r,
        "replay": args.replay,
        "databento": bool(args.databento),
        "live": bool(args.live),
        "dataset": args.dataset,
        "heartbeat_every": args.heartbeat_every,
    }
    run_log = RunLogger(args.db)
    try:
        run_loop(
            source,
            signal_params,
            position_params,
            run_log,
            heartbeat_every=args.heartbeat_every,
            heartbeat_seconds=args.heartbeat_seconds,
            config=config,
            seed=args.seed,
        )
    finally:
        run_log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
