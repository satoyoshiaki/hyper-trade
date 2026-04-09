"""
Edge case tests for safety-critical behaviors.

Tests here focus on boundary conditions, race conditions by design,
and defensive behaviors that are hard to test in unit tests for individual modules.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import (
    Cloid,
    Fill,
    KillReason,
    Order,
    OrderKind,
    OrderStatus,
    Position,
    Side,
    TIF,
)


class TestCloidUniqueness:
    def test_generated_cloids_are_unique(self):
        cloids = {Cloid.generate().value for _ in range(1000)}
        assert len(cloids) == 1000

    def test_cloid_hex_is_32_chars_plus_prefix(self):
        cloid = Cloid.generate()
        hex_str = cloid.to_hex()
        assert hex_str.startswith("0x")
        assert len(hex_str) == 34  # "0x" + 32 hex chars


class TestPnLEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_equity_does_not_divide_by_zero(self, settings, bot_state):
        """PnLManager must not divide by zero when day_start_equity is 0."""
        from app.pnl_manager import PnLManager
        pnl_mgr = PnLManager(bot_state, settings)
        # Initialize with zero equity
        await pnl_mgr.initialize(Decimal("0"))
        # Calling get_daily_loss_pct with zero equity should return 0, not crash
        pct = pnl_mgr.get_daily_loss_pct()
        assert pct == Decimal("0")

    @pytest.mark.asyncio
    async def test_unrealized_pnl_not_included_in_risk_capacity(
        self, settings, bot_state
    ):
        """
        Unrealized PnL must NOT be included in daily loss or drawdown calculations.
        This is a critical safety requirement.
        """
        from app.pnl_manager import PnLManager
        from app.risk_manager import RiskCheckFailed, RiskManager

        # Setup: large unrealized profit, but realized loss at limit
        pnl_mgr = PnLManager(bot_state, settings)
        await pnl_mgr.initialize(Decimal("1000"))

        pnl = bot_state.pnl
        pnl.day_start_equity = Decimal("1000")
        pnl.intraday_peak_equity = Decimal("1000")
        pnl.realized_pnl = Decimal("-25")   # 2.5% loss — over limit
        pnl.unrealized_pnl = Decimal("100")  # large unrealized gain — must be IGNORED
        pnl.fees_paid = Decimal("0")
        await bot_state.update_pnl(pnl)

        # Market setup
        from datetime import datetime, timezone
        from app.models import MarketSnapshot
        snap = MarketSnapshot(
            symbol="BTC", mid=Decimal("84000"),
            best_bid=Decimal("83990"), best_ask=Decimal("84010"),
            spread=Decimal("20"), spread_bps=Decimal("2"),
            imbalance=Decimal("0"), short_term_vol=Decimal("0"),
            updated_at=datetime.now(tz=timezone.utc),
        )
        await bot_state.update_market(snap)

        risk = RiskManager(bot_state, settings)
        # Should still fail because realized loss > limit, despite unrealized gain
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "daily_loss_pct" in exc.value.reason or "intraday_drawdown_pct" in exc.value.reason


class TestEmergencyFlattenConditions:
    @pytest.mark.asyncio
    async def test_emergency_flatten_only_for_qualifying_reasons(
        self, settings, bot_state
    ):
        """
        Emergency flatten must only trigger for MANUAL, DAILY_LOSS_EXCEEDED,
        and INTRADAY_DRAWDOWN_EXCEEDED — NOT for stale data or reconnect.
        This prevents unnecessary position changes during transient issues.
        """
        from app.kill_switch import KillSwitch
        from app.risk_manager import RiskManager

        flatten_called = []

        async def mock_flatten():
            flatten_called.append(True)

        risk = RiskManager(bot_state, settings)
        ks = KillSwitch(bot_state, settings, risk)
        ks.set_flatten_callback(mock_flatten)

        # These should NOT trigger flatten
        for reason in [KillReason.STALE_MARKET_DATA, KillReason.RECONNECT_STORM,
                       KillReason.CONSECUTIVE_REJECTS, KillReason.BOOK_CORRUPTED,
                       KillReason.ABRUPT_PRICE_MOVE, KillReason.ABNORMAL_SPREAD]:
            # Reset state for each test
            bot_state.kill_switch_active = False
            await ks._trigger(reason, f"test {reason.value}")
            bot_state.kill_switch_active = False  # reset for next

        assert len(flatten_called) == 0, (
            f"Flatten should not be called for transient reasons, "
            f"but was called {len(flatten_called)} times"
        )

    @pytest.mark.asyncio
    async def test_emergency_flatten_triggers_for_manual_kill(
        self, settings, bot_state
    ):
        from app.kill_switch import KillSwitch
        from app.risk_manager import RiskManager

        flatten_called = []

        async def mock_flatten():
            flatten_called.append(True)

        risk = RiskManager(bot_state, settings)
        ks = KillSwitch(bot_state, settings, risk)
        ks.set_flatten_callback(mock_flatten)

        await ks._trigger(KillReason.MANUAL, "manual kill")
        assert len(flatten_called) == 1


class TestOrderSafetyInvariants:
    """
    Invariants that must hold for all orders at all times.
    """

    @pytest.mark.asyncio
    async def test_maker_orders_always_use_alo_tif(self, settings, bot_state):
        """All MAKER_QUOTE orders must use ALO TIF (post-only)."""
        from app.exchange.client import ExchangeClientError
        from app.order_manager import OrderManager
        from unittest.mock import MagicMock

        client = MagicMock()
        client.place_order.return_value = {
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        om = OrderManager(bot_state, settings, client)

        order = await om.submit_maker_order(
            "BTC", Side.BUY, Decimal("84000"), Decimal("0.001")
        )
        assert order is not None
        assert order.tif == TIF.ALO, "Maker orders must use ALO (post-only), not GTC or IOC"
        assert order.reduce_only is False

    @pytest.mark.asyncio
    async def test_emergency_flatten_uses_reduce_only_ioc(self, settings, bot_state):
        """Emergency flatten orders must be reduce-only IOC."""
        from app.order_manager import OrderManager
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        from app.models import MarketSnapshot

        # Set up market data so flatten can compute price
        snap = MarketSnapshot(
            symbol="BTC", mid=Decimal("84000"),
            best_bid=Decimal("83990"), best_ask=Decimal("84010"),
            spread=Decimal("20"), spread_bps=Decimal("2"),
            imbalance=Decimal("0"), short_term_vol=Decimal("0"),
            updated_at=datetime.now(tz=timezone.utc),
        )
        await bot_state.update_market(snap)

        client = MagicMock()
        client.place_order.return_value = {
            "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}}
        }
        om = OrderManager(bot_state, settings, client)

        order = await om.submit_emergency_flatten(
            "BTC", Side.SELL, Decimal("0.001")
        )
        assert order is not None
        assert order.tif == TIF.IOC, "Emergency flatten must use IOC"
        assert order.reduce_only is True, "Emergency flatten must be reduce-only"
        assert order.kind == OrderKind.EMERGENCY_FLATTEN


class TestInventoryLimitEdgeCases:
    @pytest.mark.asyncio
    async def test_zero_position_never_hits_limit(self, settings, bot_state):
        """Flat position should never block orders."""
        from app.inventory_manager import InventoryManager
        pos = Position(symbol="BTC", size=Decimal("0"), avg_cost=Decimal("0"))
        await bot_state.update_position(pos)

        inv = InventoryManager(bot_state, settings)
        assert inv.is_inventory_limit_hit("BTC", Side.BUY) is False
        assert inv.is_inventory_limit_hit("BTC", Side.SELL) is False

    @pytest.mark.asyncio
    async def test_max_long_allows_short_entry(self, settings, bot_state):
        """At max long position, SELL (reducing) must still be allowed."""
        from app.inventory_manager import InventoryManager
        from datetime import datetime, timezone
        from app.models import MarketSnapshot

        mid = Decimal("84000")
        size = settings.max_position_usd_per_symbol / mid
        pos = Position(symbol="BTC", size=size, avg_cost=mid)
        await bot_state.update_position(pos)

        snap = MarketSnapshot(
            symbol="BTC", mid=mid, best_bid=mid - 10, best_ask=mid + 10,
            spread=Decimal("20"), spread_bps=Decimal("2"),
            imbalance=Decimal("0"), short_term_vol=Decimal("0"),
            updated_at=datetime.now(tz=timezone.utc),
        )
        await bot_state.update_market(snap)

        inv = InventoryManager(bot_state, settings)
        # Long at limit → BUY blocked, SELL allowed
        assert inv.is_inventory_limit_hit("BTC", Side.BUY) is True
        assert inv.is_inventory_limit_hit("BTC", Side.SELL) is False
