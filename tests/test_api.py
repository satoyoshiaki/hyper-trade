"""
Tests for dashboard API endpoints.

Uses FastAPI TestClient (httpx). Bot state is mocked with minimal state.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_dashboard_app
from app.persistence import Persistence
from app.state import BotState


@pytest.fixture
def client(settings, tmp_path):
    state = BotState(symbols=settings.symbols)
    db_path = tmp_path / "test.db"
    persistence = Persistence(db_path)
    persistence.initialize()
    app = create_dashboard_app(state, persistence, settings)
    return TestClient(app)


class TestHealth:
    def test_health_returns_ok(self, client):
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"

    def test_health_has_no_secrets(self, client):
        res = client.get("/health")
        body = res.text.lower()
        assert "private" not in body
        assert "secret" not in body
        assert "signature" not in body


class TestOverview:
    def test_overview_returns_200(self, client):
        res = client.get("/api/overview")
        assert res.status_code == 200

    def test_overview_structure(self, client):
        res = client.get("/api/overview")
        data = res.json()
        assert "bot_status" in data
        assert "kill_switch_active" in data
        assert "ws_connected" in data
        assert "testnet" in data

    def test_overview_no_secrets(self, client):
        res = client.get("/api/overview")
        body = res.text.lower()
        assert "private_key" not in body
        assert "0xaaaa" not in body  # wallet address shouldn't appear


class TestSymbols:
    def test_symbols_returns_list(self, client):
        res = client.get("/api/symbols")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) == 2  # BTC, ETH


class TestOrders:
    def test_orders_returns_list(self, client):
        res = client.get("/api/orders")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestFills:
    def test_fills_returns_list(self, client):
        res = client.get("/api/fills")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestPositions:
    def test_positions_returns_list(self, client):
        res = client.get("/api/positions")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert all("symbol" in p for p in data)


class TestPnL:
    def test_pnl_returns_200(self, client):
        res = client.get("/api/pnl")
        assert res.status_code == 200
        data = res.json()
        assert "realized_pnl" in data
        assert "unrealized_pnl" in data
        assert "fees_paid" in data


class TestRisk:
    def test_risk_returns_200(self, client):
        res = client.get("/api/risk")
        assert res.status_code == 200
        data = res.json()
        assert "kill_switch_active" in data
        assert "daily_loss_pct" in data

    def test_risk_kill_switch_visible(self, settings, tmp_path):
        """Kill switch state must be visible in risk API."""
        import asyncio
        from app.models import KillReason

        state = BotState(symbols=settings.symbols)
        asyncio.get_event_loop().run_until_complete(
            state.activate_kill_switch(KillReason.MANUAL, "test")
        )
        db_path = tmp_path / "test2.db"
        persistence = Persistence(db_path)
        persistence.initialize()
        app = create_dashboard_app(state, persistence, settings)
        client = TestClient(app)

        res = client.get("/api/risk")
        data = res.json()
        assert data["kill_switch_active"] is True
        assert data["kill_switch_reason"] == "manual"


class TestEvents:
    def test_events_returns_list(self, client):
        res = client.get("/api/events")
        assert res.status_code == 200
        assert isinstance(res.json(), list)


class TestControlEndpoints:
    def test_stop_endpoint_post_only(self, client):
        """GET on /api/stop must return 405 (method not allowed)."""
        res = client.get("/api/stop")
        assert res.status_code == 405

    def test_kill_endpoint_post_only(self, client):
        """GET on /api/kill must return 405."""
        res = client.get("/api/kill")
        assert res.status_code == 405

    def test_stop_activates_kill_switch(self, client):
        res = client.post("/api/stop")
        assert res.status_code == 200
        data = res.json()
        assert data["accepted"] is True
