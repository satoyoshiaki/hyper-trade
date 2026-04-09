"""
Tests for BotState shared state management.

Focuses on:
- Lock-protected write safety
- Kill switch idempotency
- Snapshot isolation (dashboard reads don't affect bot state)
- Order state transitions
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import (
    BotStatus,
    Cloid,
    KillReason,
    Order,
    OrderKind,
    OrderStatus,
    Side,
    TIF,
)
from app.state import BotState


@pytest.fixture
def state(settings) -> BotState:
    return BotState(symbols=settings.symbols)


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_switch_initially_inactive(self, state):
        assert state.kill_switch_active is False
        assert state.kill_switch_reason is None

    @pytest.mark.asyncio
    async def test_activate_kill_switch(self, state):
        await state.activate_kill_switch(KillReason.MANUAL, "test")
        assert state.kill_switch_active is True
        assert state.kill_switch_reason == KillReason.MANUAL
        assert state.kill_switch_triggered_at is not None

    @pytest.mark.asyncio
    async def test_kill_switch_reason_not_overwritten(self, state):
        await state.activate_kill_switch(KillReason.MANUAL, "first")
        await state.activate_kill_switch(KillReason.DAILY_LOSS_EXCEEDED, "second")
        # First activation wins
        assert state.kill_switch_reason == KillReason.MANUAL

    @pytest.mark.asyncio
    async def test_kill_switch_adds_critical_event(self, state):
        await state.activate_kill_switch(KillReason.MANUAL, "test kill")
        events = list(state.events)
        assert any(e.event_type == "kill_switch" for e in events)
        from app.models import EventLevel
        assert any(e.level == EventLevel.CRITICAL for e in events)

    def test_is_kill_switch_active_sync(self, state):
        """Sync check for use inside bot loop."""
        assert state.is_kill_switch_active() is False


class TestBotStatus:
    @pytest.mark.asyncio
    async def test_initial_status_is_starting(self, state):
        assert state.status == BotStatus.STARTING

    @pytest.mark.asyncio
    async def test_set_status_running_sets_started_at(self, state):
        assert state.started_at is None
        await state.set_status(BotStatus.RUNNING)
        assert state.status == BotStatus.RUNNING
        assert state.started_at is not None

    @pytest.mark.asyncio
    async def test_started_at_not_overwritten_on_second_running(self, state):
        await state.set_status(BotStatus.RUNNING)
        first_start = state.started_at
        await state.set_status(BotStatus.PAUSED)
        await state.set_status(BotStatus.RUNNING)
        assert state.started_at == first_start


class TestOrderManagement:
    def _make_order(self, symbol: str = "BTC") -> Order:
        return Order(
            cloid=Cloid.generate(),
            symbol=symbol,
            side=Side.BUY,
            price=Decimal("84000"),
            size=Decimal("0.001"),
            tif=TIF.ALO,
            reduce_only=False,
            kind=OrderKind.MAKER_QUOTE,
            status=OrderStatus.PENDING,
        )

    @pytest.mark.asyncio
    async def test_add_order_appears_in_open_orders(self, state):
        order = self._make_order()
        await state.add_order(order)
        assert order.cloid.to_hex() in state.open_orders

    @pytest.mark.asyncio
    async def test_filled_order_moves_to_recent(self, state):
        order = self._make_order()
        await state.add_order(order)
        order.status = OrderStatus.FILLED
        await state.update_order(order)
        assert order.cloid.to_hex() not in state.open_orders
        assert order in list(state.recent_orders)

    @pytest.mark.asyncio
    async def test_canceled_order_moves_to_recent(self, state):
        order = self._make_order()
        await state.add_order(order)
        order.status = OrderStatus.CANCELED
        await state.update_order(order)
        assert order.cloid.to_hex() not in state.open_orders

    @pytest.mark.asyncio
    async def test_open_order_stays_in_open(self, state):
        order = self._make_order()
        await state.add_order(order)
        order.status = OrderStatus.OPEN
        await state.update_order(order)
        assert order.cloid.to_hex() in state.open_orders


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_is_deep_copy(self, state):
        """Modifying snapshot must not affect original state."""
        snap = await state.snapshot()
        snap.kill_switch_active = True
        # Original should be unaffected
        assert state.kill_switch_active is False

    @pytest.mark.asyncio
    async def test_snapshot_reflects_current_state(self, state):
        await state.set_status(BotStatus.RUNNING)
        snap = await state.snapshot()
        assert snap.status == BotStatus.RUNNING


class TestReconnectTracking:
    @pytest.mark.asyncio
    async def test_reconnect_increments_count(self, state):
        assert state.risk.reconnect_count == 0
        await state.record_reconnect()
        assert state.risk.reconnect_count == 1

    @pytest.mark.asyncio
    async def test_multiple_reconnects_tracked(self, state):
        await state.record_reconnect()
        await state.record_reconnect()
        await state.record_reconnect()
        assert state.risk.reconnect_count == 3
        assert len(state.ws_reconnect_times) == 3
