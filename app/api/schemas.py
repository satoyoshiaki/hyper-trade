"""
Pydantic response models for the dashboard API.

Security: these models must NEVER include private_key, signature, or secrets.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    uptime_s: Optional[float]
    version: str = "0.1.0"
    testnet: bool
    timestamp: datetime


class MarketInfo(BaseModel):
    symbol: str
    mid: Optional[str]
    spread_bps: Optional[str]
    imbalance: Optional[str]
    short_term_vol: Optional[str]
    stale: bool
    abrupt_move: bool
    book_corrupted: bool
    quoting: bool
    bid_quote: Optional[str]
    ask_quote: Optional[str]
    bid_size: Optional[str]
    ask_size: Optional[str]
    updated_at: Optional[datetime]


class OverviewResponse(BaseModel):
    bot_status: str
    kill_switch_active: bool
    kill_switch_reason: Optional[str]
    kill_switch_triggered_at: Optional[datetime]
    ws_connected: bool
    testnet: bool
    symbols: list[str]
    today_realized_pnl: str
    today_fees: str
    net_pnl: str
    timestamp: datetime


class OrderInfo(BaseModel):
    cloid: str
    symbol: str
    side: str
    price: str
    size: str
    filled_size: str
    tif: str
    reduce_only: bool
    kind: str
    status: str
    exchange_oid: Optional[int]
    created_at: datetime
    updated_at: datetime
    reject_reason: Optional[str]


class FillInfo(BaseModel):
    fill_id: str
    cloid: Optional[str]
    symbol: str
    side: str
    price: str
    size: str
    fee: str
    fee_token: str
    filled_at: datetime
    is_maker: bool


class PositionInfo(BaseModel):
    symbol: str
    size: str
    avg_cost: str
    unrealized_pnl: str
    exposure_usd: str
    skew_bps: str
    inventory_limit_long: bool
    inventory_limit_short: bool


class PnLResponse(BaseModel):
    realized_pnl: str
    unrealized_pnl: str
    fees_paid: str
    net_pnl: str
    daily_loss_pct: str
    intraday_drawdown_pct: str
    day_start_equity: Optional[str]


class RiskResponse(BaseModel):
    kill_switch_active: bool
    kill_switch_reason: Optional[str]
    daily_loss_pct: str
    daily_loss_limit_pct: str
    intraday_drawdown_pct: str
    drawdown_limit_pct: str
    total_exposure_usd: str
    total_exposure_limit_usd: str
    consecutive_rejects: int
    max_reject_streak: int
    reconnect_count: int
    reconnect_streak: int
    max_reconnect_streak: int
    stale_data_age_ms: int
    stale_data_threshold_ms: int
    abnormal_spread: bool
    abrupt_move: bool
    book_corrupted: bool
    emergency_flatten_pending: bool


class EventInfo(BaseModel):
    event_id: str
    level: str
    event_type: str
    message: str
    symbol: Optional[str]
    occurred_at: datetime


class StopResponse(BaseModel):
    accepted: bool
    message: str
