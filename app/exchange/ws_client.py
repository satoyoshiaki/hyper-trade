"""
WebSocket connection manager for Hyperliquid.

Responsibilities:
- Subscribe to l2Book, allMids, userEvents channels
- Handle reconnection with backoff
- Track reconnect count/streak for kill switch monitoring
- Deliver parsed messages to registered callbacks

UNVERIFIED: exact WebSocket subscription API, reconnect behaviour,
            message shapes — all isolated here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

from app.settings import Settings
from app.telemetry import get_logger

log = get_logger(__name__)

# Type alias for async callbacks
AsyncCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

_RECONNECT_DELAYS = [1, 2, 5, 10, 30]  # seconds, last value repeated


class WSClient:
    """
    Manages the Hyperliquid WebSocket connection.

    UNVERIFIED: The SDK's Info class has a subscribe() method.
    ASSUMPTION: It accepts a subscription dict and a callback.
    If this doesn't work, replace with a raw websockets connection here.

    Usage:
        ws = WSClient(settings)
        ws.on_book_update("BTC", my_callback)
        ws.on_user_event(my_user_callback)
        await ws.run()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._book_callbacks: dict[str, list[AsyncCallback]] = {}
        self._mid_callbacks: list[AsyncCallback] = []
        self._user_event_callbacks: list[AsyncCallback] = []
        self._running = False
        self._reconnect_count = 0
        self._reconnect_times: list[datetime] = []
        self._info: Any = None  # hyperliquid.Info with WS enabled

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_book_update(self, symbol: str, cb: AsyncCallback) -> None:
        self._book_callbacks.setdefault(symbol, []).append(cb)

    def on_mid_update(self, cb: AsyncCallback) -> None:
        self._mid_callbacks.append(cb)

    def on_user_event(self, cb: AsyncCallback) -> None:
        self._user_event_callbacks.append(cb)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Connect and maintain the WebSocket connection.
        Reconnects automatically with exponential backoff.
        """
        self._running = True
        attempt = 0

        while self._running:
            try:
                await self._connect_and_subscribe()
            except asyncio.CancelledError:
                log.info("WSClient cancelled, shutting down.")
                break
            except Exception as exc:
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                self._reconnect_count += 1
                self._reconnect_times.append(datetime.now(tz=timezone.utc))
                log.warning(
                    "WS disconnected (attempt=%d): %s. Reconnecting in %ds.",
                    attempt + 1, exc, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
            else:
                attempt = 0  # reset on clean exit

    async def stop(self) -> None:
        self._running = False
        if self._info is not None:
            try:
                # UNVERIFIED: SDK disconnect method
                pass
            except Exception:
                pass

    def get_reconnect_count(self) -> int:
        return self._reconnect_count

    def get_recent_reconnect_times(self) -> list[datetime]:
        return list(self._reconnect_times)

    # ------------------------------------------------------------------
    # Internal: connect + subscribe
    # ------------------------------------------------------------------

    async def _connect_and_subscribe(self) -> None:
        """
        UNVERIFIED: This entire method depends on SDK WS behaviour.

        The SDK's Info class has a subscribe() method that runs a background
        thread. We wrap callbacks to dispatch into our asyncio loop.

        TODO: If the SDK doesn't support async callbacks, use a queue bridge:
              SDK thread → asyncio.Queue → our async consumer.
        """
        try:
            from hyperliquid.info import Info
        except ImportError as exc:
            raise RuntimeError(
                "hyperliquid-python-sdk is not installed."
            ) from exc

        loop = asyncio.get_running_loop()

        # UNVERIFIED: skip_ws=False enables WS mode
        self._info = Info(base_url=self._settings.api_url, skip_ws=False)

        def _make_sync_callback(
            async_cbs: list[AsyncCallback],
        ) -> Callable[[dict], None]:
            """Bridge: SDK sync callback → asyncio coroutines."""
            def _cb(data: dict) -> None:
                for async_cb in async_cbs:
                    asyncio.run_coroutine_threadsafe(async_cb(data), loop)
            return _cb

        # Subscribe to l2Book for each symbol
        for symbol in self._settings.symbols:
            cbs = self._book_callbacks.get(symbol, [])
            if cbs:
                # UNVERIFIED: subscription dict format
                # ASSUMPTION: {"type": "l2Book", "coin": symbol}
                self._info.subscribe(
                    {"type": "l2Book", "coin": symbol},
                    _make_sync_callback(cbs),
                )
                log.info("Subscribed to l2Book for %s", symbol)

        # Subscribe to allMids
        if self._mid_callbacks:
            # UNVERIFIED: allMids subscription format
            self._info.subscribe(
                {"type": "allMids"},
                _make_sync_callback(self._mid_callbacks),
            )
            log.info("Subscribed to allMids")

        # Subscribe to userEvents
        if self._user_event_callbacks:
            # UNVERIFIED: user subscription format — may require wallet address
            # ASSUMPTION: {"type": "userEvents", "user": address}
            self._info.subscribe(
                {
                    "type": "userEvents",
                    "user": self._settings.wallet_address,
                },
                _make_sync_callback(self._user_event_callbacks),
            )
            log.info("Subscribed to userEvents for %s", self._settings.wallet_address[:6] + "...")

        log.info("WS subscriptions active. Waiting for messages...")

        # Keep alive: the SDK runs its own thread, we just wait
        # UNVERIFIED: there may be a better way to block until disconnect
        while self._running:
            await asyncio.sleep(1)
