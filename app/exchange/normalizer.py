"""
Converts raw Hyperliquid API/WS responses into internal domain models.

All UNVERIFIED response shapes are marked explicitly.
If a response shape doesn't match, we raise NormalizerError so the
caller can go to no-quote / safe state rather than silently misparse.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models import BookLevel, Fill, Order, OrderStatus, Side

log = logging.getLogger(__name__)


class NormalizerError(Exception):
    """Raised when a raw response cannot be safely parsed."""


def _dec(value: Any, field: str = "") -> Decimal:
    """Safely convert a value to Decimal. Raises NormalizerError on failure."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise NormalizerError(f"Cannot convert {field}={value!r} to Decimal") from exc


def parse_book_level(raw: dict[str, Any]) -> BookLevel:
    """
    UNVERIFIED: l2Book level shape.
    Assumed: {"px": "...", "sz": "...", "n": int}
    Based on SDK examples — confirm against live data.
    """
    try:
        return BookLevel(
            price=_dec(raw["px"], "px"),
            size=_dec(raw["sz"], "sz"),
            num_orders=int(raw.get("n", 0)),
        )
    except KeyError as exc:
        raise NormalizerError(f"Missing field in book level: {exc}") from exc


def parse_l2_book(
    symbol: str, raw: dict[str, Any]
) -> tuple[list[BookLevel], list[BookLevel]]:
    """
    Parse l2Book WebSocket message into (bids, asks).

    UNVERIFIED: exact WS message shape.
    ASSUMPTION: {"levels": [[bid_levels...], [ask_levels...]]}
    """
    try:
        levels = raw.get("levels") or raw.get("data", {}).get("levels", [])
        if not levels or len(levels) < 2:
            raise NormalizerError(f"Unexpected l2Book shape for {symbol}: {raw!r}")
        bids = [parse_book_level(lvl) for lvl in levels[0]]
        asks = [parse_book_level(lvl) for lvl in levels[1]]
        return bids, asks
    except (TypeError, IndexError) as exc:
        raise NormalizerError(f"Failed to parse l2Book for {symbol}") from exc


def parse_fill(raw: dict[str, Any]) -> Fill:
    """
    Parse a userEvents fill into a Fill model.

    UNVERIFIED: exact fill event shape.
    ASSUMPTION: {"coin": str, "side": "A"/"B", "px": str, "sz": str,
                 "fee": str, "oid": int, "cloid": str|None, "tid": int}
    """
    try:
        side_raw = raw.get("side", "")
        # UNVERIFIED: "A" = ask (sell), "B" = bid (buy)
        side = Side.SELL if side_raw == "A" else Side.BUY

        from app.models import Cloid
        cloid_raw = raw.get("cloid")
        cloid = None
        if cloid_raw:
            try:
                cloid = Cloid(value=int(cloid_raw, 16))
            except (ValueError, TypeError):
                log.warning("Could not parse cloid: %r", cloid_raw)

        return Fill(
            fill_id=str(raw.get("tid", "")),
            cloid=cloid,
            exchange_oid=raw.get("oid"),
            symbol=str(raw.get("coin", "")),
            side=side,
            price=_dec(raw["px"], "px"),
            size=_dec(raw["sz"], "sz"),
            fee=_dec(raw.get("fee", "0"), "fee"),
        )
    except KeyError as exc:
        raise NormalizerError(f"Missing field in fill: {exc}") from exc


def parse_order_update(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Parse an order status update from userEvents.

    UNVERIFIED: exact shape. Returns a dict for now; full Order mapping
    requires matching against local CLOID registry in order_manager.
    TODO: confirm exact status strings from exchange ("open", "filled", etc.)
    """
    return {
        "oid": raw.get("oid"),
        "cloid": raw.get("cloid"),
        "status": raw.get("status", ""),
        "filled_size": _dec(raw.get("filledSz", "0"), "filledSz"),
        "price": _dec(raw.get("limitPx", "0"), "limitPx") if raw.get("limitPx") else None,
    }
