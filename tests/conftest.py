"""
Test configuration and shared fixtures.

Integration tests (requiring network/exchange) are marked with
@pytest.mark.integration and skipped in normal CI.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.models import BotStatus, MarketSnapshot, Position
from app.settings import Settings
from app.state import BotState


@pytest.fixture
def settings() -> Settings:
    """Minimal settings for tests — pass all values directly, bypass env."""
    # Clear lru_cache so any previous singleton is not reused
    from app import settings as settings_module
    settings_module.get_settings.cache_clear()

    # Construct with explicit kwargs only — SettingsConfigDict env_file is
    # bypassed when all required fields are provided via init kwargs.
    # We also clear SYMBOLS from the environment to prevent pydantic-settings
    # from trying to JSON-decode an empty or unexpected SYMBOLS env var.
    import os
    os.environ.pop("SYMBOLS", None)

    s = Settings(
            private_key="0x" + "a" * 64,
            wallet_address="0x" + "b" * 40,
            testnet=True,
            symbols=["BTC", "ETH"],
            max_order_size_usd=Decimal("25"),
            max_position_usd_per_symbol=Decimal("50"),
            max_total_exposure_usd=Decimal("100"),
            max_daily_loss_pct=Decimal("2.0"),
            max_intraday_drawdown_pct=Decimal("1.0"),
            base_spread_bps=Decimal("10"),
            min_edge_bps=Decimal("5"),
            vol_multiplier=Decimal("2.0"),
            inventory_skew_max_bps=Decimal("20"),
            imbalance_weight=Decimal("0.5"),
            quote_refresh_ms=5000,
            max_quote_age_ms=30000,
            price_replace_threshold_bps=Decimal("2.0"),
            stale_data_threshold_ms=5000,
            abnormal_spread_multiplier=Decimal("5.0"),
            abrupt_move_pct=Decimal("1.0"),
            max_reject_streak=10,
            max_reconnect_streak=5,
            reconnect_window_s=60,
            emergency_flatten_enabled=True,
            dashboard_enabled=False,  # disable in tests
            dashboard_host="127.0.0.1",
            dashboard_port=8080,
            log_level="WARNING",
        )
    return s


@pytest.fixture
def bot_state(settings: Settings) -> BotState:
    return BotState(symbols=settings.symbols)


@pytest.fixture
def btc_market() -> MarketSnapshot:
    """A clean BTC market snapshot."""
    from datetime import datetime, timezone

    return MarketSnapshot(
        symbol="BTC",
        mid=Decimal("84000"),
        best_bid=Decimal("83990"),
        best_ask=Decimal("84010"),
        spread=Decimal("20"),
        spread_bps=Decimal("0.238"),
        imbalance=Decimal("0"),
        short_term_vol=Decimal("0.0001"),
        updated_at=datetime.now(tz=timezone.utc),
        stale=False,
        abrupt_move=False,
        book_corrupted=False,
    )


@pytest.fixture
def btc_position_long() -> Position:
    return Position(
        symbol="BTC",
        size=Decimal("0.0005"),   # small long
        avg_cost=Decimal("83000"),
        unrealized_pnl=Decimal("0.5"),
    )


@pytest.fixture
def btc_position_flat() -> Position:
    return Position(
        symbol="BTC",
        size=Decimal("0"),
        avg_cost=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
