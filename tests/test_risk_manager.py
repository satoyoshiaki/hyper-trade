"""Tests for RiskManager."""

from decimal import Decimal

import pytest

from app.models import MarketSnapshot, PnLState, Position, Side
from app.risk_manager import RiskCheckFailed, RiskManager
from app.state import BotState

from datetime import datetime, timezone


def make_risk(settings, state) -> RiskManager:
    return RiskManager(state, settings)


def set_btc_market(state: BotState, mid=Decimal("84000"), spread_bps=Decimal("2")):
    import asyncio
    from app.models import MarketSnapshot
    snap = MarketSnapshot(
        symbol="BTC",
        mid=mid,
        best_bid=mid - Decimal("10"),
        best_ask=mid + Decimal("10"),
        spread=Decimal("20"),
        spread_bps=spread_bps,
        imbalance=Decimal("0"),
        short_term_vol=Decimal("0"),
        updated_at=datetime.now(tz=timezone.utc),
        stale=False,
        abrupt_move=False,
        book_corrupted=False,
    )
    asyncio.get_event_loop().run_until_complete(state.update_market(snap))


class TestOrderSizeLimit:
    def test_order_size_at_limit_passes(self, settings, bot_state):
        set_btc_market(bot_state)
        risk = make_risk(settings, bot_state)
        # Should not raise
        risk.check_order("BTC", Side.BUY, settings.max_order_size_usd)

    def test_order_size_over_limit_raises(self, settings, bot_state):
        set_btc_market(bot_state)
        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, settings.max_order_size_usd + Decimal("1"))
        assert "order_size_usd" in exc.value.reason

    def test_kill_switch_active_blocks_order(self, settings, bot_state):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            bot_state.activate_kill_switch(
                __import__("app.models", fromlist=["KillReason"]).KillReason.MANUAL, "test"
            )
        )
        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "kill_switch_active" in exc.value.reason


class TestLossLimits:
    def test_daily_loss_limit_triggers(self, settings, bot_state):
        import asyncio
        set_btc_market(bot_state)
        pnl = bot_state.pnl
        pnl.day_start_equity = Decimal("1000")
        pnl.intraday_peak_equity = Decimal("1000")
        # Loss of 3% > max 2%
        pnl.realized_pnl = Decimal("-30")
        pnl.fees_paid = Decimal("0")
        asyncio.get_event_loop().run_until_complete(bot_state.update_pnl(pnl))

        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "daily_loss_pct" in exc.value.reason

    def test_intraday_drawdown_limit_triggers(self, settings, bot_state):
        import asyncio
        set_btc_market(bot_state)
        pnl = bot_state.pnl
        pnl.day_start_equity = Decimal("1000")
        pnl.intraday_peak_equity = Decimal("1000")
        # Drawdown of 1.5% > max 1%
        pnl.realized_pnl = Decimal("-15")
        pnl.fees_paid = Decimal("0")
        asyncio.get_event_loop().run_until_complete(bot_state.update_pnl(pnl))

        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "intraday_drawdown_pct" in exc.value.reason


class TestMarketQuality:
    def test_stale_market_blocks_order(self, settings, bot_state):
        import asyncio
        from app.models import MarketSnapshot
        snap = MarketSnapshot(
            symbol="BTC", mid=Decimal("84000"), best_bid=Decimal("83990"),
            best_ask=Decimal("84010"), spread=Decimal("20"),
            spread_bps=Decimal("2"), imbalance=Decimal("0"),
            short_term_vol=Decimal("0"),
            updated_at=datetime.now(tz=timezone.utc),
            stale=True, abrupt_move=False, book_corrupted=False,
        )
        asyncio.get_event_loop().run_until_complete(bot_state.update_market(snap))

        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "stale_market_data" in exc.value.reason

    def test_book_corrupted_blocks_order(self, settings, bot_state):
        import asyncio
        from app.models import MarketSnapshot
        snap = MarketSnapshot(
            symbol="BTC", mid=Decimal("84000"), best_bid=Decimal("84000"),
            best_ask=Decimal("83990"), spread=Decimal("-10"),
            spread_bps=Decimal("-1"), imbalance=Decimal("0"),
            short_term_vol=Decimal("0"),
            updated_at=datetime.now(tz=timezone.utc),
            stale=False, abrupt_move=False, book_corrupted=True,
        )
        asyncio.get_event_loop().run_until_complete(bot_state.update_market(snap))

        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "book_corrupted" in exc.value.reason


class TestRejectStreak:
    def test_reject_streak_blocks_order(self, settings, bot_state):
        set_btc_market(bot_state)
        bot_state.risk.consecutive_rejects = settings.max_reject_streak
        risk = make_risk(settings, bot_state)
        with pytest.raises(RiskCheckFailed) as exc:
            risk.check_order("BTC", Side.BUY, Decimal("10"))
        assert "consecutive_rejects" in exc.value.reason

    def test_record_success_resets_streak(self, settings, bot_state):
        bot_state.risk.consecutive_rejects = 5
        risk = make_risk(settings, bot_state)
        risk.record_success()
        assert bot_state.risk.consecutive_rejects == 0

    def test_record_reject_increments_streak(self, settings, bot_state):
        bot_state.risk.consecutive_rejects = 0
        risk = make_risk(settings, bot_state)
        risk.record_reject()
        assert bot_state.risk.consecutive_rejects == 1


class TestEmergencyBypass:
    def test_emergency_order_bypasses_kill_switch(self, settings, bot_state):
        """is_emergency=True should bypass kill switch check."""
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            bot_state.activate_kill_switch(
                __import__("app.models", fromlist=["KillReason"]).KillReason.MANUAL, "test"
            )
        )
        risk = make_risk(settings, bot_state)
        # Should NOT raise for emergency orders
        risk.check_order("BTC", Side.SELL, Decimal("10"), is_emergency=True)
