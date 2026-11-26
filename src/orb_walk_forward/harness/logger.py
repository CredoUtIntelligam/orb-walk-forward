"""SQLite audit log for one paper run.

Tables (append-only): ``bars``, ``signals``, ``orders``, ``fills``,
``heartbeats``, and ``events``. Every row carries ``run_id`` plus the event
timestamp and ``logged_at``. ``events.kind`` is ``config``, ``gap``,
``bracket_rebuild``, ``cash_close``, or ``shutdown``. A gap is an event, not
a filled bar. ``bracket_rebuild`` stores the intended stop and target from
the signal price and the levels rebuilt from the entry fill. Size is not
trimmed.

``log_config`` writes the parameter JSON, the git SHA when ``git`` can see
one, and the seed. Keys whose names contain ``key``, ``secret``, ``token``,
or ``password`` are dropped before the JSON is stored. The Databento key is
never passed in. The seed is recorded even though this skeleton does not
draw random numbers, so a later stochastic fill model can reuse the same id.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .ingest import CHICAGO, Bar
from .position import Fill, LevelUpdate, Order
from .signal import Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    session TEXT NOT NULL,
    side TEXT NOT NULL,
    or_high REAL NOT NULL,
    or_low REAL NOT NULL,
    price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    side TEXT NOT NULL,
    intended_qty INTEGER NOT NULL,
    filled_qty INTEGER NOT NULL,
    price REAL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    bar_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

_SECRET_WORDS = ("key", "secret", "token", "password")


def git_sha() -> str | None:
    """Return HEAD if this directory is in a git checkout, else ``None``."""

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.strip()
    return sha or None


def _iso(ts: datetime) -> str:
    return ts.isoformat()


class RunLogger:
    """One SQLite file, one ``run_id``."""

    def __init__(self, path: str | Path, run_id: str | None = None) -> None:
        self.path = str(path)
        self.run_id = run_id or uuid.uuid4().hex
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._closed = False

    def log_config(self, params: Mapping[str, Any], seed: int | None) -> None:
        safe = {
            key: value
            for key, value in params.items()
            if not any(word in key.lower() for word in _SECRET_WORDS)
        }
        detail = json.dumps(
            {"params": safe, "git_sha": git_sha(), "seed": seed, "schema_version": 1},
            sort_keys=True,
            default=str,
        )
        self.event(datetime.now(CHICAGO), "config", detail)

    def log_bar(self, bar: Bar) -> None:
        self._insert(
            "bars",
            {
                "ts": _iso(bar.ts),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            },
        )

    def log_signal(self, signal: Signal) -> None:
        self._insert(
            "signals",
            {
                "ts": _iso(signal.ts),
                "session": signal.session.isoformat(),
                "side": signal.side,
                "or_high": signal.or_high,
                "or_low": signal.or_low,
                "price": signal.price,
            },
        )

    def log_order(self, order: Order) -> None:
        self._insert(
            "orders",
            {
                "ts": _iso(order.ts),
                "order_id": order.order_id,
                "kind": order.kind,
                "side": order.side,
                "intended_qty": order.intended_qty,
                "filled_qty": order.filled_qty,
                "price": order.price,
                "status": order.status,
            },
        )

    def log_fill(self, fill: Fill) -> None:
        self._insert(
            "fills",
            {
                "ts": _iso(fill.ts),
                "order_id": fill.order_id,
                "kind": fill.kind,
                "side": fill.side,
                "qty": fill.qty,
                "price": fill.price,
            },
        )

    def heartbeat(self, ts: datetime, bar_count: int) -> None:
        self._insert("heartbeats", {"ts": _iso(ts), "bar_count": bar_count})

    def log_level_update(self, update: LevelUpdate) -> None:
        detail = json.dumps(
            {
                "fill_price": update.fill_price,
                "intended_distance": update.intended_distance,
                "intended_qty": update.intended_qty,
                "intended_stop": update.intended_stop,
                "intended_target": update.intended_target,
                "rebuilt_distance": update.rebuilt_distance,
                "rebuilt_stop": update.rebuilt_stop,
                "rebuilt_target": update.rebuilt_target,
                "risk_qty_at_fill": update.risk_qty_at_fill,
                "side": update.side,
                "size_trimmed": update.size_trimmed,
            },
            sort_keys=True,
        )
        self.event(update.ts, "bracket_rebuild", detail)

    def log_gap(self, ts: datetime, start: datetime, end: datetime) -> None:
        detail = json.dumps({"from": start.isoformat(), "to": end.isoformat()})
        self.event(ts, "gap", detail)

    def event(self, ts: datetime, kind: str, detail: str) -> None:
        self._insert("events", {"ts": _iso(ts), "kind": kind, "detail": detail})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._conn.commit()
        self._conn.close()

    def _insert(self, table: str, columns: Mapping[str, Any]) -> None:
        if self._closed:
            return
        row = {"run_id": self.run_id, "logged_at": _iso(datetime.now(CHICAGO)), **dict(columns)}
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})",
            tuple(row.values()),
        )
        self._conn.commit()
