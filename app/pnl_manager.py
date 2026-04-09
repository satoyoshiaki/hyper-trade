"""
PnL manager: tracks realized/unrealized PnL and fees.

Design:
- Realized PnL is computed from fills only (never trust exchange PnL without reconciliation).
- Unrealized PnL uses exchange-provided values (updated from userEvents/user_state).
- Daily loss and drawdown tracking use realized PnL minus fees only.
  NEVER use unrealized PnL as capacity for risk limits.
- day_start_equity is set at bot startup and reset at each calendar day boundary.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app.models import Fill, PnLState, Position, Side
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)


class PnLManager:
    """
    Maintains PnL state from fills and exchange snapshots.
    """

    def __init__(self, state: BotState, settings: Settings) -> None:
        self._state = state
        self._settings = settings

    async def initialize(self, initial_equity: Decimal) -> None:
        """
        Set the day-start equity baseline.
        Call this once at startup after fetching user_state from exchange.
        """
        pnl = self._state.pnl
        pnl.day_start_equity = initial_equity
        pnl.intraday_peak_equity = initial_equity
        await self._state.update_pnl(pnl)
        log.info("PnL initialized: day_start_equity=%s", initial_equity)

    async def on_fill(self, fill: Fill) -> None:
        """
        Update realized PnL from a fill event.

        Realized PnL is calculated as:
          For a closing fill: (fill_price - avg_cost) * fill_size * direction
        We approximate here; exact calculation requires the position context.
        The inventory_manager has the avg_cost information.

        Fees are always deducted immediately.
        """
        pnl = self._state.pnl
        pnl.fees_paid += fill.fee

        # Get position for this symbol to calculate realized PnL
        sym_state = self._state.symbols.get(fill.symbol)
        if sym_state and sym_state.position and sym_state.position.avg_cost > 0:
            pos = sym_state.position
            # Determine if this fill reduces the position
            if (fill.side == Side.SELL and pos.size > 0) or \
               (fill.side == Side.BUY and pos.size < 0):
                # Closing trade: realize PnL
                direction = Decimal("1") if pos.size > 0 else Decimal("-1")
                realized = (fill.price - pos.avg_cost) * fill.size * direction
                pnl.realized_pnl += realized
                log.info(
                    "Realized PnL: symbol=%s realized=%s (fill=%s avg_cost=%s)",
                    fill.symbol, realized, fill.price, pos.avg_cost,
                )

        # Update intraday peak
        current_equity = self._current_equity(pnl)
        if pnl.intraday_peak_equity is None or current_equity > pnl.intraday_peak_equity:
            pnl.intraday_peak_equity = current_equity

        await self._state.update_pnl(pnl)

    async def update_unrealized(self, positions: list[Position]) -> None:
        """
        Update unrealized PnL from exchange-provided position data.
        Called when userEvents / user_state provides fresh position data.
        """
        total_unrealized = Decimal("0")
        for pos in positions:
            total_unrealized += pos.unrealized_pnl

        pnl = self._state.pnl
        pnl.unrealized_pnl = total_unrealized
        await self._state.update_pnl(pnl)

    async def reset_daily(self, new_equity: Decimal) -> None:
        """
        Reset daily tracking at the start of a new calendar day.
        Preserves cumulative realized PnL but resets day-start baseline.
        """
        pnl = self._state.pnl
        pnl.day_start_equity = new_equity
        pnl.intraday_peak_equity = new_equity
        await self._state.update_pnl(pnl)
        log.info("Daily PnL reset: new day_start_equity=%s", new_equity)

    def _current_equity(self, pnl: PnLState) -> Decimal:
        """
        Conservative equity estimate using realized only.
        Do NOT include unrealized PnL in risk capacity calculations.
        """
        base = pnl.day_start_equity or Decimal("0")
        return base + pnl.realized_pnl - pnl.fees_paid

    def get_daily_loss_pct(self) -> Decimal:
        """Return current daily loss as a percentage (positive = loss)."""
        pnl = self._state.pnl
        if not pnl.day_start_equity or pnl.day_start_equity <= 0:
            return Decimal("0")
        return -(pnl.realized_pnl - pnl.fees_paid) / pnl.day_start_equity * 100

    def get_intraday_drawdown_pct(self) -> Decimal:
        """Return current intraday drawdown as a percentage (positive = drawdown)."""
        pnl = self._state.pnl
        if not pnl.intraday_peak_equity or pnl.intraday_peak_equity <= 0:
            return Decimal("0")
        current = self._current_equity(pnl)
        return (pnl.intraday_peak_equity - current) / pnl.intraday_peak_equity * 100
