"""
Inventory and position manager.

Tracks per-symbol inventory from fills and exchange position snapshots.
Computes inventory skew to feed into the quote engine.

Safety:
- Position updates from exchange are authoritative over local fill accounting.
- If inventory exceeds limit, same-direction new orders are blocked.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app.models import Fill, Position, Side
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)


class InventoryManager:
    """
    Maintains inventory state per symbol and computes skew for quote engine.
    """

    def __init__(self, state: BotState, settings: Settings) -> None:
        self._state = state
        self._settings = settings

    # ------------------------------------------------------------------
    # Position updates (authoritative source from exchange)
    # ------------------------------------------------------------------

    async def update_from_exchange(self, positions: list[dict]) -> None:
        """
        Update positions from exchange user_state response.

        UNVERIFIED: exact field names in position dict.
        ASSUMPTION: {"coin": str, "szi": str, "entryPx": str, "unrealizedPnl": str}
        """
        for raw in positions:
            symbol = raw.get("coin", "")
            if symbol not in self._settings.symbols:
                continue
            try:
                size = Decimal(str(raw.get("szi", "0")))
                avg_cost_raw = raw.get("entryPx")
                avg_cost = Decimal(str(avg_cost_raw)) if avg_cost_raw else Decimal("0")
                unrealized = Decimal(str(raw.get("unrealizedPnl", "0")))
            except Exception as exc:
                log.warning("Failed to parse position for %s: %s", symbol, exc)
                continue

            pos = Position(
                symbol=symbol,
                size=size,
                avg_cost=avg_cost,
                unrealized_pnl=unrealized,
            )
            await self._state.update_position(pos)
            log.debug(
                "Position updated %s size=%s avg_cost=%s", symbol, size, avg_cost
            )

    async def apply_fill(self, fill: Fill) -> None:
        """
        Apply a fill to local inventory tracking.

        Note: exchange position updates are authoritative. This is a local
        estimate used for skew computation between exchange reconciliations.
        """
        sym_state = self._state.symbols.get(fill.symbol)
        if sym_state is None:
            return

        pos = sym_state.position
        if pos is None:
            # Bootstrap position from fill
            size = fill.size if fill.side == Side.BUY else -fill.size
            pos = Position(symbol=fill.symbol, size=size, avg_cost=fill.price)
            await self._state.update_position(pos)
            return

        old_size = pos.size
        delta = fill.size if fill.side == Side.BUY else -fill.size
        new_size = old_size + delta

        if new_size == 0:
            avg_cost = Decimal("0")
        elif (old_size >= 0 and delta > 0) or (old_size <= 0 and delta < 0):
            # Adding to position: weighted average cost
            total_cost = abs(old_size) * pos.avg_cost + fill.size * fill.price
            avg_cost = total_cost / abs(new_size)
        else:
            # Reducing or flipping: avg_cost unchanged for the remaining side
            avg_cost = pos.avg_cost

        updated = Position(
            symbol=fill.symbol,
            size=new_size,
            avg_cost=avg_cost,
            unrealized_pnl=pos.unrealized_pnl,
        )
        await self._state.update_position(updated)

    # ------------------------------------------------------------------
    # Skew and limit checks
    # ------------------------------------------------------------------

    def get_inventory_skew_bps(self, symbol: str) -> Decimal:
        """
        Compute inventory skew in bps to pass to quote engine.

        Positive skew → we are long → raise ask, lower bid.
        Negative skew → we are short → lower bid (more competitive), raise ask.

        Skew magnitude is proportional to position vs max_position, capped at
        inventory_skew_max_bps.

        Returns 0 if no position data.
        """
        sym_state = self._state.symbols.get(symbol)
        if sym_state is None or sym_state.position is None:
            return Decimal("0")

        pos_size = sym_state.position.size
        if pos_size == 0:
            return Decimal("0")

        mid = sym_state.market.mid if sym_state.market else Decimal("0")
        if mid == 0:
            return Decimal("0")

        pos_usd = abs(pos_size) * mid
        max_pos_usd = self._settings.max_position_usd_per_symbol

        # Utilization ratio: -1.0 (max short) to +1.0 (max long)
        ratio = pos_size * mid / max_pos_usd
        # Clamp to [-1, 1]
        ratio = max(Decimal("-1"), min(Decimal("1"), ratio))

        skew = ratio * self._settings.inventory_skew_max_bps
        return skew

    def is_inventory_limit_hit(self, symbol: str, side: Side) -> bool:
        """
        Return True if adding a new order on the given side would exceed
        the max position limit for the symbol.

        Blocks same-direction new entries when at the limit.
        """
        sym_state = self._state.symbols.get(symbol)
        if sym_state is None or sym_state.position is None:
            return False

        pos = sym_state.position
        mid = sym_state.market.mid if sym_state.market else Decimal("0")
        if mid == 0:
            return False

        pos_usd = abs(pos.size) * mid
        at_limit = pos_usd >= self._settings.max_position_usd_per_symbol

        if not at_limit:
            return False

        # At limit: block same-direction new entries only
        if side == Side.BUY and pos.size > 0:
            return True
        if side == Side.SELL and pos.size < 0:
            return True
        return False

    def get_total_exposure_usd(self) -> Decimal:
        """Sum of absolute USD exposure across all symbols."""
        total = Decimal("0")
        for symbol, sym_state in self._state.symbols.items():
            if sym_state.position is None or sym_state.market is None:
                continue
            total += abs(sym_state.position.size) * sym_state.market.mid
        return total
