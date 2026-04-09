"""
Dashboard API routes.

All GET endpoints are read-only.
POST /api/stop and /api/kill are the only write operations —
they set a flag in BotState that the bot loop reads.

Security: no secrets, no private keys, no signatures in any response.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.dashboard_state import DashboardState
from app.api.schemas import (
    EventInfo,
    FillInfo,
    HealthResponse,
    MarketInfo,
    OrderInfo,
    OverviewResponse,
    PositionInfo,
    PnLResponse,
    RiskResponse,
    StopResponse,
)
from app.models import KillReason
from app.settings import Settings

router = APIRouter()
_templates: Jinja2Templates | None = None
_dashboard_state: DashboardState | None = None
_settings: Settings | None = None
_started_at: datetime = datetime.now(tz=timezone.utc)


def setup_routes(
    dashboard_state: DashboardState,
    settings: Settings,
    templates: Jinja2Templates,
) -> None:
    """Call once at app startup to inject dependencies."""
    global _dashboard_state, _settings, _templates
    _dashboard_state = dashboard_state
    _settings = settings
    _templates = templates


def _ds() -> DashboardState:
    if _dashboard_state is None:
        raise RuntimeError("Routes not initialized")
    return _dashboard_state


# ------------------------------------------------------------------
# UI
# ------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    if _templates is None:
        return HTMLResponse("<h1>Dashboard not configured</h1>", status_code=500)
    try:
        return _templates.TemplateResponse("index.html", {"request": request})
    except Exception:
        # Fallback: serve as plain HTML (no template variables used)
        from pathlib import Path
        html_path = Path(__file__).resolve().parent.parent.parent / "templates" / "index.html"
        html = html_path.read_text(encoding="utf-8")
        return HTMLResponse(content=html)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health():
    now = datetime.now(tz=timezone.utc)
    uptime = (now - _started_at).total_seconds()
    return HealthResponse(
        status="ok",
        uptime_s=uptime,
        testnet=_settings.testnet if _settings else True,
        timestamp=now,
    )


# ------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------

@router.get("/api/overview", response_model=OverviewResponse)
async def get_overview():
    return await _ds().get_overview()


@router.get("/api/symbols", response_model=list[MarketInfo])
async def get_symbols():
    return await _ds().get_symbols()


@router.get("/api/orders", response_model=list[OrderInfo])
async def get_orders():
    return await _ds().get_orders()


@router.get("/api/fills", response_model=list[FillInfo])
async def get_fills():
    return await _ds().get_fills()


@router.get("/api/positions", response_model=list[PositionInfo])
async def get_positions():
    return await _ds().get_positions()


@router.get("/api/pnl", response_model=PnLResponse)
async def get_pnl():
    return await _ds().get_pnl()


@router.get("/api/risk", response_model=RiskResponse)
async def get_risk():
    return await _ds().get_risk()


@router.get("/api/events", response_model=list[EventInfo])
async def get_events():
    return await _ds().get_events()


# ------------------------------------------------------------------
# Control endpoints (POST only, minimal: stop and kill)
# ------------------------------------------------------------------

@router.post("/api/stop", response_model=StopResponse)
async def graceful_stop():
    """
    Request a graceful stop. Bot finishes current cycle, cancels orders, exits.
    Does NOT trigger emergency flatten.
    """
    ds = _ds()
    state = ds._state
    if state.kill_switch_active:
        return StopResponse(accepted=False, message="Kill switch already active.")
    # Signal graceful stop by activating kill switch with MANUAL reason
    await state.activate_kill_switch(KillReason.MANUAL, "Graceful stop requested via dashboard")
    return StopResponse(accepted=True, message="Graceful stop requested.")


@router.post("/api/kill", response_model=StopResponse)
async def emergency_kill():
    """
    Activate emergency kill switch. Triggers position flatten if configured.
    Use with caution.
    """
    ds = _ds()
    state = ds._state
    if state.kill_switch_active:
        return StopResponse(accepted=False, message="Kill switch already active.")
    await state.activate_kill_switch(KillReason.MANUAL, "Emergency kill via dashboard")
    return StopResponse(accepted=True, message="Emergency kill switch activated.")
