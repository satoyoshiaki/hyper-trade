"""
SQLite persistence layer.

Tables:
  orders            - order history (terminal states)
  fills             - all fills received
  pnl_snapshots     - periodic PnL snapshots
  inventory_snapshots - periodic position snapshots
  events            - bot events and errors

Design:
  - WAL mode for concurrent reads while bot writes
  - sync writes (asyncio.to_thread for blocking ops)
  - bot startup restores last known state from DB
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.models import BotEvent, Fill, Order, Position
from app.telemetry import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    cloid TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    tif TEXT NOT NULL,
    reduce_only INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    exchange_oid INTEGER,
    filled_size TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    cloid TEXT,
    exchange_oid INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    fee TEXT NOT NULL,
    fee_token TEXT NOT NULL DEFAULT 'USDC',
    filled_at TEXT NOT NULL,
    is_maker INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    realized_pnl TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    fees_paid TEXT NOT NULL,
    snapshot_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    size TEXT NOT NULL,
    avg_cost TEXT NOT NULL,
    snapshot_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    symbol TEXT,
    detail_json TEXT,
    occurred_at TEXT NOT NULL
);
"""

# Retention: purge records older than these days
_FILL_RETENTION_DAYS = 30
_SNAPSHOT_RETENTION_DAYS = 7
_EVENT_RETENTION_DAYS = 14


class Persistence:
    """
    SQLite-backed persistence for the bot.

    All write methods are synchronous internally but wrapped in
    asyncio.to_thread so they don't block the event loop.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def initialize(self) -> None:
        """Create DB and apply schema. Call at startup (sync)."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
            conn.commit()
        log.info("SQLite initialized at %s", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def save_order(self, order: Order) -> None:
        self._save_order_sync(order)

    def _save_order_sync(self, order: Order) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO orders
                  (cloid, symbol, side, price, size, tif, reduce_only, kind,
                   status, exchange_oid, filled_size, created_at, updated_at, reject_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    order.cloid.to_hex(),
                    order.symbol,
                    order.side.value,
                    str(order.price),
                    str(order.size),
                    order.tif.value,
                    int(order.reduce_only),
                    order.kind.value,
                    order.status.value,
                    order.exchange_oid,
                    str(order.filled_size),
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                    order.reject_reason,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    async def save_fill(self, fill: Fill) -> None:
        self._save_fill_sync(fill)

    def _save_fill_sync(self, fill: Fill) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO fills
                  (fill_id, cloid, exchange_oid, symbol, side, price, size,
                   fee, fee_token, filled_at, is_maker)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fill.fill_id,
                    fill.cloid.to_hex() if fill.cloid else None,
                    fill.exchange_oid,
                    fill.symbol,
                    fill.side.value,
                    str(fill.price),
                    str(fill.size),
                    str(fill.fee),
                    fill.fee_token,
                    fill.filled_at.isoformat(),
                    int(fill.is_maker),
                ),
            )
            conn.commit()

    async def get_recent_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get_recent_fills_sync(limit)

    def _get_recent_fills_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # PnL snapshots
    # ------------------------------------------------------------------

    async def save_pnl_snapshot(
        self,
        realized: Decimal,
        unrealized: Decimal,
        fees: Decimal,
    ) -> None:
        self._save_pnl_sync(realized, unrealized, fees)

    def _save_pnl_sync(
        self, realized: Decimal, unrealized: Decimal, fees: Decimal
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pnl_snapshots (realized_pnl, unrealized_pnl, fees_paid, snapshot_at)
                VALUES (?,?,?,?)
                """,
                (str(realized), str(unrealized), str(fees), datetime.now(tz=timezone.utc).isoformat()),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Inventory snapshots
    # ------------------------------------------------------------------

    async def save_inventory_snapshot(self, position: Position) -> None:
        self._save_inventory_sync(position)

    def _save_inventory_sync(self, position: Position) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inventory_snapshots (symbol, size, avg_cost, snapshot_at)
                VALUES (?,?,?,?)
                """,
                (
                    position.symbol,
                    str(position.size),
                    str(position.avg_cost),
                    datetime.now(tz=timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def save_event(self, event: BotEvent) -> None:
        self._save_event_sync(event)

    def _save_event_sync(self, event: BotEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO events
                  (event_id, level, event_type, message, symbol, detail_json, occurred_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.level.value,
                    event.event_type,
                    event.message,
                    event.symbol,
                    json.dumps(event.detail) if event.detail else None,
                    event.occurred_at.isoformat(),
                ),
            )
            conn.commit()

    async def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._get_recent_events_sync(limit)

    def _get_recent_events_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY occurred_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def purge_old_records(self) -> None:
        """Delete records older than retention periods. Call daily."""
        self._purge_sync()

    def _purge_sync(self) -> None:
        from datetime import timedelta
        now = datetime.now(tz=timezone.utc)
        fills_cutoff = (now - timedelta(days=_FILL_RETENTION_DAYS)).isoformat()
        snap_cutoff = (now - timedelta(days=_SNAPSHOT_RETENTION_DAYS)).isoformat()
        event_cutoff = (now - timedelta(days=_EVENT_RETENTION_DAYS)).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM fills WHERE filled_at < ?", (fills_cutoff,))
            conn.execute("DELETE FROM pnl_snapshots WHERE snapshot_at < ?", (snap_cutoff,))
            conn.execute("DELETE FROM inventory_snapshots WHERE snapshot_at < ?", (snap_cutoff,))
            conn.execute("DELETE FROM events WHERE occurred_at < ?", (event_cutoff,))
            conn.commit()
        log.debug("Old records purged.")
