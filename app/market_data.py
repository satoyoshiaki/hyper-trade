"""
Market data processor.

Receives raw book/mid updates from the WS client, validates them,
computes derived metrics, and updates BotState.

Safety:
- Stale data detection: if no update within stale_data_threshold_ms,
  MarketSnapshot.stale is set True and callers must stop quoting.
- Book corruption detection: bid >= ask is flagged immediately.
- Abrupt move detection: price jump > abrupt_move_pct% in recent ticks.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.exchange.normalizer import NormalizerError, parse_l2_book
from app.models import BookLevel, MarketSnapshot, NoQuoteReason
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)

_PRICE_HISTORY_LEN = 30  # ticks kept for vol/abrupt-move calculation


class MarketDataProcessor:
    """
    Processes incoming WebSocket messages and maintains MarketSnapshot
    for each symbol in BotState.
    """

    def __init__(self, state: BotState, settings: Settings) -> None:
        self._state = state
        self._settings = settings
        # Recent mid prices per symbol for vol/abrupt-move calculation
        self._price_history: dict[str, deque[Decimal]] = {
            sym: deque(maxlen=_PRICE_HISTORY_LEN)
            for sym in settings.symbols
        }
        self._last_mid: dict[str, Optional[Decimal]] = {
            sym: None for sym in settings.symbols
        }

    # ------------------------------------------------------------------
    # Callbacks registered with WSClient
    # ------------------------------------------------------------------

    async def on_book_update(self, symbol: str, raw: dict[str, Any]) -> None:
        """Called by WSClient when an l2Book message arrives."""
        try:
            bids, asks = parse_l2_book(symbol, raw)
            await self._process_book(symbol, bids, asks)
        except NormalizerError as exc:
            log.warning("Failed to parse l2Book for %s: %s", symbol, exc)
            # Mark stale so quote engine stops quoting
            await self._mark_stale(symbol, reason="parse_error")

    async def on_mid_update(self, raw: dict[str, Any]) -> None:
        """
        Called when an allMids message arrives.
        UNVERIFIED: allMids message shape.
        ASSUMPTION: {"mids": {"BTC": "84200.5", ...}}
        """
        mids: dict[str, str] = raw.get("mids", {})
        for symbol in self._settings.symbols:
            mid_str = mids.get(symbol)
            if mid_str is None:
                continue
            try:
                mid = Decimal(mid_str)
                self._last_mid[symbol] = mid
            except InvalidOperation:
                log.warning("Invalid mid price for %s: %r", symbol, mid_str)

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    async def _process_book(
        self, symbol: str, bids: list[BookLevel], asks: list[BookLevel]
    ) -> None:
        if not bids or not asks:
            log.warning("Empty book for %s — no-quote", symbol)
            await self._mark_stale(symbol, reason="empty_book")
            return

        best_bid = bids[0].price
        best_ask = asks[0].price

        # Book corruption check
        if best_bid >= best_ask:
            log.error(
                "Book corrupted %s: bid=%s >= ask=%s", symbol, best_bid, best_ask
            )
            snap = await self._build_snapshot(
                symbol, best_bid, best_ask, bids, asks, book_corrupted=True
            )
            await self._state.update_market(snap)
            return

        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_bps = (spread / mid * 10000) if mid > 0 else Decimal("0")

        # Imbalance: (bid_qty - ask_qty) / total_qty at top N levels
        bid_qty = sum(b.size for b in bids[:5])
        ask_qty = sum(a.size for a in asks[:5])
        total_qty = bid_qty + ask_qty
        imbalance = (
            (bid_qty - ask_qty) / total_qty if total_qty > 0 else Decimal("0")
        )

        # Update price history
        self._price_history[symbol].append(mid)
        self._last_mid[symbol] = mid

        # Short-term volatility (std dev of returns over recent ticks)
        vol = self._calc_vol(symbol)

        # Abrupt move detection
        abrupt = self._detect_abrupt_move(symbol, mid)
        if abrupt:
            log.warning("Abrupt price move detected for %s mid=%s", symbol, mid)

        snap = await self._build_snapshot(
            symbol, best_bid, best_ask, bids, asks,
            mid=mid, spread=spread, spread_bps=spread_bps,
            imbalance=imbalance, vol=vol, abrupt_move=abrupt,
        )
        await self._state.update_market(snap)

    async def _build_snapshot(
        self,
        symbol: str,
        best_bid: Decimal,
        best_ask: Decimal,
        bids: list[BookLevel],
        asks: list[BookLevel],
        mid: Optional[Decimal] = None,
        spread: Optional[Decimal] = None,
        spread_bps: Optional[Decimal] = None,
        imbalance: Decimal = Decimal("0"),
        vol: Decimal = Decimal("0"),
        stale: bool = False,
        abrupt_move: bool = False,
        book_corrupted: bool = False,
    ) -> MarketSnapshot:
        _mid = mid or (best_bid + best_ask) / 2
        _spread = spread or (best_ask - best_bid)
        _spread_bps = spread_bps or (_spread / _mid * 10000 if _mid > 0 else Decimal("0"))
        return MarketSnapshot(
            symbol=symbol,
            mid=_mid,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=_spread,
            spread_bps=_spread_bps,
            imbalance=imbalance,
            short_term_vol=vol,
            bids=bids,
            asks=asks,
            updated_at=datetime.now(tz=timezone.utc),
            stale=stale,
            abrupt_move=abrupt_move,
            book_corrupted=book_corrupted,
        )

    async def _mark_stale(self, symbol: str, reason: str = "") -> None:
        """Mark symbol's market data as stale so quote engine no-quotes."""
        sym_state = self._state.symbols.get(symbol)
        if sym_state and sym_state.market:
            sym_state.market.stale = True
            log.warning("Market data marked stale for %s: %s", symbol, reason)

    def _calc_vol(self, symbol: str) -> Decimal:
        """
        Estimate short-term realized volatility from recent mid price ticks.
        Returns 0 if fewer than 2 data points.
        """
        prices = list(self._price_history[symbol])
        if len(prices) < 2:
            return Decimal("0")
        returns = [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if not returns:
            return Decimal("0")
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / n
        # Integer sqrt approximation via float — acceptable for vol estimation
        vol = Decimal(str(float(variance) ** 0.5))
        return vol

    def _detect_abrupt_move(self, symbol: str, current_mid: Decimal) -> bool:
        """
        Detect if price has moved > abrupt_move_pct% from recent reference.
        Uses the oldest price in the recent window as reference.
        """
        prices = list(self._price_history[symbol])
        if len(prices) < 2:
            return False
        ref = prices[0]
        if ref <= 0:
            return False
        move_pct = abs(current_mid - ref) / ref * 100
        return move_pct >= self._settings.abrupt_move_pct

    # ------------------------------------------------------------------
    # Stale data check (called by risk_manager / kill_switch)
    # ------------------------------------------------------------------

    def is_stale(self, symbol: str) -> tuple[bool, int]:
        """
        Return (is_stale, age_ms) based on last MarketSnapshot update time.
        """
        sym_state = self._state.symbols.get(symbol)
        if sym_state is None or sym_state.market is None:
            return True, 999999
        age_ms = int(
            (datetime.now(tz=timezone.utc) - sym_state.market.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
            * 1000
        )
        return age_ms >= self._settings.stale_data_threshold_ms, age_ms
