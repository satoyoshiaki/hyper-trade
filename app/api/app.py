"""
FastAPI application factory for the dashboard.

Security:
- Default bind is 127.0.0.1 (enforced in main.py, not here)
- Logs a warning if bound to a non-loopback address
- No auth in v1 — external bind is explicitly discouraged
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.dashboard_state import DashboardState
from app.api.routes import router, setup_routes
from app.persistence import Persistence
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent


def create_dashboard_app(
    state: BotState,
    persistence: Persistence,
    settings: Settings,
) -> FastAPI:
    """Create and configure the dashboard FastAPI app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("Dashboard API started.")
        yield
        log.info("Dashboard API stopped.")

    app = FastAPI(
        title="HL Maker Bot Dashboard",
        description="Read-only dashboard for Hyperliquid maker bot",
        version="0.1.0",
        docs_url="/docs",     # Swagger UI — only accessible on localhost
        redoc_url=None,
        lifespan=lifespan,
    )

    # Static files and templates
    static_dir = _BASE_DIR / "static"
    templates_dir = _BASE_DIR / "templates"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates = Jinja2Templates(directory=str(templates_dir))

    # Dashboard state bridge
    dashboard_state = DashboardState(state, persistence, settings)

    # Wire up routes
    setup_routes(dashboard_state, settings, templates)
    app.include_router(router)

    return app
