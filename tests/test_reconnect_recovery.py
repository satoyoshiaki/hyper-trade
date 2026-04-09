"""
Reconnect state recovery tests.

These tests verify the DESIGN INTENT for reconnect recovery.
Full end-to-end recovery (actual WS reconnect) requires network access
and is marked as integration tests (skipped in normal CI).

What is tested here:
- State consistency checks that run after reconnect
- Kill switch trigger when reconnect storm is detected
- Order reconciliation logic skeleton

What is NOT tested (marked explicitly):
- Actual WebSocket reconnect over the network (integration)
- Order state reconciliation against live exchange (integration)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.kill_switch import KillSwitch
from app.models import KillReason
from app.risk_manager import RiskManager
from app.state import BotState


class TestReconnectStormDetection:
    """Kill switch must trigger when WS reconnects too frequently."""

    @pytest.mark.asyncio
    async def test_reconnect_storm_triggers_kill(self, settings, bot_state):
        """
        If max_reconnect_streak reconnects happen within reconnect_window_s,
        risk_manager.compute_kill_conditions() must return RECONNECT_STORM.
        """
        risk = RiskManager(bot_state, settings)

        # Simulate reconnects within the window
        now = datetime.now(tz=timezone.utc)
        for i in range(settings.max_reconnect_streak):
            await bot_state.record_reconnect(at=now - timedelta(seconds=i * 5))

        reasons = risk.compute_kill_conditions()
        assert KillReason.RECONNECT_STORM in reasons

    @pytest.mark.asyncio
    async def test_old_reconnects_not_counted_in_streak(self, settings, bot_state):
        """
        Reconnects older than reconnect_window_s must not count toward the streak.
        """
        risk = RiskManager(bot_state, settings)

        # All reconnects are older than the window
        old = datetime.now(tz=timezone.utc) - timedelta(
            seconds=settings.reconnect_window_s + 10
        )
        for _ in range(settings.max_reconnect_streak + 5):
            await bot_state.record_reconnect(at=old)

        reasons = risk.compute_kill_conditions()
        assert KillReason.RECONNECT_STORM not in reasons

    @pytest.mark.asyncio
    async def test_reconnect_storm_activates_kill_switch(self, settings, bot_state):
        """Full path: storm detected → kill_switch.run() would activate."""
        risk = RiskManager(bot_state, settings)
        ks = KillSwitch(bot_state, settings, risk)

        now = datetime.now(tz=timezone.utc)
        for i in range(settings.max_reconnect_streak):
            await bot_state.record_reconnect(at=now - timedelta(seconds=i))

        # Manually trigger as kill_switch.run() would
        reasons = risk.compute_kill_conditions()
        assert reasons  # should have at least RECONNECT_STORM
        await ks._trigger(reasons[0], "test storm")
        assert bot_state.kill_switch_active is True


class TestStateAfterReconnect:
    """
    Verify that state is safe to trade after reconnect.

    DESIGN INTENT: On WS reconnect, the bot should:
    1. Fetch open orders from exchange (REST) and reconcile with local state
    2. Fetch current positions and update inventory
    3. Only resume quoting after state is confirmed consistent

    This class tests the individual pieces of that flow.
    """

    @pytest.mark.asyncio
    async def test_open_orders_can_be_cleared_on_reconnect(self, settings, bot_state):
        """
        Local open_orders can be cleared and rebuilt from exchange data
        after reconnect. State transitions must work correctly.
        """
        from app.models import Cloid, Order, OrderKind, OrderStatus, Side, TIF

        # Simulate pre-reconnect open order
        cloid = Cloid.generate()
        order = Order(
            cloid=cloid,
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("84000"),
            size=Decimal("0.001"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.OPEN,
        )
        await bot_state.add_order(order)
        assert cloid.to_hex() in bot_state.open_orders

        # On reconnect: cancel stale local orders (exchange will be authoritative)
        order.status = OrderStatus.CANCELED
        await bot_state.update_order(order)
        assert cloid.to_hex() not in bot_state.open_orders

    @pytest.mark.asyncio
    async def test_position_update_from_exchange_is_authoritative(
        self, settings, bot_state
    ):
        """
        Position from exchange REST call overwrites local estimate.
        This is the correct behaviour after reconnect.
        """
        from app.models import Position

        # Set local position
        local_pos = Position(symbol="BTC", size=Decimal("0.001"), avg_cost=Decimal("84000"))
        await bot_state.update_position(local_pos)

        # Simulate exchange-authoritative position (different size)
        exchange_pos = Position(symbol="BTC", size=Decimal("0.0005"), avg_cost=Decimal("83500"))
        await bot_state.update_position(exchange_pos)

        assert bot_state.symbols["BTC"].position.size == Decimal("0.0005")


class TestReconnectIntegration:
    """
    Integration tests for actual reconnect behavior.
    These require a live connection to Hyperliquid testnet.
    """

    @pytest.mark.integration
    def test_ws_reconnect_and_resume_quoting(self):
        """
        UNTESTED (integration): Simulate WS drop and verify:
        1. WSClient reconnects automatically
        2. Market data resumes within stale_data_threshold_ms
        3. Quoting resumes after book snapshot is received
        4. No duplicate orders are placed

        Reason not tested: requires live network and exchange connection.
        """
        pytest.skip("Integration test — requires live testnet connection")

    @pytest.mark.integration
    def test_order_reconciliation_after_reconnect(self):
        """
        UNTESTED (integration): After reconnect:
        1. Fetch open_orders from exchange
        2. Cross-reference with local open_orders
        3. Orders present on exchange but not locally → add to local state
        4. Orders present locally but not on exchange → mark as canceled/filled

        Reason not tested: requires live exchange API access.
        """
        pytest.skip("Integration test — requires live testnet connection")
