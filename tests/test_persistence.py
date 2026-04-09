"""Tests for SQLite persistence layer."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.models import BotEvent, EventLevel, Fill, Order, OrderKind, OrderStatus, Side, TIF, Cloid
from app.persistence import Persistence


@pytest.fixture
def db(tmp_path) -> Persistence:
    p = Persistence(tmp_path / "test.db")
    p.initialize()
    return p


class TestFills:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_fill(self, db):
        fill = Fill(
            fill_id="test-fill-1",
            cloid=None,
            exchange_oid=12345,
            symbol="BTC",
            side=Side.BUY,
            price=Decimal("84000"),
            size=Decimal("0.001"),
            fee=Decimal("0.05"),
        )
        await db.save_fill(fill)
        fills = await db.get_recent_fills(limit=10)
        assert len(fills) == 1
        assert fills[0]["fill_id"] == "test-fill-1"
        assert fills[0]["symbol"] == "BTC"

    @pytest.mark.asyncio
    async def test_duplicate_fill_ignored(self, db):
        fill = Fill(
            fill_id="dup-fill",
            cloid=None,
            exchange_oid=None,
            symbol="ETH",
            side=Side.SELL,
            price=Decimal("2100"),
            size=Decimal("0.01"),
            fee=Decimal("0.01"),
        )
        await db.save_fill(fill)
        await db.save_fill(fill)  # second insert should be ignored
        fills = await db.get_recent_fills(limit=10)
        assert len(fills) == 1


class TestEvents:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_event(self, db):
        event = BotEvent(
            level=EventLevel.CRITICAL,
            event_type="kill_switch",
            message="test kill",
        )
        await db.save_event(event)
        events = await db.get_recent_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "kill_switch"
        assert events[0]["level"] == "critical"


class TestPnLSnapshots:
    @pytest.mark.asyncio
    async def test_save_pnl_snapshot(self, db):
        await db.save_pnl_snapshot(
            realized=Decimal("10.5"),
            unrealized=Decimal("2.3"),
            fees=Decimal("0.5"),
        )
        # No exception = pass


class TestSchema:
    def test_multiple_initialize_is_safe(self, tmp_path):
        """Calling initialize() twice should not error (IF NOT EXISTS)."""
        p = Persistence(tmp_path / "multi.db")
        p.initialize()
        p.initialize()  # should not raise
