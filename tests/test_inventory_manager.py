"""Tests for InventoryManager."""

from decimal import Decimal

import pytest

from app.inventory_manager import InventoryManager
from app.models import Fill, Position, Side


def make_inv(settings, bot_state) -> InventoryManager:
    return InventoryManager(bot_state, settings)


def set_btc_market_mid(bot_state, mid=Decimal("84000")):
    import asyncio
    from datetime import datetime, timezone
    from app.models import MarketSnapshot
    snap = MarketSnapshot(
        symbol="BTC", mid=mid, best_bid=mid - 10, best_ask=mid + 10,
        spread=Decimal("20"), spread_bps=Decimal("2"),
        imbalance=Decimal("0"), short_term_vol=Decimal("0"),
        updated_at=datetime.now(tz=timezone.utc),
    )
    asyncio.get_event_loop().run_until_complete(bot_state.update_market(snap))


class TestInventorySkew:
    def test_zero_position_zero_skew(self, settings, bot_state):
        import asyncio
        pos = Position(symbol="BTC", size=Decimal("0"), avg_cost=Decimal("0"))
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state)
        inv = make_inv(settings, bot_state)
        assert inv.get_inventory_skew_bps("BTC") == Decimal("0")

    def test_long_position_positive_skew(self, settings, bot_state):
        import asyncio
        # Long: positive skew (pushes prices down, encourages selling)
        mid = Decimal("84000")
        # Position of $42 (84% of $50 limit)
        size = Decimal("42") / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state, mid)
        inv = make_inv(settings, bot_state)
        skew = inv.get_inventory_skew_bps("BTC")
        assert skew > 0

    def test_short_position_negative_skew(self, settings, bot_state):
        import asyncio
        mid = Decimal("84000")
        size = -Decimal("42") / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state, mid)
        inv = make_inv(settings, bot_state)
        skew = inv.get_inventory_skew_bps("BTC")
        assert skew < 0

    def test_skew_capped_at_max(self, settings, bot_state):
        import asyncio
        # Oversize position: skew should be capped at inventory_skew_max_bps
        mid = Decimal("84000")
        # Position 2x the limit
        size = settings.max_position_usd_per_symbol * 2 / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state, mid)
        inv = make_inv(settings, bot_state)
        skew = inv.get_inventory_skew_bps("BTC")
        assert abs(skew) <= settings.inventory_skew_max_bps


class TestInventoryLimit:
    def test_at_limit_blocks_same_direction_long(self, settings, bot_state):
        import asyncio
        mid = Decimal("84000")
        # Position right at the USD limit
        size = settings.max_position_usd_per_symbol / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state, mid)
        inv = make_inv(settings, bot_state)
        # Long position at limit → BUY is blocked
        assert inv.is_inventory_limit_hit("BTC", Side.BUY) is True

    def test_at_limit_allows_opposite_direction(self, settings, bot_state):
        import asyncio
        mid = Decimal("84000")
        size = settings.max_position_usd_per_symbol / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))
        set_btc_market_mid(bot_state, mid)
        inv = make_inv(settings, bot_state)
        # Long position at limit → SELL is NOT blocked
        assert inv.is_inventory_limit_hit("BTC", Side.SELL) is False


class TestFillApplication:
    def test_apply_buy_fill_increases_position(self, settings, bot_state):
        import asyncio
        pos = Position(symbol="BTC", size=Decimal("0"), avg_cost=Decimal("0"))
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))

        fill = Fill(
            fill_id="f1",
            cloid=None,
            exchange_oid=None,
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("84000"),
            size=Decimal("0.001"),
            fee=Decimal("0.1"),
        )
        inv = make_inv(settings, bot_state)
        asyncio.get_event_loop().run_until_complete(inv.apply_fill(fill))
        assert bot_state.symbols["BTC"].position.size == Decimal("0.001")

    def test_apply_sell_fill_decreases_position(self, settings, bot_state):
        import asyncio
        pos = Position(symbol="BTC", size=Decimal("0.001"), avg_cost=Decimal("84000"))
        asyncio.get_event_loop().run_until_complete(bot_state.update_position(pos))

        fill = Fill(
            fill_id="f2",
            cloid=None,
            exchange_oid=None,
            symbol="BTC",
            side=Side.SELL,
            price=Decimal("84100"),
            size=Decimal("0.001"),
            fee=Decimal("0.1"),
        )
        inv = make_inv(settings, bot_state)
        asyncio.get_event_loop().run_until_complete(inv.apply_fill(fill))
        assert bot_state.symbols["BTC"].position.size == Decimal("0")
