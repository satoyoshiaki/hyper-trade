"""
Bot entrypoint.

Startup sequence:
  1. Load and validate settings (fail fast on invalid config)
  2. Setup logging
  3. Initialize persistence (SQLite)
  4. Connect to exchange (REST), fetch meta, set leverage
  5. Initialize all components
  6. Start WebSocket listeners
  7. Start kill switch monitor
  8. Start dashboard (if enabled)
  9. Run main quote loop

Shutdown sequence:
  - Cancel all open orders
  - Persist final state
  - Close WS connection
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from decimal import Decimal

from app.exchange.client import ExchangeClient, ExchangeClientError
from app.exchange.ws_client import WSClient
from app.execution import Execution
from app.inventory_manager import InventoryManager
from app.kill_switch import KillSwitch
from app.market_data import MarketDataProcessor
from app.models import BotStatus, EventLevel, BotEvent
from app.order_manager import OrderManager
from app.persistence import Persistence
from app.pnl_manager import PnLManager
from app.quote_engine import QuoteEngine
from app.risk_manager import RiskManager
from app.settings import Settings, get_settings
from app.state import BotState
from app.telemetry import get_logger, setup_logging

log = get_logger(__name__)

_SNAPSHOT_INTERVAL_S = 30
_QUOTE_LOOP_SLEEP_S = 0.1  # inner loop polling interval


async def run_bot(settings: Settings) -> None:
    """Main bot coroutine."""

    # ----------------------------------------------------------------
    # 1. Initialize components
    # ----------------------------------------------------------------
    state = BotState(symbols=settings.symbols)
    await state.set_status(BotStatus.STARTING)

    persistence = Persistence(settings.db_path)
    persistence.initialize()

    exchange_client = ExchangeClient(settings)
    market_data = MarketDataProcessor(state, settings)
    inventory_manager = InventoryManager(state, settings)
    pnl_manager = PnLManager(state, settings)
    quote_engine = QuoteEngine(settings)
    risk_manager = RiskManager(state, settings)
    order_manager = OrderManager(state, settings, exchange_client)
    kill_switch = KillSwitch(state, settings, risk_manager)
    execution = Execution(
        state, settings, quote_engine, risk_manager,
        order_manager, inventory_manager, kill_switch,
    )

    # ----------------------------------------------------------------
    # 2. Connect to exchange, fetch metadata
    # ----------------------------------------------------------------
    log.info("Connecting to exchange (testnet=%s)...", settings.testnet)
    try:
        exchange_client.connect()
        exchange_client.fetch_meta()
    except ExchangeClientError as exc:
        log.critical("Failed to connect to exchange: %s", exc)
        await state.set_status(BotStatus.ERROR)
        return

    # Populate tick/lot sizes into quote engine
    for symbol in settings.symbols:
        try:
            tick = exchange_client.get_tick_size(symbol)
            sz_dec = exchange_client.get_sz_decimals(symbol)
            quote_engine.set_asset_specs(symbol, tick, sz_dec)
            log.info("Asset specs loaded: %s tick=%s sz_dec=%d", symbol, tick, sz_dec)
        except ExchangeClientError as exc:
            log.critical("Cannot get asset specs for %s: %s", symbol, exc)
            await state.set_status(BotStatus.ERROR)
            return

    # Set leverage for all symbols
    for symbol in settings.symbols:
        try:
            exchange_client.update_leverage(symbol, settings.leverage)
        except ExchangeClientError as exc:
            log.warning("Could not set leverage for %s: %s", symbol, exc)

    # Fetch initial user state for PnL baseline
    try:
        user_state = exchange_client.get_user_state()
        # UNVERIFIED: exact field for account equity
        # ASSUMPTION: marginSummary.accountValue is total equity in USDC
        equity_raw = (
            user_state.get("marginSummary", {}).get("accountValue", "0")
        )
        initial_equity = Decimal(str(equity_raw))
        await pnl_manager.initialize(initial_equity)
        # Sync initial positions
        positions = user_state.get("assetPositions", [])
        await inventory_manager.update_from_exchange(
            [p.get("position", {}) for p in positions]
        )
    except Exception as exc:
        log.warning("Could not fetch initial user state: %s", exc)

    # ----------------------------------------------------------------
    # 3. Setup WebSocket
    # ----------------------------------------------------------------
    ws_client = WSClient(settings)

    for symbol in settings.symbols:
        async def _book_cb(raw: dict, _sym: str = symbol) -> None:
            await market_data.on_book_update(_sym, raw)
        ws_client.on_book_update(symbol, _book_cb)

    ws_client.on_mid_update(market_data.on_mid_update)

    async def _user_event_cb(raw: dict) -> None:
        # UNVERIFIED: userEvents structure — route fills and order updates
        # ASSUMPTION: event has "fills" list and/or "orders" list
        await _handle_user_event(raw, order_manager, inventory_manager, pnl_manager, state, persistence)

    ws_client.on_user_event(_user_event_cb)

    # ----------------------------------------------------------------
    # 4. Start background tasks
    # ----------------------------------------------------------------
    tasks = [
        asyncio.create_task(ws_client.run(), name="ws_client"),
        asyncio.create_task(kill_switch.run(), name="kill_switch"),
        asyncio.create_task(
            _snapshot_loop(state, persistence, settings),
            name="snapshot_loop",
        ),
        asyncio.create_task(
            _stale_quote_cleanup(order_manager),
            name="stale_quote_cleanup",
        ),
    ]

    # Dashboard
    if settings.dashboard_enabled:
        from app.api.app import create_dashboard_app
        import uvicorn
        dashboard_app = create_dashboard_app(state, persistence, settings)
        config = uvicorn.Config(
            dashboard_app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
        log.info(
            "Dashboard starting at http://%s:%d",
            settings.dashboard_host, settings.dashboard_port,
        )
        if settings.dashboard_host != "127.0.0.1":
            log.warning(
                "WARNING: Dashboard is bound to %s — this exposes it externally. "
                "See README for security notes.",
                settings.dashboard_host,
            )

    await state.set_status(BotStatus.RUNNING)
    log.info("Bot running. Symbols: %s", settings.symbols)

    # ----------------------------------------------------------------
    # 5. Main quote loop
    # ----------------------------------------------------------------
    quote_interval_s = settings.quote_refresh_ms / 1000

    try:
        while not state.is_kill_switch_active():
            await state.heartbeat()
            for symbol in settings.symbols:
                await execution.quote_tick(symbol)
            await asyncio.sleep(quote_interval_s)
    except asyncio.CancelledError:
        log.info("Main loop cancelled.")
    except Exception as exc:
        log.critical("Unexpected error in main loop: %s", exc, exc_info=True)
    finally:
        await _shutdown(state, order_manager, ws_client, kill_switch, tasks)


async def _handle_user_event(
    raw: dict,
    order_manager: OrderManager,
    inventory_manager: InventoryManager,
    pnl_manager: PnLManager,
    state: BotState,
    persistence: Persistence,
) -> None:
    """
    Route incoming userEvents to the appropriate handlers.
    UNVERIFIED: exact message structure.
    """
    from app.exchange.normalizer import NormalizerError, parse_fill

    # Process fills
    fills_raw = raw.get("fills", [])
    for fill_raw in fills_raw:
        try:
            fill = parse_fill(fill_raw)
            await order_manager.process_fill(fill)
            await inventory_manager.apply_fill(fill)
            await pnl_manager.on_fill(fill)
            await persistence.save_fill(fill)
        except NormalizerError as exc:
            log.warning("Could not parse fill: %s | raw=%s", exc, fill_raw)

    # UNVERIFIED: order update events in userEvents
    # TODO: handle order status changes from exchange (resting→filled, canceled, etc.)


async def _snapshot_loop(
    state: BotState,
    persistence: Persistence,
    settings: Settings,
) -> None:
    """Periodic persistence of PnL and inventory snapshots."""
    while True:
        await asyncio.sleep(_SNAPSHOT_INTERVAL_S)
        try:
            pnl = state.pnl
            await persistence.save_pnl_snapshot(
                pnl.realized_pnl, pnl.unrealized_pnl, pnl.fees_paid
            )
            for sym, sym_state in state.symbols.items():
                if sym_state.position:
                    await persistence.save_inventory_snapshot(sym_state.position)
        except Exception as exc:
            log.warning("Snapshot failed: %s", exc)


async def _stale_quote_cleanup(order_manager: OrderManager) -> None:
    """Periodically cancel quotes that have aged out."""
    while True:
        await asyncio.sleep(10)
        await order_manager.cancel_stale_quotes()


async def _shutdown(
    state: BotState,
    order_manager: OrderManager,
    ws_client: WSClient,
    kill_switch: KillSwitch,
    tasks: list,
) -> None:
    """Graceful shutdown sequence."""
    log.info("Shutting down...")
    await state.set_status(BotStatus.STOPPING)

    # Cancel all open orders
    cancelled = await order_manager.cancel_all_open_orders()
    log.info("Cancelled %d open orders on shutdown.", cancelled)

    # Stop WS and kill switch monitor
    await ws_client.stop()
    await kill_switch.stop()

    # Cancel background tasks
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await state.set_status(BotStatus.STOPPED)
    log.info("Bot stopped.")


def main() -> None:
    """CLI entrypoint."""
    try:
        settings = get_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.log_dir)

    log.info(
        "Starting Hyperliquid maker bot. testnet=%s symbols=%s",
        settings.testnet, settings.symbols,
    )

    # Handle SIGINT/SIGTERM gracefully
    loop = asyncio.new_event_loop()

    def _handle_signal() -> None:
        log.info("Signal received — requesting shutdown...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(run_bot(settings))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
