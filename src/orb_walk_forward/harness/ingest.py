"""Bar ingest for the paper ORB harness.

Assumptions
-----------
* CME equity-index futures (MES/ES) are stamped in America/Chicago.
  The RTH open is 08:30 CT, which is 09:30 America/New_York.
* Databento ``ohlcv-1m`` ``ts_event`` is the bar **start**, not the close.
* Inputs are one-minute bars. A hole longer than one minute is a gap: it is
  logged (a warning here, and an ``events`` row from the runner) and never
  forward-filled. A gap inside the opening window does not invalidate that
  session. The signal uses the bars that arrived.
* The first sighting of a timestamp wins. A reconnect or replay that resends
  a bar cannot double-count it. A bar that arrives after a later timestamp
  was already released is stored for audit but is not released again, so it
  cannot revise an opening range that has moved on.
* Naive timestamps are interpreted as America/Chicago. Aware timestamps are
  converted to that zone.
* ``DATABENTO_API_KEY`` is read from the environment inside the default
  client factory and is never stored on the instance or written to a log.
  Disconnects retry with exponential backoff from the last released
  timestamp. A missing key or missing SDK fails immediately.

Parameters
----------
``symbol``, ``dataset`` (default ``GLBX.MDP3``), ``schema`` (``ohlcv-1m``),
``stype_in`` (default ``raw_symbol``; this is the Databento symbology used
for a specific contract, not a continuous parent), ``start``, ``end``,
``live``, ``max_backoff_s``, ``max_retries`` (``None`` retries until the
process stops).
"""

from __future__ import annotations

import csv
import logging
import math
import os
import time
from bisect import insort
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
MINUTE = timedelta(minutes=1)
_LOG = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    """Configuration problem. The reconnect loop does not retry these."""


@dataclass(frozen=True)
class Bar:
    """One one-minute bar. ``ts`` is the bar start in America/Chicago."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BarSource(Protocol):
    """Anything that yields bars. No source in this package can send orders."""

    def bars(self) -> Iterator[Bar]:
        """Yield bars in the source's natural order."""


def as_chicago(ts: datetime) -> datetime:
    """Normalize ``ts`` to America/Chicago. Naive values are already CT."""

    if ts.tzinfo is None:
        return ts.replace(tzinfo=CHICAGO)
    return ts.astimezone(CHICAGO)


