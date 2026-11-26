"""Paper sizing and bracket orders. There is no live broker.

Size::

    size = floor(equity * risk_pct / (stop_distance_points * point_value))

``point_value`` is a parameter (MES 5, ES 50). A result of 0, or a non-positive
input, submits nothing and the signal is skipped. ``equity`` is the configured
figure; fills do not mark it to market.

Stop and target
---------------
* ``stop_mode="or_opposite"`` (default): long stop at the OR low, short stop
  at the OR high. Distance is ``abs(signal_price - stop)``.
* ``stop_mode="fixed"``: stop is ``stop_points`` away from the anchor price.
  ``stop_points`` must be positive or the bracket is skipped.
* Target is ``target_r`` R away from the same anchor (default 1R).
* At submission the anchor is the signal price. Those prices are the
  intended stop and target. When the entry fills, the anchor becomes the
  fill price and the stop and target are rebuilt with the same parameters.
  The size is computed once, at submission. The fill recomputes the size
  the rebuilt distance would allow and records it. The filled quantity is
  not trimmed when that allowance is smaller.
* A long bracket and a short bracket in the same session are independent.
  Both may be open together. Neither side cancels or suppresses the other.

:class:`SimBroker` fill rules
-----------------------------
1. A bracket submitted on bar T cannot fill until a bar with a later start.
2. The entry fills at that later bar's open, for the full intended quantity.
   ``filled_qty`` stays 0 until then. Intended and filled quantities are both
   stored on the order.
3. After the entry fill, the same bar may exit. A long stop is hit when
   ``low <= stop``; a long target when ``high >= target``. Shorts mirror.
   If both are hit on one bar, the stop fills and the target is cancelled.
4. The exit fills at its price. If the bar opens through that price, the
   fill is the open.
5. ``flatten_all`` cancels every working order. An open position is closed
   with a flatten fill at the given price, or the last bar close. A bracket
   that has not filled is cancelled and never opens.

:class:`SimBroker` is the only broker implementation. IBKR order methods live
on :class:`~orb_walk_forward.harness.ingest.IbkrMarketDataStub` and raise
``NotImplementedError``.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .ingest import CHICAGO, Bar
from .signal import Signal


def size_contracts(
    equity: float,
    risk_pct: float,
    stop_distance_points: float,
    point_value: float,
) -> int:
    """Floor contract count. Zero means do not trade."""

    if min(equity, risk_pct, stop_distance_points, point_value) <= 0:
        return 0
    return math.floor(equity * risk_pct / (stop_distance_points * point_value))


@dataclass(frozen=True)
class PositionParams:
    """Frozen sizing and bracket parameters."""

    equity: float
    risk_pct: float
    point_value: float
    stop_mode: str = "or_opposite"
    stop_points: float | None = None
    target_r: float = 1.0

    def __post_init__(self) -> None:
        if self.stop_mode not in ("or_opposite", "fixed"):
            raise ValueError("stop_mode must be 'or_opposite' or 'fixed'")
        if self.target_r < 0:
            raise ValueError("target_r must be >= 0")


@dataclass
class Order:
    """Working, filled, or cancelled paper order. Mutable lifecycle."""

    order_id: int
    kind: str
    side: str
    intended_qty: int
    price: float | None
    status: str
    filled_qty: int
    ts: datetime


@dataclass(frozen=True)
class Fill:
    """One paper fill. ``price`` is the simulated execution price."""

    order_id: int
    ts: datetime
    side: str
    qty: int
    price: float
    kind: str


@dataclass(frozen=True)
class LevelUpdate:
    """Intended levels from the signal, and the levels rebuilt from the fill.

    ``size_trimmed`` is always false. The filled quantity stays at
    ``intended_qty`` even when ``risk_qty_at_fill`` is smaller.
    """

    ts: datetime
    side: str
    fill_price: float
    intended_stop: float
    intended_target: float
    rebuilt_stop: float
    rebuilt_target: float
    intended_distance: float
    rebuilt_distance: float
    intended_qty: int
    risk_qty_at_fill: int
    size_trimmed: bool
    stop_order_id: int
    target_order_id: int


@dataclass
class Bracket:
    """Entry plus protective stop and target for one signal.

    A session may hold one long bracket and one short bracket at the same
    time. Submitting one side does not touch the other.
    """

    side: str
    intended_qty: int
    entry: Order
    stop: Order
    target: Order
    submitted_ts: datetime
    state: str
    params: PositionParams
    or_high: float
    or_low: float
    intended_stop: float
    intended_target: float
    intended_distance: float
    rebuilt_stop: float | None = None
    rebuilt_target: float | None = None


class Broker(Protocol):
    """Order interface. The only concrete broker is :class:`SimBroker`."""

    def submit_bracket(self, signal: Signal, params: PositionParams) -> Sequence[Order]:
        """Submit a paper bracket, or nothing when size is 0."""

    def on_bar(self, bar: Bar) -> Sequence[Fill]:
        """Advance working brackets. May fill on this bar."""

    def flatten_all(self, price: float | None = None) -> Sequence[Fill]:
        """Cancel working orders and close any open paper position."""


class SimBroker:
    """Next-bar paper broker. See the module docstring for fill rules."""

    def __init__(self) -> None:
        self.brackets: list[Bracket] = []
        self.level_updates: list[LevelUpdate] = []
        self._orders: dict[int, Order] = {}
        self._ids = itertools.count(1)
        self.last_ts: datetime | None = None
        self.last_price: float | None = None

    def order(self, order_id: int) -> Order:
        return self._orders[order_id]

    def all_orders(self) -> list[Order]:
        return [self._orders[key] for key in sorted(self._orders)]

    def position_qty(self) -> int:
        """Signed open contracts. Long is positive. Flat is 0."""

        net = 0
        for bracket in self.brackets:
            if bracket.state == "open":
                sign = 1 if bracket.side == "long" else -1
                net += sign * bracket.entry.filled_qty
        return net

    def submit_bracket(self, signal: Signal, params: PositionParams) -> list[Order]:
        levels = _levels(signal.side, signal.price, params, signal.or_high, signal.or_low)
        if levels is None:
            return []
        stop_px, target_px, distance = levels
        qty = size_contracts(params.equity, params.risk_pct, distance, params.point_value)
        if qty <= 0:
            return []
        if signal.side == "long":
            entry_side, exit_side = "buy", "sell"
        else:
            entry_side, exit_side = "sell", "buy"
        entry = self._make("entry", entry_side, qty, None, signal.ts)
        stop = self._make("stop", exit_side, qty, stop_px, signal.ts)
        target = self._make("target", exit_side, qty, target_px, signal.ts)
        self.brackets.append(
            Bracket(
                signal.side,
                qty,
                entry,
                stop,
                target,
                signal.ts,
                "pending",
                params,
                signal.or_high,
                signal.or_low,
                stop_px,
                target_px,
                distance,
            )
        )
        return [entry, stop, target]

    def on_bar(self, bar: Bar) -> list[Fill]:
        self.last_ts = bar.ts
        self.last_price = bar.close
        fills: list[Fill] = []
        for bracket in self.brackets:
            if bracket.state == "pending" and bar.ts > bracket.submitted_ts:
                fills.append(self._apply_fill(bracket.entry, bar.ts, bar.open, bracket.intended_qty))
                bracket.state = "open"
                self._rebuild_from_fill(bracket, bar)
            if bracket.state == "open":
                exit_fill = self._exit_fill(bracket, bar)
                if exit_fill is not None:
                    fills.append(exit_fill)
        return fills

    def flatten_all(self, price: float | None = None, ts: datetime | None = None) -> list[Fill]:
        if ts is not None:
            self.last_ts = ts
        if price is not None:
            self.last_price = price
        px = self.last_price if price is None else price
        ts = self.last_ts or datetime.now(CHICAGO)
        fills: list[Fill] = []
        for bracket in self.brackets:
            if bracket.state == "pending":
                for order in (bracket.entry, bracket.stop, bracket.target):
                    order.status = "cancelled"
                    order.ts = ts
                bracket.state = "cancelled"
            elif bracket.state == "open":
                for order in (bracket.stop, bracket.target):
                    if order.status == "working":
                        order.status = "cancelled"
                        order.ts = ts
                qty = bracket.entry.filled_qty
                if qty > 0 and px is None:
                    raise ValueError("flatten_all requires a price when a position is open")
                if qty > 0 and px is not None:
                    side = "sell" if bracket.side == "long" else "buy"
                    order = self._make("flatten", side, qty, px, ts)
                    fills.append(self._apply_fill(order, ts, px, qty))
                bracket.state = "closed"
        return fills

    def _rebuild_from_fill(self, bracket: Bracket, bar: Bar) -> None:
        """Move stop and target onto the fill. Do not change the quantity."""

        levels = _levels(
            bracket.side,
            bar.open,
            bracket.params,
            bracket.or_high,
            bracket.or_low,
        )
        assert levels is not None
        stop_px, target_px, distance = levels
        bracket.rebuilt_stop = stop_px
        bracket.rebuilt_target = target_px
        if bracket.stop.status == "working":
            bracket.stop.price = stop_px
            bracket.stop.ts = bar.ts
        if bracket.target.status == "working":
            bracket.target.price = target_px
            bracket.target.ts = bar.ts
        allowed = size_contracts(
            bracket.params.equity,
            bracket.params.risk_pct,
            distance,
            bracket.params.point_value,
        )
        self.level_updates.append(
            LevelUpdate(
                bar.ts,
                bracket.side,
                bar.open,
                bracket.intended_stop,
                bracket.intended_target,
                stop_px,
                target_px,
                bracket.intended_distance,
                distance,
                bracket.intended_qty,
                allowed,
                False,
                bracket.stop.order_id,
                bracket.target.order_id,
            )
        )

    def _exit_fill(self, bracket: Bracket, bar: Bar) -> Fill | None:
        long = bracket.side == "long"
        stop_px = bracket.stop.price
        target_px = bracket.target.price
        assert stop_px is not None and target_px is not None
        stop_hit = bar.low <= stop_px if long else bar.high >= stop_px
        target_hit = bar.high >= target_px if long else bar.low <= target_px
        if stop_hit:
            # Stop wins when both levels trade on the same bar.
            through = (long and bar.open < stop_px) or ((not long) and bar.open > stop_px)
            price = bar.open if through else stop_px
            return self._close(bracket, bracket.stop, bracket.target, bar.ts, price)
        if target_hit:
            through = (long and bar.open > target_px) or ((not long) and bar.open < target_px)
            price = bar.open if through else target_px
            return self._close(bracket, bracket.target, bracket.stop, bar.ts, price)
        return None

    def _close(
        self,
        bracket: Bracket,
        order: Order,
        other: Order,
        ts: datetime,
        price: float,
    ) -> Fill:
        fill = self._apply_fill(order, ts, price, bracket.entry.filled_qty)
        if other.status == "working":
            other.status = "cancelled"
            other.ts = ts
        bracket.state = "closed"
        return fill

    def _apply_fill(self, order: Order, ts: datetime, price: float, qty: int) -> Fill:
        order.status = "filled"
        order.filled_qty = qty
        order.ts = ts
        if order.price is None:
            order.price = price
        return Fill(order.order_id, ts, order.side, qty, price, order.kind)

    def _make(
        self,
        kind: str,
        side: str,
        qty: int,
        price: float | None,
        ts: datetime,
    ) -> Order:
        order = Order(next(self._ids), kind, side, qty, price, "working", 0, ts)
        self._orders[order.order_id] = order
        return order


def _levels(
    side: str,
    anchor: float,
    params: PositionParams,
    or_high: float,
    or_low: float,
) -> tuple[float, float, float] | None:
    """Stop, target, and distance for one anchor price.

    ``or_opposite`` keeps the stop on the far side of the opening range.
    ``fixed`` places the stop ``stop_points`` away from ``anchor``. The
    target is ``target_r`` times that distance from the same anchor.
    """

    if params.stop_mode == "or_opposite":
        stop = or_low if side == "long" else or_high
    elif params.stop_points is None or params.stop_points <= 0:
        return None
    elif side == "long":
        stop = anchor - params.stop_points
    else:
        stop = anchor + params.stop_points
    distance = abs(anchor - stop)
    if side == "long":
        target = anchor + params.target_r * distance
    else:
        target = anchor - params.target_r * distance
    return stop, target, distance
