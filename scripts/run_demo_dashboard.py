"""
Demo dashboard runner with mock data.

Populates BotState with realistic mock data (no exchange connection needed)
and starts the FastAPI dashboard so screenshots can be taken.

Usage:
    python scripts/run_demo_dashboard.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from app.api.app import create_dashboard_app
from app.models import (
    ActiveQuote,
    BotEvent,
    BotStatus,
    Cloid,
    EventLevel,
    Fill,
    KillReason,
    MarketSnapshot,
    Order,
    OrderKind,
    OrderStatus,
    PnLState,
    Position,
    RiskMetrics,
    Side,
    TIF,
)
from app.persistence import Persistence
from app.settings import Settings
from app.state import BotState


def make_settings() -> Settings:
    import os
    os.environ.pop("SYMBOLS", None)
    return Settings(
        private_key="0x" + "a" * 64,
        wallet_address="0x" + "b" * 40,
        testnet=True,
        symbols=["BTC", "ETH"],
        base_spread_bps=Decimal("10"),
        min_edge_bps=Decimal("5"),
        vol_multiplier=Decimal("2.0"),
        max_order_size_usd=Decimal("25"),
        max_position_usd_per_symbol=Decimal("500"),
        max_total_exposure_usd=Decimal("1000"),
        max_daily_loss_pct=Decimal("2"),
        max_intraday_drawdown_pct=Decimal("1.5"),
        inventory_skew_max_bps=Decimal("20"),
        imbalance_weight=Decimal("0.5"),
        quote_refresh_ms=5000,
        max_quote_age_ms=30000,
        price_replace_threshold_bps=Decimal("2.0"),
        leverage=1,
        max_reject_streak=10,
        max_reconnect_streak=5,
        reconnect_window_s=60,
        stale_data_threshold_ms=5000,
        abrupt_move_pct=Decimal("1.0"),
        abnormal_spread_multiplier=Decimal("5.0"),
        emergency_flatten_enabled=True,
        log_level="WARNING",
        dashboard_enabled=True,
        dashboard_host="127.0.0.1",
        dashboard_port=8080,
    )


async def populate_state(state: BotState, settings: Settings) -> None:
    now = datetime.now(tz=timezone.utc)

    await state.set_status(BotStatus.RUNNING)

    # Market snapshots
    btc_snap = MarketSnapshot(
        symbol="BTC",
        mid=Decimal("84250"),
        best_bid=Decimal("84240"),
        best_ask=Decimal("84260"),
        spread=Decimal("20"),
        spread_bps=Decimal("2.37"),
        imbalance=Decimal("0.12"),
        short_term_vol=Decimal("0.0031"),
        updated_at=now - timedelta(milliseconds=120),
    )
    eth_snap = MarketSnapshot(
        symbol="ETH",
        mid=Decimal("3182.50"),
        best_bid=Decimal("3182.00"),
        best_ask=Decimal("3183.00"),
        spread=Decimal("1"),
        spread_bps=Decimal("3.14"),
        imbalance=Decimal("-0.05"),
        short_term_vol=Decimal("0.0018"),
        updated_at=now - timedelta(milliseconds=95),
    )
    await state.update_market(btc_snap)
    await state.update_market(eth_snap)

    # Positions
    btc_pos = Position(
        symbol="BTC",
        size=Decimal("0.012"),
        avg_cost=Decimal("83900"),
        unrealized_pnl=Decimal("4.20"),
    )
    eth_pos = Position(
        symbol="ETH",
        size=Decimal("-0.5"),
        avg_cost=Decimal("3190"),
        unrealized_pnl=Decimal("-3.75"),
    )
    await state.update_position(btc_pos)
    await state.update_position(eth_pos)

    # Active quotes
    btc_quote = ActiveQuote(
        symbol="BTC",
        bid_cloid=Cloid.generate(),
        ask_cloid=Cloid.generate(),
        bid_price=Decimal("84237"),
        ask_price=Decimal("84263"),
        bid_size=Decimal("0.001"),
        ask_size=Decimal("0.001"),
        submitted_at=now - timedelta(seconds=4),
    )
    eth_quote = ActiveQuote(
        symbol="ETH",
        bid_cloid=Cloid.generate(),
        ask_cloid=Cloid.generate(),
        bid_price=Decimal("3181.70"),
        ask_price=Decimal("3183.30"),
        bid_size=Decimal("0.01"),
        ask_size=Decimal("0.01"),
        submitted_at=now - timedelta(seconds=2),
    )
    async with state._lock:
        state.symbols["BTC"].active_quote = btc_quote
        state.symbols["ETH"].active_quote = eth_quote

    # PnL state
    pnl = state.pnl
    pnl.day_start_equity = Decimal("1000")
    pnl.intraday_peak_equity = Decimal("1007.30")
    pnl.realized_pnl = Decimal("5.80")
    pnl.unrealized_pnl = Decimal("0.45")
    pnl.fees_paid = Decimal("0.42")
    await state.update_pnl(pnl)

    # Open orders
    orders = [
        Order(
            cloid=Cloid.generate(),
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("84237"),
            size=Decimal("0.001"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.OPEN,
        ),
        Order(
            cloid=Cloid.generate(),
            symbol="BTC",
            side=Side.SELL,
            price=Decimal("84263"),
            size=Decimal("0.001"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.OPEN,
        ),
        Order(
            cloid=Cloid.generate(),
            symbol="ETH",
            side=Side.BUY,
            price=Decimal("3181.70"),
            size=Decimal("0.01"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.OPEN,
        ),
        Order(
            cloid=Cloid.generate(),
            symbol="ETH",
            side=Side.SELL,
            price=Decimal("3183.30"),
            size=Decimal("0.01"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.OPEN,
        ),
    ]
    for o in orders:
        o.exchange_oid = 100000 + len(orders)
        await state.add_order(o)

    # Recent filled orders
    filled = Order(
        cloid=Cloid.generate(),
        symbol="BTC",
        side=Side.BUY,
        price=Decimal("83910"),
        size=Decimal("0.001"),
        tif=TIF.ALO,
        reduce_only=False,
        kind=OrderKind.MAKER_QUOTE,
        status=OrderStatus.FILLED,
    )
    filled.filled_size = Decimal("0.001")
    filled.created_at = now - timedelta(minutes=8)
    filled.updated_at = now - timedelta(minutes=7, seconds=40)
    await state.add_order(filled)
    filled.status = OrderStatus.FILLED
    await state.update_order(filled)

    # Risk metrics
    async with state._lock:
        state.risk.reconnect_count = 2
        state.risk.consecutive_rejects = 0
        state.ws_connected = True

    # Bot events
    event_data = [
        (EventLevel.INFO, "startup", "Bot started (testnet mode)", None),
        (EventLevel.INFO, "ws_connect", "WebSocket connected to Hyperliquid", None),
        (EventLevel.INFO, "market_data", "BTC market data feed active", "BTC"),
        (EventLevel.INFO, "market_data", "ETH market data feed active", "ETH"),
        (EventLevel.INFO, "order_placed", "BTC BUY 84237 × 0.001 [ALO] placed", "BTC"),
        (EventLevel.INFO, "order_placed", "BTC SELL 84263 × 0.001 [ALO] placed", "BTC"),
        (EventLevel.INFO, "fill", "BTC BUY 83910 × 0.001 filled (maker)", "BTC"),
        (EventLevel.WARN, "ws_reconnect", "WebSocket reconnected after 2s backoff", None),
        (EventLevel.INFO, "order_replaced", "BTC quotes refreshed after reconnect", "BTC"),
    ]
    async with state._lock:
        for i, (level, etype, msg, sym) in enumerate(event_data):
            e = BotEvent(
                event_id=str(uuid.uuid4()),
                level=level,
                event_type=etype,
                message=msg,
                symbol=sym,
                occurred_at=now - timedelta(minutes=30 - i * 3),
            )
            state.events.append(e)


def run_server(app, host: str, port: int) -> None:
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


async def main() -> None:
    settings = make_settings()
    state = BotState(symbols=settings.symbols)

    # Use temp DB for demo
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "demo.db"
    persistence = Persistence(db_path)
    persistence.initialize()

    # Populate mock data
    await populate_state(state, settings)

    app = create_dashboard_app(state, persistence, settings)

    host = settings.dashboard_host
    port = settings.dashboard_port

    print(f"Demo dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    # Run uvicorn in this thread
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
