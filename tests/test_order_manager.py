"""Tests for OrderManager (mocked exchange client)."""

from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

import pytest

from app.models import Cloid, Order, OrderKind, OrderStatus, Side, TIF
from app.order_manager import OrderManager


def make_mock_client():
    client = MagicMock()
    client.place_order.return_value = {
        "response": {"data": {"statuses": [{"resting": {"oid": 999}}]}}
    }
    client.cancel_by_cloid.return_value = {"status": "ok"}
    return client


def make_om(settings, bot_state, client=None):
    return OrderManager(bot_state, settings, client or make_mock_client())


async def set_btc_market(bot_state):
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


class TestSubmitMakerOrder:
    @pytest.mark.asyncio
    async def test_submit_returns_order(self, settings, bot_state):
        await set_btc_market(bot_state)
        om = make_om(settings, bot_state)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is not None
        assert order.status == OrderStatus.OPEN
        assert order.tif == TIF.ALO  # must be post-only

    @pytest.mark.asyncio
    async def test_submit_blocked_by_kill_switch(self, settings, bot_state):
        import asyncio
        from app.models import KillReason
        await bot_state.activate_kill_switch(KillReason.MANUAL, "test")
        om = make_om(settings, bot_state)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is None

    @pytest.mark.asyncio
    async def test_rejected_order_marked_rejected(self, settings, bot_state):
        client = make_mock_client()
        from app.exchange.client import ExchangeClientError
        client.place_order.side_effect = ExchangeClientError("Rejected by exchange")
        om = make_om(settings, bot_state, client)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is None
        # Check that the order in recent_orders is REJECTED
        rejected = [o for o in bot_state.recent_orders if o.status == OrderStatus.REJECTED]
        assert len(rejected) == 1


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancel_open_order(self, settings, bot_state):
        om = make_om(settings, bot_state)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is not None
        success = await om.cancel_order(order.cloid)
        assert success is True
        # Should be removed from open_orders
        assert order.cloid.to_hex() not in bot_state.open_orders

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_order_returns_false(self, settings, bot_state):
        om = make_om(settings, bot_state)
        fake_cloid = Cloid.generate()
        success = await om.cancel_order(fake_cloid)
        assert success is False

    @pytest.mark.asyncio
    async def test_double_cancel_prevented(self, settings, bot_state):
        client = make_mock_client()
        om = make_om(settings, bot_state, client)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is not None
        await om.cancel_order(order.cloid)
        # Second cancel: order not in open_orders anymore → should return False
        success2 = await om.cancel_order(order.cloid)
        assert success2 is False


class TestFillProcessing:
    @pytest.mark.asyncio
    async def test_fill_updates_order_status(self, settings, bot_state):
        om = make_om(settings, bot_state)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.001"))
        assert order is not None

        from app.models import Fill
        fill = Fill(
            fill_id="f1",
            cloid=order.cloid,
            exchange_oid=order.exchange_oid,
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("83990"),
            size=Decimal("0.001"),
            fee=Decimal("0.05"),
        )
        await om.process_fill(fill)
        # Order should now be FILLED and in recent_orders
        filled = [o for o in bot_state.recent_orders if o.status == OrderStatus.FILLED]
        assert len(filled) == 1

    @pytest.mark.asyncio
    async def test_partial_fill_sets_partially_filled(self, settings, bot_state):
        om = make_om(settings, bot_state)
        order = await om.submit_maker_order("BTC", Side.BUY, Decimal("83990"), Decimal("0.002"))
        assert order is not None

        from app.models import Fill
        fill = Fill(
            fill_id="f2",
            cloid=order.cloid,
            exchange_oid=order.exchange_oid,
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("83990"),
            size=Decimal("0.001"),   # only half
            fee=Decimal("0.05"),
        )
        await om.process_fill(fill)
        # Order should be in open_orders with PARTIALLY_FILLED status
        updated = bot_state.open_orders.get(order.cloid.to_hex())
        assert updated is not None
        assert updated.status == OrderStatus.PARTIALLY_FILLED