def parse_ts(value: Any) -> datetime:
    """Parse an ISO string, ``datetime``, or epoch (s/ms/us/ns) into CT."""

    if isinstance(value, datetime):
        return as_chicago(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e17:
            seconds = number / 1e9
        elif number > 1e14:
            seconds = number / 1e6
        elif number > 1e11:
            seconds = number / 1e3
        else:
            seconds = number
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(CHICAGO)
    text = str(value).strip()
    if not text:
        raise ValueError("missing timestamp")
    return as_chicago(datetime.fromisoformat(text.replace("Z", "+00:00")))


def row_to_bar(row: Mapping[str, Any]) -> Bar:
    """Build a bar from a CSV row or DataFrame record."""

    lowered = {str(key).strip().lower(): value for key, value in row.items()}

    def take(*names: str) -> Any:
        for name in names:
            if name in lowered and lowered[name] not in (None, ""):
                return lowered[name]
        raise KeyError(names[0])

    ts = parse_ts(take("ts", "timestamp", "ts_event", "time"))
    prices = [float(take(name)) for name in ("open", "high", "low", "close")]
    raw_volume = lowered.get("volume")
    volume = 0.0 if raw_volume in (None, "") else float(raw_volume)
    open_, high, low, close = prices
    if not all(math.isfinite(value) for value in (open_, high, low, close, volume)):
        raise ValueError(f"non-finite bar at {ts.isoformat()}")
    if high < low:
        raise ValueError(f"high < low at {ts.isoformat()}")
    return Bar(ts, open_, high, low, close, volume)


def _with_chicago_ts(bar: Bar) -> Bar:
    """Return ``bar`` with an America/Chicago start. Naive timestamps are CT."""

    if getattr(bar.ts.tzinfo, "key", None) == "America/Chicago":
        return bar
    return Bar(as_chicago(bar.ts), bar.open, bar.high, bar.low, bar.close, bar.volume)


def bar_from_record(record: Any) -> Bar:
    """Accept a :class:`Bar`, a mapping, or a Databento record."""

    if isinstance(record, Bar):
        return _with_chicago_ts(record)
    if isinstance(record, Mapping):
        return row_to_bar(record)
    return row_to_bar(
        {
            "ts_event": getattr(record, "ts_event"),
            "open": getattr(record, "open"),
            "high": getattr(record, "high"),
            "low": getattr(record, "low"),
            "close": getattr(record, "close"),
            "volume": getattr(record, "volume", 0),
        }
    )


def _frame_rows(frame: Any) -> list[Mapping[str, Any]]:
    if hasattr(frame, "reset_index") and hasattr(frame, "to_dict"):
        return list(frame.reset_index().to_dict("records"))
    return list(frame)


class ReplaySource:
    """Replay bars from a CSV file or a DataFrame-like object. No network."""

    def __init__(self, path: str | Path | None = None, frame: Any = None) -> None:
        if (path is None) == (frame is None):
            raise ValueError("pass exactly one of path or frame")
        self.path = Path(path) if path is not None else None
        self.frame = frame

    def bars(self) -> Iterator[Bar]:
        if self.path is not None:
            with self.path.open(newline="") as handle:
                yield from (row_to_bar(row) for row in csv.DictReader(handle))
            return
        yield from (row_to_bar(row) for row in _frame_rows(self.frame))


def _load_databento() -> Any:
    try:
        import databento as db
    except ImportError as exc:
        raise MarketDataError("databento package is not installed") from exc
    return db


class DatabentoSource:
    """Live or historical ``ohlcv-1m`` via the optional ``databento`` SDK.

    Symbology defaults to ``raw_symbol`` (one outright contract). Pass
    ``stype_in`` to override it. ``client_factory`` and ``sleep`` exist so
    tests can simulate a disconnect without the SDK and without sleeping.
    The factory receives the resume timestamp (the last released bar start,
    or the configured start).
    """

    def __init__(
        self,
        symbol: str,
        *,
        dataset: str = "GLBX.MDP3",
        schema: str = "ohlcv-1m",
        stype_in: str = "raw_symbol",
        start: datetime | None = None,
        end: datetime | None = None,
        live: bool = False,
        max_backoff_s: float = 30.0,
        max_retries: int | None = None,
        client_factory: Callable[[datetime | None], Iterable[Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.symbol = symbol
        self.dataset = dataset
        self.schema = schema
        self.stype_in = stype_in
        self.start = as_chicago(start) if start is not None else None
        self.end = as_chicago(end) if end is not None else None
        self.live = live
        self.max_backoff_s = max_backoff_s
        self.max_retries = max_retries
        self._client_factory = client_factory
        self._sleep = sleep
        self._last_ts: datetime | None = None

    def bars(self) -> Iterator[Bar]:
        backoff = 1.0
        attempts = 0
        while True:
            try:
                yield from self._stream(self._last_ts or self.start)
                return
            except (KeyboardInterrupt, SystemExit, MarketDataError):
                raise
            except Exception as exc:
                attempts += 1
                # Log the exception type only. SDK messages can echo the key.
                _LOG.warning(
                    "market-data disconnect (%s); reconnecting in %.1fs",
                    type(exc).__name__,
                    backoff,
                )
                if self.max_retries is not None and attempts > self.max_retries:
                    raise
                self._sleep(backoff)
                backoff = min(self.max_backoff_s, backoff * 2)

    def _stream(self, start: datetime | None) -> Iterator[Bar]:
        factory = self._client_factory or self._default_factory
        for record in factory(start):
            bar = bar_from_record(record)
            if self._last_ts is not None and bar.ts <= self._last_ts:
                continue
            self._last_ts = bar.ts
            yield bar

    def _default_factory(self, start: datetime | None) -> Iterable[Any]:
        key = os.environ.get("DATABENTO_API_KEY")
        if not key:
            raise MarketDataError("DATABENTO_API_KEY is not set")
        db = _load_databento()
        if self.live:
            live = db.Live(key=key)
            kwargs: dict[str, Any] = {
                "dataset": self.dataset,
                "schema": self.schema,
                "symbols": self.symbol,
                "stype_in": self.stype_in,
            }
            if start is not None:
                kwargs["start"] = start.isoformat()
            live.subscribe(**kwargs)
            if hasattr(live, "start"):
                live.start()
            return live
        if start is None:
            raise MarketDataError("Databento historical replay requires start")
        hist = db.Historical(key=key)
        kwargs = {
            "dataset": self.dataset,
            "schema": self.schema,
            "symbols": self.symbol,
            "stype_in": self.stype_in,
            "start": start.isoformat(),
        }
        if self.end is not None:
            kwargs["end"] = self.end.isoformat()
        store = hist.timeseries.get_range(**kwargs)
        frame = store.to_df() if hasattr(store, "to_df") else store
        return ReplaySource(frame=frame).bars()


class IbkrMarketDataStub:
    """Read-only IBKR placeholder.

    Market data is not wired. Every order method raises ``NotImplementedError``.
    This harness has no path that submits a live order.
    """

    def bars(self) -> Iterator[Bar]:
        raise NotImplementedError(
            "IBKR market data is not implemented; use ReplaySource or DatabentoSource"
        )

    def place_order(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("IBKR order routing is disabled")

    def submit_bracket(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("IBKR order routing is disabled")

    def flatten_all(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("IBKR order routing is disabled")


class BarBuffer:
    """Dedupe by timestamp, keep bars ordered, and record gaps.

    ``add`` returns ``(status, bar, gap)``. ``status`` is ``released`` (hand
    this bar to the strategy), ``late`` (stored only), or ``duplicate``
    (ignored). ``gap`` is ``(first_missing_minute, arrived_bar_start)`` when
    the released bar jumps more than one minute.
    """

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.gaps: list[tuple[datetime, datetime]] = []
        self._seen: set[datetime] = set()
        self._watermark: datetime | None = None

    def add(self, bar: Bar) -> tuple[str, Bar, tuple[datetime, datetime] | None]:
        bar = _with_chicago_ts(bar)
        if bar.ts in self._seen:
            return "duplicate", bar, None
        self._seen.add(bar.ts)
        insort(self.bars, bar, key=lambda item: item.ts)
        if self._watermark is not None and bar.ts < self._watermark:
            return "late", bar, None
        gap: tuple[datetime, datetime] | None = None
        if self._watermark is not None and bar.ts > self._watermark + MINUTE:
            gap = (self._watermark + MINUTE, bar.ts)
            self.gaps.append(gap)
            _LOG.warning(
                "bar gap %s -> %s (missing minute, not forward-filled)",
                gap[0].isoformat(),
                gap[1].isoformat(),
            )
        self._watermark = bar.ts
        return "released", bar, gap
