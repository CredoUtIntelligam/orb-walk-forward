"""Opening-range breakout with a required retest. The spec is frozen.

Assumptions
-----------
* One session is one America/Chicago calendar date. Bars before the RTH
  open (08:30 CT, equal to 09:30 America/New_York) are ignored. This
  skeleton does not model the Sunday reopen. The paper runner, not this
  tracker, flattens open positions at the 15:00 CT cash close.
* The opening range is the high and low of the observed one-minute bars
  whose start lies in ``[08:30, 08:30 + or_minutes)``. That is the first
  15 or 30 minutes of RTH, including the 08:30 bar. ``or_minutes`` must
  be 15 or 30.
* A missing minute is not filled and does not invalidate the session. The
  range is the high and low of the bars that actually arrived. It completes
  when ``or_minutes`` in-window bars have been seen, or when the first bar
  at/after the window end arrives, whichever comes first. If the window
  contained no bars, the session produces no signal. The runner logs each
  gap as a warning and as an ``events`` row with kind ``gap``. Once the
  range is complete it does not change; a late in-window bar is ignored.
* A duplicate timestamp is ignored, so a replay cannot widen the range.

Trigger
-------
After the range is complete, long side:

1. A bar closes strictly above the OR high. That is the break. It is never
   itself a signal, even if its low trades back through the OR high.
2. A later bar has low at or below the OR high (the pullback) and closes
   strictly back above it. That close is the long signal. The signal price
   is that close.

The short side is the mirror: a close strictly below the OR low, then a
later bar whose high touches the OR low and whose close is back below it.

At most one signal per side per session. The sides do not suppress each
other: one session may emit one long and one short, and both brackets may
be open at the same time. Nothing here optimizes or filters. There is no
signal on the forming range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from .ingest import CHICAGO, Bar, as_chicago

Side = Literal["long", "short"]
RTH_OPEN = time(8, 30)
CASH_CLOSE = time(15, 0)


@dataclass(frozen=True)
class SignalParams:
    """Frozen signal parameters. Only ``or_minutes`` is configurable."""

    or_minutes: int = 30

    def __post_init__(self) -> None:
        if type(self.or_minutes) is not int or self.or_minutes not in (15, 30):
            raise ValueError("or_minutes must be 15 or 30")


@dataclass(frozen=True)
class Signal:
    """One frozen break-and-retest signal. ``ts`` is the trigger bar start."""

    session: date
    side: Side
    ts: datetime
    or_high: float
    or_low: float
    price: float


class OpeningRangeTracker:
    """Consume bars in release order and emit at most one signal per side."""

    def __init__(self, params: SignalParams, rth_open: time = RTH_OPEN) -> None:
        self.params = params
        self.rth_open = rth_open
        self._seen: set[datetime] = set()
        self._session: date | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._count = 0
        self._complete = False
        self._long_broken = False
        self._short_broken = False
        self._long_done = False
        self._short_done = False

    def on_bar(self, bar: Bar) -> Signal | None:
        if bar.high < bar.low:
            raise ValueError("high < low")
        ts = as_chicago(bar.ts)
        if ts in self._seen:
            return None
        self._seen.add(ts)
        session = ts.date()
        if session != self._session:
            self._reset(session)
        open_dt = datetime.combine(session, self.rth_open, tzinfo=CHICAGO)
        end_dt = open_dt + timedelta(minutes=self.params.or_minutes)
        if ts < open_dt:
            return None
        if ts < end_dt:
            if self._complete:
                return None
            self._high = bar.high if self._high is None else max(self._high, bar.high)
            self._low = bar.low if self._low is None else min(self._low, bar.low)
            self._count += 1
            if self._count >= self.params.or_minutes:
                self._complete = True
            return None
        if not self._complete:
            self._complete = True
            if self._high is None or self._low is None:
                return None
        return self._evaluate(bar, session)

    def _reset(self, session: date) -> None:
        self._session = session
        self._high = None
        self._low = None
        self._count = 0
        self._complete = False
        self._long_broken = False
        self._short_broken = False
        self._long_done = False
        self._short_done = False

    def _evaluate(self, bar: Bar, session: date) -> Signal | None:
        assert self._high is not None and self._low is not None
        return self._long(bar, session) or self._short(bar, session)

    def _long(self, bar: Bar, session: date) -> Signal | None:
        assert self._high is not None and self._low is not None
        if self._long_done:
            return None
        if not self._long_broken:
            if bar.close > self._high:
                self._long_broken = True
            return None
        if bar.low <= self._high and bar.close > self._high:
            self._long_done = True
            return Signal(session, "long", as_chicago(bar.ts), self._high, self._low, bar.close)
        return None

    def _short(self, bar: Bar, session: date) -> Signal | None:
        assert self._high is not None and self._low is not None
        if self._short_done:
            return None
        if not self._short_broken:
            if bar.close < self._low:
                self._short_broken = True
            return None
        if bar.high >= self._low and bar.close < self._low:
            self._short_done = True
            return Signal(session, "short", as_chicago(bar.ts), self._high, self._low, bar.close)
        return None
