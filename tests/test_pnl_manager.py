"""Tests for PnLManager."""

from decimal import Decimal

import pytest

from app.models import Fill, Position, Side
from app.pnl_manager import PnLManager


def make_pnl(settings, bot_state) -> PnLManager:
    return PnLManager(bot_state, settings)


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_sets_equity(self, settings, bot_state):
        pnl = make_pnl(settings, bot_state)
        await pnl.initialize(Decimal("1000"))
        assert bot_state.pnl.day_start_equity == Decimal("1000")
        assert bot_state.pnl.intraday_peak_equity == Decimal("1000")


class TestFeeTracking:
    @pytest.mark.asyncio
    async def test_fee_deducted_on_fill(self, settings, bot_state):
        await bot_state.update_position(
            Position(symbol="BTC", size=Decimal("0"), avg_cost=Decimal("0"))
        )
        pnl = make_pnl(settings, bot_state)
        fill = Fill(
            fill_id="f1", cloid=None, exchange_oid=None,
            symbol="BTC", side=Side.BUY,
            price=Decimal("84000"), size=Decimal("0.001"),
            fee=Decimal("0.1"),
        )
        await pnl.on_fill(fill)
        assert bot_state.pnl.fees_paid == Decimal("0.1")


class TestRealizedPnL:
    @pytest.mark.asyncio
    async def test_closing_long_position_realizes_pnl(self, settings, bot_state):
        # Set up long position
        pos = Position(symbol="BTC", size=Decimal("0.001"), avg_cost=Decimal("80000"))
        await bot_state.update_position(pos)

        pnl_mgr = make_pnl(settings, bot_state)
        # Sell fill closing the position at higher price
        fill = Fill(
            fill_id="f2", cloid=None, exchange_oid=None,
            symbol="BTC", side=Side.SELL,
            price=Decimal("84000"), size=Decimal("0.001"),
            fee=Decimal("0.1"),
        )
        await pnl_mgr.on_fill(fill)
        # Realized PnL = (84000 - 80000) * 0.001 = 4.0
        assert bot_state.pnl.realized_pnl == Decimal("4.0")


class TestDailyLossPct:
    @pytest.mark.asyncio
    async def test_daily_loss_pct_computed_correctly(self, settings, bot_state):
        pnl_mgr = make_pnl(settings, bot_state)
        await pnl_mgr.initialize(Decimal("1000"))
        # Manually set a loss
        bot_state.pnl.realized_pnl = Decimal("-25")
        # Daily loss = 25/1000 = 2.5%
        pct = pnl_mgr.get_daily_loss_pct()
        assert pct == Decimal("2.5")
