"""
Execution engine: orchestrates the quote→risk→order flow.

This is the main trading loop logic for one symbol.
Called periodically from main.py.

Flow per tick:
  1. Check kill switch → skip if active
  2. Get market snapshot
  3. Compute inventory skew
  4. Compute quote from quote_engine
  5. Check if quote needs replacement
  6. Risk check
  7. Cancel old quotes if replacing
  8. Submit new maker orders
  9. Cancel stale quotes
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.inventory_manager import InventoryManager
from app.kill_switch import KillSwitch
from app.models import BotEvent, EventLevel, OrderKind, Side
from app.order_manager import OrderManager
from app.quote_engine import QuoteEngine
from app.risk_manager import RiskCheckFailed, RiskManager
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)


class Execution:
    """
    Ties together quote_engine, risk_manager, and order_manager
    into the per-symbol trading loop.
    """

    def __init__(
        self,
        state: BotState,
        settings: Settings,
        quote_engine: QuoteEngine,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        inventory_manager: InventoryManager,
        kill_switch: KillSwitch,
    ) -> None:
        self._state = state
        self._settings = settings
        self._qe = quote_engine
        self._risk = risk_manager
        self._om = order_manager
        self._inv = inventory_manager
        self._ks = kill_switch

        # Register emergency flatten callback with kill switch
        self._ks.set_flatten_callback(self.emergency_flatten_all)

    # ------------------------------------------------------------------
    # Per-symbol quote tick
    # ------------------------------------------------------------------

    async def quote_tick(self, symbol: str) -> None:
        """
        Run one quote cycle for a symbol.
        Should be called every quote_refresh_ms.
        """
        if self._state.is_kill_switch_active():
            return

        sym_state = self._state.symbols.get(symbol)
        if sym_state is None or sym_state.market is None:
            log.debug("quote_tick: no market data for %s", symbol)
            return

        market = sym_state.market
        active_quote = sym_state.active_quote

        # Compute inventory signals
        skew_bps = self._inv.get_inventory_skew_bps(symbol)
        inv_limit_bid = self._inv.is_inventory_limit_hit(symbol, Side.BUY)
        inv_limit_ask = self._inv.is_inventory_limit_hit(symbol, Side.SELL)

        # Compute new quote
        result = self._qe.compute_quote(
            symbol=symbol,
            market=market,
            inventory_skew_bps=skew_bps,
            inventory_limit_bid=inv_limit_bid,
            inventory_limit_ask=inv_limit_ask,
        )

        if result.quote is None:
            log.debug(
                "No quote for %s: reason=%s", symbol,
                result.reason.value if result.reason else "unknown",
            )
            # Cancel any existing quotes since we're going no-quote
            await self._om.cancel_all_open_orders(symbol)
            await self._state.update_active_quote(symbol, None)
            return

        new_quote = result.quote

        # Decide whether to replace existing quote
        should_replace = True
        if active_quote is not None:
            now_ms = int(
                (datetime.now(tz=timezone.utc)
                 - active_quote.submitted_at.replace(tzinfo=timezone.utc)
                ).total_seconds() * 1000
            )
            should_replace = self._qe.should_replace(new_quote, active_quote, now_ms)

        if not should_replace:
            log.debug("quote_tick %s: quote unchanged, no replace needed", symbol)
            return

        # Cancel existing quotes before placing new ones
        if active_quote is not None:
            for cloid in [active_quote.bid_cloid, active_quote.ask_cloid]:
                if cloid is not None:
                    await self._om.cancel_order(cloid)

        # Risk check and submit
        bid_order = None
        ask_order = None

        if new_quote.bid_size > 0:
            try:
                self._risk.check_order(symbol, Side.BUY, new_quote.bid_price * new_quote.bid_size)
                bid_order = await self._om.submit_maker_order(
                    symbol, Side.BUY, new_quote.bid_price, new_quote.bid_size
                )
                if bid_order:
                    self._risk.record_success()
                else:
                    self._risk.record_reject()
            except RiskCheckFailed as exc:
                log.debug("Bid risk check failed %s: %s", symbol, exc.reason)

        if new_quote.ask_size > 0:
            try:
                self._risk.check_order(symbol, Side.SELL, new_quote.ask_price * new_quote.ask_size)
                ask_order = await self._om.submit_maker_order(
                    symbol, Side.SELL, new_quote.ask_price, new_quote.ask_size
                )
                if ask_order:
                    self._risk.record_success()
                else:
                    self._risk.record_reject()
            except RiskCheckFailed as exc:
                log.debug("Ask risk check failed %s: %s", symbol, exc.reason)

        # Update active quote state
        from app.models import ActiveQuote
        if bid_order or ask_order:
            aq = ActiveQuote(
                symbol=symbol,
                bid_cloid=bid_order.cloid if bid_order else None,
                ask_cloid=ask_order.cloid if ask_order else None,
                bid_price=new_quote.bid_price,
                ask_price=new_quote.ask_price,
                bid_size=new_quote.bid_size,
                ask_size=new_quote.ask_size,
            )
            await self._state.update_active_quote(symbol, aq)
        else:
            await self._state.update_active_quote(symbol, None)

    # ------------------------------------------------------------------
    # Emergency flatten
    # ------------------------------------------------------------------

    async def emergency_flatten_all(self) -> None:
        """
        Attempt to flatten all open positions using reduce-only IOC orders.
        Called by kill_switch on trigger.
        Does NOT raise — errors are logged but ignored (we're already in kill mode).
        """
        log.warning("Emergency flatten: cancelling all open orders first...")
        await self._om.cancel_all_open_orders()

        log.warning("Emergency flatten: submitting reduce-only IOC for all positions...")
        for symbol, sym_state in self._state.symbols.items():
            if sym_state.position is None or sym_state.position.size == 0:
                continue

            pos_size = sym_state.position.size
            side = Side.SELL if pos_size > 0 else Side.BUY
            size = abs(pos_size)

            log.warning(
                "Emergency flatten: %s %s %s", symbol, side.value, size
            )
            try:
                await self._om.submit_emergency_flatten(symbol, side, size)
            except Exception as exc:
                log.error("Flatten failed for %s: %s", symbol, exc)
                # Continue to next symbol even if this one fails
