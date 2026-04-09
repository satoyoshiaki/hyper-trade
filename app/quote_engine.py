"""
Quote engine: computes bid/ask prices for a given symbol.

Design principles:
- Returns a Quote or None (no-quote).
- No-quote is not an error — it is the safe default when conditions are bad.
- All adjustments are additive bps offsets from mid.
- tick/lot rounding is applied last.
- cancel/replace suppression: don't replace if price hasn't moved enough.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional

from app.models import ActiveQuote, MarketSnapshot, NoQuoteReason, Quote, Side
from app.settings import Settings
from app.telemetry import get_logger

log = get_logger(__name__)


@dataclass
class QuoteResult:
    quote: Optional[Quote]
    reason: Optional[NoQuoteReason] = None  # set when quote is None


class QuoteEngine:
    """
    Computes bid/ask quotes from market snapshot + inventory skew.

    The caller is responsible for:
    - Checking kill switch (before calling this)
    - Applying risk checks (after receiving a quote)
    - Rounding to exchange tick/lot sizes (via helpers here)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Tick and size decimal info per symbol — populated at startup
        self._tick_sizes: dict[str, Decimal] = {}
        self._sz_decimals: dict[str, int] = {}

    def set_asset_specs(
        self, symbol: str, tick_size: Decimal, sz_decimals: int
    ) -> None:
        """Must be called at startup before quoting."""
        self._tick_sizes[symbol] = tick_size
        self._sz_decimals[symbol] = sz_decimals

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def compute_quote(
        self,
        symbol: str,
        market: MarketSnapshot,
        inventory_skew_bps: Decimal,
        inventory_limit_bid: bool = False,
        inventory_limit_ask: bool = False,
    ) -> QuoteResult:
        """
        Compute a two-sided quote for the given symbol.

        Returns QuoteResult with quote=None if quoting is not safe.
        """
        # --- Safety checks before computing ---
        if market.stale:
            return QuoteResult(None, NoQuoteReason.STALE_MARKET_DATA)
        if market.book_corrupted:
            return QuoteResult(None, NoQuoteReason.BOOK_CORRUPTED)
        if market.abrupt_move:
            return QuoteResult(None, NoQuoteReason.ABRUPT_MOVE)
        if market.mid <= 0:
            return QuoteResult(None, NoQuoteReason.INSUFFICIENT_DATA)

        # Check abnormal spread
        sym_state_baseline = None  # caller should pass baseline; use spread_bps heuristic
        if market.spread_bps > self._settings.base_spread_bps * self._settings.abnormal_spread_multiplier:
            return QuoteResult(None, NoQuoteReason.ABNORMAL_SPREAD)

        # --- Compute spread adjustment ---
        half_spread_bps = self._compute_half_spread_bps(
            market.spread_bps,
            market.short_term_vol,
            market.imbalance,
            inventory_skew_bps,
        )

        # --- Min edge check ---
        if half_spread_bps < self._settings.min_edge_bps / 2:
            return QuoteResult(None, NoQuoteReason.MIN_EDGE_NOT_MET)

        mid = market.mid

        # --- Apply inventory skew to shift quotes asymmetrically ---
        # Positive skew (long): push ask up (harder to buy more), pull bid down
        # Negative skew (short): push bid down (harder to sell more), pull ask up
        skew_offset_bps = inventory_skew_bps
        skew_offset = mid * skew_offset_bps / Decimal("10000")

        half_spread = mid * half_spread_bps / Decimal("10000")

        raw_bid = mid - half_spread - skew_offset
        raw_ask = mid + half_spread - skew_offset  # skew shifts both the same direction

        # --- Compute order sizes ---
        tick_size = self._tick_sizes.get(symbol, Decimal("0.1"))
        sz_dec = self._sz_decimals.get(symbol, 4)

        bid_price = self._round_price(raw_bid, tick_size, side=Side.BUY)
        ask_price = self._round_price(raw_ask, tick_size, side=Side.SELL)

        # Final sanity: bid must be below ask
        if bid_price >= ask_price:
            return QuoteResult(None, NoQuoteReason.MIN_EDGE_NOT_MET)

        # --- Compute sizes ---
        # Simple fixed-USD sizing; TODO: vol-scaled sizing in future
        order_size_usd = self._settings.max_order_size_usd
        bid_size = self._round_size(order_size_usd / bid_price, sz_dec)
        ask_size = self._round_size(order_size_usd / ask_price, sz_dec)

        # Suppress quoting on a side if inventory limit hit
        if inventory_limit_bid:
            bid_size = Decimal("0")
        if inventory_limit_ask:
            ask_size = Decimal("0")

        # If both sides are zero, no-quote
        if bid_size == 0 and ask_size == 0:
            return QuoteResult(None, NoQuoteReason.INVENTORY_LIMIT_HIT)

        quote = Quote(
            symbol=symbol,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
        )
        return QuoteResult(quote=quote)

    # ------------------------------------------------------------------
    # Cancel/replace suppression
    # ------------------------------------------------------------------

    def should_replace(
        self,
        new_quote: Quote,
        active: ActiveQuote,
        elapsed_ms: int,
    ) -> bool:
        """
        Return True if the active quote should be replaced.

        Replace if:
        - Elapsed time > max_quote_age_ms
        - Either price has moved more than price_replace_threshold_bps
        """
        if elapsed_ms >= self._settings.max_quote_age_ms:
            return True

        threshold = self._settings.price_replace_threshold_bps
        mid = (new_quote.bid_price + new_quote.ask_price) / 2

        bid_move_bps = (
            abs(new_quote.bid_price - active.bid_price) / mid * 10000
        ) if mid > 0 else Decimal("0")
        ask_move_bps = (
            abs(new_quote.ask_price - active.ask_price) / mid * 10000
        ) if mid > 0 else Decimal("0")

        return bid_move_bps >= threshold or ask_move_bps >= threshold

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_half_spread_bps(
        self,
        market_spread_bps: Decimal,
        short_term_vol: Decimal,
        imbalance: Decimal,
        inventory_skew_bps: Decimal,
    ) -> Decimal:
        """
        half_spread = base / 2
                    + vol_component
                    + imbalance_component
        Inventory skew is handled separately (shifts prices, not spread).
        """
        base_half = self._settings.base_spread_bps / 2

        # Vol widens the spread
        # vol is in return units; convert to bps-like units
        vol_bps = short_term_vol * 10000 * self._settings.vol_multiplier
        vol_component = vol_bps / 2

        # Imbalance: positive imbalance (bid-heavy) → raise bid side to reduce fill risk
        # We widen the half-spread proportionally
        imbalance_component = (
            abs(imbalance)
            * self._settings.imbalance_weight
            * self._settings.base_spread_bps
            / 2
        )

        half_spread = base_half + vol_component + imbalance_component
        return half_spread

    def _round_price(self, price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
        """
        Round price to exchange tick size.

        Bid (buy): round DOWN (be conservative, don't cross the spread).
        Ask (sell): round UP (be conservative, don't cross the spread).

        FACT: Hyperliquid requires max 5 significant figures for perp prices.
        UNVERIFIED: exact rounding rules — confirm against live API.
        """
        if tick_size <= 0:
            return price
        if side == Side.BUY:
            return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
        else:
            return (price / tick_size).to_integral_value(rounding=ROUND_UP) * tick_size

    def _round_size(self, size: Decimal, sz_decimals: int) -> Decimal:
        """
        Round size to the allowed number of decimal places.
        Always round DOWN (never send more than intended).
        """
        quantize_str = Decimal("1") / (Decimal("10") ** sz_decimals)
        return size.quantize(quantize_str, rounding=ROUND_DOWN)
