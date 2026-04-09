"""
Shared in-memory bot state.

BotState is the single source of truth for the current runtime state.
All components read/write through the async accessor methods which hold
an asyncio.Lock to prevent data races.

The dashboard reads a copy via snapshot() — bot logic is never blocked
by dashboard reads.
"""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.models import (
    ActiveQuote,
    BotEvent,
    BotStatus,
    Fill,
    KillReason,
    MarketSnapshot,
    Order,
    PnLState,
    Position,
    RiskMetrics,
)

_MAX_FILLS_IN_MEMORY = 500
_MAX_EVENTS_IN_MEMORY = 200
_MAX_RECENT_ORDERS_IN_MEMORY = 200


class SymbolState:
    """Per-symbol runtime state."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.market: Optional[MarketSnapshot] = None
        self.active_quote: Optional[ActiveQuote] = None
        self.position: Optional[Position] = None
        # baseline spread for abnormal detection (set on first good tick)
        self.baseline_spread_bps: Optional[Decimal] = None


class BotState:
    """
    Central in-memory state store.

    Thread-safety: all public methods hold self._lock (asyncio.Lock).
    The dashboard uses snapshot() to get a deep copy without holding the lock.
    """

    def __init__(self, symbols: list[str]) -> None:
        self._lock = asyncio.Lock()

        self.status: BotStatus = BotStatus.STARTING
        self.started_at: Optional[datetime] = None
        self.last_heartbeat: Optional[datetime] = None

        # Kill switch
        self.kill_switch_active: bool = False
        self.kill_switch_reason: Optional[KillReason] = None
        self.kill_switch_triggered_at: Optional[datetime] = None

        # Per-symbol state
        self.symbols: dict[str, SymbolState] = {
            sym: SymbolState(sym) for sym in symbols
        }

        # Orders: open + recently closed
        self.open_orders: dict[str, Order] = {}        # cloid hex → Order
        self.recent_orders: deque[Order] = deque(maxlen=_MAX_RECENT_ORDERS_IN_MEMORY)

        # Fills
        self.fills: deque[Fill] = deque(maxlen=_MAX_FILLS_IN_MEMORY)

        # PnL
        self.pnl: PnLState = PnLState()

        # Risk
        self.risk: RiskMetrics = RiskMetrics()

        # Events
        self.events: deque[BotEvent] = deque(maxlen=_MAX_EVENTS_IN_MEMORY)

        # Network
        self.ws_connected: bool = False
        self.ws_reconnect_times: deque[datetime] = deque(maxlen=100)

    # ------------------------------------------------------------------
    # Lock-protected write helpers
    # ------------------------------------------------------------------

    async def set_status(self, status: BotStatus) -> None:
        async with self._lock:
            self.status = status
            if status == BotStatus.RUNNING and self.started_at is None:
                self.started_at = datetime.utcnow()

    async def activate_kill_switch(
        self, reason: KillReason, message: str = ""
    ) -> None:
        async with self._lock:
            if self.kill_switch_active:
                return  # already active, don't overwrite reason
            self.kill_switch_active = True
            self.kill_switch_reason = reason
            self.kill_switch_triggered_at = datetime.utcnow()
            self.events.append(
                BotEvent(
                    level=__import__("app.models", fromlist=["EventLevel"]).EventLevel.CRITICAL,
                    event_type="kill_switch",
                    message=message or reason.value,
                )
            )

    async def update_market(self, snapshot: MarketSnapshot) -> None:
        async with self._lock:
            sym = self.symbols.get(snapshot.symbol)
            if sym is None:
                return
            sym.market = snapshot
            # Set baseline spread on first clean tick
            if (
                sym.baseline_spread_bps is None
                and not snapshot.stale
                and not snapshot.book_corrupted
                and snapshot.spread_bps > 0
            ):
                sym.baseline_spread_bps = snapshot.spread_bps

    async def update_active_quote(
        self, symbol: str, quote: Optional[ActiveQuote]
    ) -> None:
        async with self._lock:
            sym = self.symbols.get(symbol)
            if sym is not None:
                sym.active_quote = quote

    async def update_position(self, position: Position) -> None:
        async with self._lock:
            sym = self.symbols.get(position.symbol)
            if sym is not None:
                sym.position = position

    async def add_order(self, order: Order) -> None:
        async with self._lock:
            self.open_orders[order.cloid.to_hex()] = order

    async def update_order(self, order: Order) -> None:
        async with self._lock:
            key = order.cloid.to_hex()
            self.open_orders[key] = order
            # Move to recent if terminal
            from app.models import OrderStatus
            if order.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
            ):
                self.open_orders.pop(key, None)
                self.recent_orders.append(order)

    async def add_fill(self, fill: Fill) -> None:
        async with self._lock:
            self.fills.append(fill)

    async def update_pnl(self, pnl: PnLState) -> None:
        async with self._lock:
            self.pnl = pnl

    async def update_risk(self, risk: RiskMetrics) -> None:
        async with self._lock:
            self.risk = risk

    async def add_event(self, event: BotEvent) -> None:
        async with self._lock:
            self.events.append(event)

    async def record_reconnect(self, at: Optional[datetime] = None) -> None:
        async with self._lock:
            self.ws_reconnect_times.append(at or datetime.utcnow())
            self.risk.reconnect_count += 1

    async def set_ws_connected(self, connected: bool) -> None:
        async with self._lock:
            self.ws_connected = connected

    async def heartbeat(self) -> None:
        async with self._lock:
            self.last_heartbeat = datetime.utcnow()

    # ------------------------------------------------------------------
    # Snapshot for dashboard (no lock held in caller)
    # ------------------------------------------------------------------

    async def snapshot(self) -> "BotState":
        """Return a deep copy of current state for dashboard consumption."""
        async with self._lock:
            return copy.deepcopy(self)

    # ------------------------------------------------------------------
    # Convenience read (lock-free, use only from within the bot loop)
    # ------------------------------------------------------------------

    def is_kill_switch_active(self) -> bool:
        return self.kill_switch_active
