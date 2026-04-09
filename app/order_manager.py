"""
Order manager: manages the full lifecycle of orders.

Responsibilities:
- Assigns and tracks CLOIDs
- Maintains order state machine
- Prevents double-submission and double-cancellation
- Handles stale quote cancellation
- Interfaces with ExchangeClient for submit/cancel

State machine:
  PENDING → OPEN → PARTIALLY_FILLED → FILLED
                 ↘ CANCELED
                 ↘ REJECTED
                 ↘ EXPIRED
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.exchange.client import ExchangeClient, ExchangeClientError
from app.models import (
    Cloid,
    Fill,
    Order,
    OrderKind,
    OrderStatus,
    Side,
    TIF,
)
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)


class OrderManagerError(Exception):
    pass


class OrderManager:
    """
    Manages order submission, tracking, and cancellation.

    Thread-safety: all public methods must be called from the asyncio event loop.
    """

    def __init__(
        self,
        state: BotState,
        settings: Settings,
        exchange_client: ExchangeClient,
    ) -> None:
        self._state = state
        self._settings = settings
        self._client = exchange_client
        # Track in-flight submits to prevent double-submission
        self._submitting: set[str] = set()   # cloid hex strings
        self._canceling: set[str] = set()    # cloid hex strings

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_maker_order(
        self,
        symbol: str,
        side: Side,
        price: Decimal,
        size: Decimal,
    ) -> Optional[Order]:
        """
        Submit a post-only (ALO) maker order.
        Returns the Order on success, None on failure.
        Does NOT submit if kill switch is active.
        """
        if self._state.is_kill_switch_active():
            log.warning("submit_maker_order blocked: kill switch active")
            return None

        cloid = Cloid.generate()

        # Prevent double-submission for same CLOID (should not happen, but guard)
        cloid_hex = cloid.to_hex()
        if cloid_hex in self._submitting:
            log.warning("Duplicate submit detected for cloid %s — skipping", cloid_hex)
            return None

        order = Order(
            cloid=cloid,
            symbol=symbol,
            side=side,
            price=price,
            size=size,
            tif=TIF.ALO,          # FACT: ALO = post-only on Hyperliquid
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.PENDING,
        )

        self._submitting.add(cloid_hex)
        await self._state.add_order(order)

        try:
            result = self._client.place_order(
                symbol=symbol,
                side=side,
                price=price,
                size=size,
                tif=TIF.ALO,
                reduce_only=False,
                cloid=cloid,
            )
            # UNVERIFIED: result shape — assume {"status": "ok", "response": {...}}
            # TODO: parse exchange_oid from result and attach to order
            exchange_oid = self._parse_exchange_oid(result)
            order.status = OrderStatus.OPEN
            order.exchange_oid = exchange_oid
            order.updated_at = datetime.now(tz=timezone.utc)
            await self._state.update_order(order)
            log.info(
                "Maker order submitted symbol=%s side=%s price=%s size=%s cloid=%s",
                symbol, side.value, price, size, cloid,
                extra={"symbol": symbol, "action": "submit", "side": side.value,
                       "price": str(price), "size": str(size), "cloid": str(cloid)},
            )
            return order
        except ExchangeClientError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            order.updated_at = datetime.now(tz=timezone.utc)
            await self._state.update_order(order)
            log.warning("Order rejected cloid=%s: %s", cloid, exc)
            return None
        finally:
            self._submitting.discard(cloid_hex)

    async def submit_emergency_flatten(
        self,
        symbol: str,
        side: Side,
        size: Decimal,
    ) -> Optional[Order]:
        """
        Submit a reduce-only IOC order for emergency position flatten.
        Only used by kill_switch / execution on emergency shutdown.
        """
        cloid = Cloid.generate()
        order = Order(
            cloid=cloid,
            symbol=symbol,
            side=side,
            price=Decimal("0"),     # market-ish price for IOC
            size=size,
            tif=TIF.IOC,
            reduce_only=True,
            kind=OrderKind.EMERGENCY_FLATTEN,
            status=OrderStatus.PENDING,
        )

        await self._state.add_order(order)

        try:
            # For IOC flatten, use a price that will certainly match
            # UNVERIFIED: best approach for reduce-only market-equivalent on HL
            # ASSUMPTION: setting limit_px far from mid will get IOC fill at market
            sym_state = self._state.symbols.get(symbol)
            if sym_state and sym_state.market:
                # Offer at mid ± 5% to ensure fill
                if side == Side.SELL:
                    price = sym_state.market.mid * Decimal("0.95")
                else:
                    price = sym_state.market.mid * Decimal("1.05")
            else:
                log.error("Cannot flatten %s: no market data", symbol)
                return None

            result = self._client.place_order(
                symbol=symbol,
                side=side,
                price=price,
                size=size,
                tif=TIF.IOC,
                reduce_only=True,
                cloid=cloid,
            )
            order.status = OrderStatus.OPEN
            order.updated_at = datetime.now(tz=timezone.utc)
            await self._state.update_order(order)
            log.warning(
                "Emergency flatten submitted symbol=%s side=%s size=%s cloid=%s",
                symbol, side.value, size, cloid,
                extra={"symbol": symbol, "action": "emergency_flatten",
                       "side": side.value, "size": str(size), "cloid": str(cloid)},
            )
            return order
        except ExchangeClientError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            order.updated_at = datetime.now(tz=timezone.utc)
            await self._state.update_order(order)
            log.error("Emergency flatten rejected: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, cloid: Cloid) -> bool:
        """
        Cancel an open order by CLOID.
        Returns True if cancel was sent, False if order not found or already terminal.
        """
        cloid_hex = cloid.to_hex()

        # Prevent double-cancel
        if cloid_hex in self._canceling:
            log.debug("Cancel already in-flight for %s", cloid_hex)
            return False

        order = self._state.open_orders.get(cloid_hex)
        if order is None:
            log.debug("cancel_order: order %s not in open_orders", cloid_hex)
            return False
        if order.status not in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            log.debug("cancel_order: order %s is already %s", cloid_hex, order.status)
            return False

        self._canceling.add(cloid_hex)
        try:
            self._client.cancel_by_cloid(order.symbol, cloid)
            order.status = OrderStatus.CANCELED
            order.updated_at = datetime.now(tz=timezone.utc)
            await self._state.update_order(order)
            log.info("Order cancelled cloid=%s", cloid,
                     extra={"action": "cancel", "cloid": str(cloid), "symbol": order.symbol})
            return True
        except ExchangeClientError as exc:
            log.warning("Cancel failed for cloid=%s: %s", cloid_hex, exc)
            return False
        finally:
            self._canceling.discard(cloid_hex)

    async def cancel_all_open_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders, optionally for a specific symbol.
        Returns number of cancel requests sent.
        """
        to_cancel = [
            order for order in list(self._state.open_orders.values())
            if symbol is None or order.symbol == symbol
        ]
        count = 0
        for order in to_cancel:
            if await self.cancel_order(order.cloid):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    async def process_fill(self, fill: Fill) -> None:
        """
        Update order state when a fill event arrives from userEvents.
        """
        if fill.cloid is None:
            log.debug("Fill with no CLOID: fill_id=%s", fill.fill_id)
            return

        cloid_hex = fill.cloid.to_hex()
        order = self._state.open_orders.get(cloid_hex)
        if order is None:
            log.debug("Fill for unknown CLOID %s (may be pre-restart order)", cloid_hex)
            return

        order.filled_size += fill.size
        order.updated_at = datetime.now(tz=timezone.utc)

        if order.filled_size >= order.size:
            order.status = OrderStatus.FILLED
        elif order.filled_size > 0:
            order.status = OrderStatus.PARTIALLY_FILLED

        await self._state.update_order(order)
        await self._state.add_fill(fill)
        log.info(
            "Fill processed cloid=%s symbol=%s side=%s fill_price=%s fill_size=%s",
            cloid_hex, fill.symbol, fill.side.value, fill.price, fill.size,
            extra={"action": "fill", "cloid": cloid_hex, "symbol": fill.symbol,
                   "side": fill.side.value, "price": str(fill.price), "size": str(fill.size)},
        )

    # ------------------------------------------------------------------
    # Stale quote cleanup
    # ------------------------------------------------------------------

    async def cancel_stale_quotes(self) -> None:
        """
        Cancel any open maker quotes older than max_quote_age_ms.
        Called periodically by execution loop.
        """
        now = datetime.now(tz=timezone.utc)
        max_age_ms = self._settings.max_quote_age_ms

        for order in list(self._state.open_orders.values()):
            if order.kind != OrderKind.MAKER_QUOTE:
                continue
            age_ms = int((now - order.created_at.replace(tzinfo=timezone.utc)).total_seconds() * 1000)
            if age_ms >= max_age_ms:
                log.info(
                    "Cancelling stale quote cloid=%s age=%dms", order.cloid, age_ms
                )
                await self.cancel_order(order.cloid)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_exchange_oid(self, result: dict) -> Optional[int]:
        """
        Extract exchange order ID from place_order result.
        UNVERIFIED: result structure — adjust when live result is confirmed.
        ASSUMPTION: {"response": {"data": {"statuses": [{"resting": {"oid": int}}]}}}
        """
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                first = statuses[0]
                resting = first.get("resting") or first.get("filled")
                if resting:
                    return int(resting.get("oid", 0))
        except Exception:
            pass
        return None
