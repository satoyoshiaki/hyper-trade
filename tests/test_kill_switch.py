"""Tests for KillSwitch."""

from decimal import Decimal

import pytest

from app.kill_switch import KillSwitch
from app.models import KillReason
from app.risk_manager import RiskManager


def make_ks(settings, bot_state) -> KillSwitch:
    risk = RiskManager(bot_state, settings)
    return KillSwitch(bot_state, settings, risk)


class TestManualKill:
    @pytest.mark.asyncio
    async def test_manual_kill_activates_switch(self, settings, bot_state):
        ks = make_ks(settings, bot_state)
        await ks.manual_kill("test")
        assert bot_state.kill_switch_active is True
        assert bot_state.kill_switch_reason == KillReason.MANUAL

    @pytest.mark.asyncio
    async def test_manual_kill_is_idempotent(self, settings, bot_state):
        ks = make_ks(settings, bot_state)
        await ks.manual_kill("first")
        reason_first = bot_state.kill_switch_reason
        await ks.manual_kill("second")
        # Reason should not be overwritten
        assert bot_state.kill_switch_reason == reason_first


class TestFlattenCallback:
    @pytest.mark.asyncio
    async def test_flatten_callback_called_on_manual_kill(self, settings, bot_state):
        flatten_called = []

        async def mock_flatten():
            flatten_called.append(True)

        ks = make_ks(settings, bot_state)
        ks.set_flatten_callback(mock_flatten)
        await ks.manual_kill("test")
        # Flatten should be called for MANUAL reason when emergency_flatten_enabled
        assert len(flatten_called) == 1

    @pytest.mark.asyncio
    async def test_flatten_callback_not_called_for_stale_data(self, settings, bot_state):
        """Stale data kill does NOT trigger flatten (we might just need to reconnect)."""
        flatten_called = []

        async def mock_flatten():
            flatten_called.append(True)

        ks = make_ks(settings, bot_state)
        ks.set_flatten_callback(mock_flatten)
        await ks._trigger(KillReason.STALE_MARKET_DATA, "test stale")
        assert len(flatten_called) == 0


class TestKillEventLogged:
    @pytest.mark.asyncio
    async def test_kill_event_added_to_state(self, settings, bot_state):
        ks = make_ks(settings, bot_state)
        await ks.manual_kill("test reason")
        events = list(bot_state.events)
        assert any(e.event_type == "kill_switch" for e in events)
