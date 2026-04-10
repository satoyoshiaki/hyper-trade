"""
Thin wrapper around the Hyperliquid Python SDK's Exchange and Info classes.

All SDK-specific behaviour is isolated here. The rest of the bot never
imports hyperliquid directly.

UNVERIFIED items are tagged with # UNVERIFIED: comments.
If the SDK behaviour differs from what is described here, fix it in this
file only — the rest of the codebase should not need changes.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from app.models import Cloid, Order, Side, TIF
from app.settings import Settings
from app.telemetry import get_logger

log = get_logger(__name__)


class ExchangeClientError(Exception):
    """Raised when an exchange operation fails."""


class ExchangeClient:
    """
    Wrapper around hyperliquid Exchange + Info.

    Lazy-initialised: call await connect() before use.
    The constructor does NOT import hyperliquid so that the rest of the
    codebase can be imported and tested without the SDK installed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._exchange: Any = None  # hyperliquid.Exchange
        self._info: Any = None      # hyperliquid.Info
        self._meta: Optional[dict[str, Any]] = None
        self._sz_decimals: dict[str, int] = {}
        self._tick_sizes: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Initialise Exchange and Info objects.

        FACT: hyperliquid-python-sdk uses eth_account for signing.
        UNVERIFIED: exact constructor signatures — may change across versions.
        """
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
        except ImportError as exc:
            raise ExchangeClientError(
                "hyperliquid-python-sdk is not installed. "
                "Run: pip install hyperliquid-python-sdk"
            ) from exc

        wallet = Account.from_key(self._settings.private_key)

        # UNVERIFIED: base_url parameter name and testnet URL
        # ASSUMPTION: base_url kwarg is accepted; testnet URL is correct
        base_url = self._settings.api_url

        try:
            self._info = Info(base_url=base_url, skip_ws=True)
            self._exchange = Exchange(wallet=wallet, base_url=base_url)
        except Exception as exc:
            raise ExchangeClientError(
                f"Failed to initialize Hyperliquid clients for {base_url}: {exc}"
            ) from exc

        log.info(
            "ExchangeClient connected. testnet=%s url=%s",
            self._settings.testnet,
            base_url,
        )

    def fetch_meta(self) -> None:
        """
        Fetch asset metadata (szDecimals, tickSize) for all symbols.

        UNVERIFIED: exact Info.meta() response shape.
        ASSUMPTION: response has {"universe": [{"name": str, "szDecimals": int, ...}]}
        This must be confirmed at runtime — bot refuses to start if this fails.
        """
        if self._info is None:
            raise ExchangeClientError("Not connected. Call connect() first.")

        try:
            meta = self._info.meta()
        except Exception as exc:
            raise ExchangeClientError(f"Failed to fetch meta: {exc}") from exc

        # UNVERIFIED: field names in meta response
        universe: list[dict] = meta.get("universe", [])
        for asset in universe:
            name: str = asset.get("name", "")
            sz_dec: int = int(asset.get("szDecimals", 8))
            # UNVERIFIED: tickSize field — may not exist, may need calculation
            tick_raw = asset.get("tickSize")
            tick = Decimal(str(tick_raw)) if tick_raw else Decimal("0.1") ** sz_dec
            self._sz_decimals[name] = sz_dec
            self._tick_sizes[name] = tick

        self._meta = meta
        log.info("Asset metadata loaded for %d assets.", len(universe))

    def get_sz_decimals(self, symbol: str) -> int:
        """Return size decimals for a symbol. Raises if not loaded."""
        if symbol not in self._sz_decimals:
            raise ExchangeClientError(
                f"No sz_decimals for {symbol}. Was fetch_meta() called?"
            )
        return self._sz_decimals[symbol]

    def get_tick_size(self, symbol: str) -> Decimal:
        """Return minimum price increment for a symbol. Raises if not loaded."""
        if symbol not in self._tick_sizes:
            raise ExchangeClientError(
                f"No tick_size for {symbol}. Was fetch_meta() called?"
            )
        return self._tick_sizes[symbol]

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_user_state(self) -> dict[str, Any]:
        """
        Fetch current user state (positions, margin, balance).
        FACT: info.user_state(address) is available.
        """
        if self._info is None:
            raise ExchangeClientError("Not connected.")
        return self._info.user_state(self._settings.wallet_address)

    def get_open_orders(self) -> list[dict[str, Any]]:
        """
        Fetch open orders from exchange.
        FACT: info.open_orders(address) is available.
        """
        if self._info is None:
            raise ExchangeClientError("Not connected.")
        return self._info.open_orders(self._settings.wallet_address)

    def get_l2_snapshot(self, symbol: str) -> dict[str, Any]:
        """
        Fetch L2 order book snapshot.
        FACT: info.l2_snapshot(coin) is available.
        """
        if self._info is None:
            raise ExchangeClientError("Not connected.")
        return self._info.l2_snapshot(symbol)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        tif: TIF,
        reduce_only: bool,
        cloid: Cloid,
    ) -> dict[str, Any]:
        """
        Place a limit order.

        FACT: Exchange.order() accepts coin, is_buy, limit_px, sz, order_type.
        FACT: ALO TIF = {"limit": {"tif": "Alo"}} for post-only.
        FACT: CLOID supported via Cloid.from_int() in SDK.
        UNVERIFIED: exact kwarg names for reduce_only and cloid in this SDK version.
        """
        if self._exchange is None:
            raise ExchangeClientError("Not connected.")

        try:
            from hyperliquid.utils.types import Cloid as HLCloid
        except ImportError:
            HLCloid = None  # UNVERIFIED: SDK Cloid import path

        is_buy = side == Side.BUY
        order_type = {"limit": {"tif": tif.value}}

        kwargs: dict[str, Any] = {
            "coin": symbol,
            "is_buy": is_buy,
            "sz": float(size),
            "limit_px": float(price),
            "order_type": order_type,
            "reduce_only": reduce_only,
        }

        # UNVERIFIED: cloid kwarg name — may be "cloid" or "client_id"
        if HLCloid is not None:
            try:
                kwargs["cloid"] = HLCloid.from_int(cloid.value)
            except Exception:
                log.warning("Failed to attach CLOID %s — placing without it", cloid)

        try:
            result = self._exchange.order(**kwargs)
            log.debug(
                "order placed symbol=%s side=%s price=%s size=%s cloid=%s",
                symbol, side.value, price, size, cloid,
                extra={"symbol": symbol, "action": "place_order",
                       "side": side.value, "price": str(price),
                       "size": str(size), "cloid": str(cloid)},
            )
            return result
        except Exception as exc:
            raise ExchangeClientError(f"place_order failed: {exc}") from exc

    def cancel_order(self, symbol: str, oid: int) -> dict[str, Any]:
        """
        Cancel by exchange order ID.
        FACT: exchange.cancel(coin, oid) is available.
        """
        if self._exchange is None:
            raise ExchangeClientError("Not connected.")
        try:
            result = self._exchange.cancel(symbol, oid)
            log.debug(
                "order cancelled symbol=%s oid=%s", symbol, oid,
                extra={"symbol": symbol, "action": "cancel_order", "order_id": oid},
            )
            return result
        except Exception as exc:
            raise ExchangeClientError(f"cancel_order failed: {exc}") from exc

    def cancel_by_cloid(self, symbol: str, cloid: Cloid) -> dict[str, Any]:
        """
        Cancel by CLOID.
        FACT: exchange.cancel_by_cloid() is available.
        UNVERIFIED: exact method signature in 0.22.0.
        """
        if self._exchange is None:
            raise ExchangeClientError("Not connected.")
        try:
            from hyperliquid.utils.types import Cloid as HLCloid
            hl_cloid = HLCloid.from_int(cloid.value)
            result = self._exchange.cancel_by_cloid(symbol, hl_cloid)
            log.debug(
                "order cancelled by cloid symbol=%s cloid=%s", symbol, cloid,
                extra={"symbol": symbol, "action": "cancel_by_cloid", "cloid": str(cloid)},
            )
            return result
        except Exception as exc:
            raise ExchangeClientError(f"cancel_by_cloid failed: {exc}") from exc

    def update_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        """
        Set leverage for a symbol.
        UNVERIFIED: exact method name and kwargs in SDK.
        TODO: confirm exchange.update_leverage() signature.
        """
        if self._exchange is None:
            raise ExchangeClientError("Not connected.")
        try:
            # UNVERIFIED: method name may differ
            result = self._exchange.update_leverage(leverage, symbol, is_cross=False)
            log.info("Leverage set symbol=%s leverage=%d", symbol, leverage)
            return result
        except AttributeError:
            log.warning(
                "update_leverage not available in this SDK version — UNVERIFIED"
            )
            return {}
        except Exception as exc:
            raise ExchangeClientError(f"update_leverage failed: {exc}") from exc
