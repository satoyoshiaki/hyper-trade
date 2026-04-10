"""
Tests for dashboard API endpoints.

Uses FastAPI TestClient (httpx). Bot state is mocked with minimal state.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.api.app import create_dashboard_app
from app.persistence import Persistence
from app.state import BotState


@pytest_asyncio.fixture
async def client(settings, tmp_path):
    state = BotState(symbols=settings.symbols)
    db_path = tmp_path / "test.db"
    persistence = Persistence(db_path)
    persistence.initialize()
    app = create_dashboard_app(state, persistence, settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_has_no_secrets(self, client):
        res = await client.get("/health")
        body = res.text.lower()
        assert "private" not in body
        assert "secret" not in body
        assert "signature" not in body


class TestDashboardUi:
    @pytest.mark.asyncio
    async def test_index_contains_language_selector(self, client):
        res = await client.get("/")
        assert res.status_code == 200
        body = res.text
        assert 'id="language-select"' in body
        assert 'data-i18n="nav.overview"' in body

    def test_japanese_locale_file_is_present(self):
        locale_file = Path(__file__).resolve().parent.parent / "static" / "locales" / "ja.json"
        data = json.loads(locale_file.read_text(encoding="utf-8"))
        assert data["meta"]["defaultLocale"] == "ja"
        assert data["nav"]["overview"] == "概要"


class TestOverview:
    @pytest.mark.asyncio
    async def test_overview_returns_200(self, client):
        res = await client.get("/api/overview")
        assert res.status_code == 200

    @pytest.mark.asyncio
    async def test_overview_structure(self, client):
        res = await client.get("/api/overview")
        data = res.json()
        assert "bot_status" in data
        assert "kill_switch_active" in data
        assert "ws_connected" in data
        assert "testnet" in data

    @pytest.mark.asyncio
    async def test_overview_no_secrets(self, client):
        res = await client.get("/api/overview")
        body = res.text.lower()
        assert "private_key" not in body
        assert "0xaaaa" not in body  # wallet address shouldn't appear


class TestSymbols:
    @pytest.mark.asyncio
    async def test_symbols_returns_list(self, client):
        res = await client.get("/api/symbols")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) == 2  # BTC, ETH


class TestOrders:
    @pytest.mark.asyncio
    async def test_orders_returns_list(self, client):
        res = await client.get("/api/orders")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestFills:
    @pytest.mark.asyncio
    async def test_fills_returns_list(self, client):
        res = await client.get("/api/fills")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestPositions:
    @pytest.mark.asyncio
    async def test_positions_returns_list(self, client):
        res = await client.get("/api/positions")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert all("symbol" in p for p in data)


class TestPnL:
    @pytest.mark.asyncio
    async def test_pnl_returns_200(self, client):
        res = await client.get("/api/pnl")
        assert res.status_code == 200
        data = res.json()
        assert "realized_pnl" in data
        assert "unrealized_pnl" in data
        assert "fees_paid" in data


class TestRisk:
    @pytest.mark.asyncio
    async def test_risk_returns_200(self, client):
        res = await client.get("/api/risk")
        assert res.status_code == 200
        data = res.json()
        assert "kill_switch_active" in data
        assert "daily_loss_pct" in data

    @pytest.mark.asyncio
    async def test_risk_kill_switch_visible(self, settings, tmp_path):
        """Kill switch state must be visible in risk API."""
        from app.models import KillReason

        state = BotState(symbols=settings.symbols)
        await state.activate_kill_switch(KillReason.MANUAL, "test")
        db_path = tmp_path / "test2.db"
        persistence = Persistence(db_path)
        persistence.initialize()
        app = create_dashboard_app(state, persistence, settings)
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

        async with client:
            res = await client.get("/api/risk")
            data = res.json()
            assert data["kill_switch_active"] is True
            assert data["kill_switch_reason"] == "manual"


class TestEvents:
    @pytest.mark.asyncio
    async def test_events_returns_list(self, client):
        res = await client.get("/api/events")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestControlEndpoints:
    @pytest.mark.asyncio
    async def test_stop_endpoint_post_only(self, client):
        """GET on /api/stop must return 405 (method not allowed)."""
        res = await client.get("/api/stop")
        assert res.status_code == 405

    @pytest.mark.asyncio
    async def test_kill_endpoint_post_only(self, client):
        """GET on /api/kill must return 405."""
        res = await client.get("/api/kill")
        assert res.status_code == 405

    @pytest.mark.asyncio
    async def test_stop_activates_kill_switch(self, client):
        res = await client.post("/api/stop")
        assert res.status_code == 200
        data = res.json()
        assert data["accepted"] is True

    @pytest.mark.asyncio
    async def test_stop_returns_localized_message_for_japanese(self, client):
        res = await client.post("/api/stop", headers={"X-Dashboard-Language": "ja"})
        assert res.status_code == 200
        data = res.json()
        assert data["message"] == "正常停止を受け付けました。"
