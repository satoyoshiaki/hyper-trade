"""
Core domain models for the Hyperliquid maker bot.

All exchange-specific raw types are handled in app/exchange/normalizer.py.
These models are exchange-agnostic internal representations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BotStatus(Enum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Side(Enum):
    BUY = "buy"
    SELL = "sell"

    def opposite(self) -> "Side":
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderStatus(Enum):
    PENDING = "pending"       # submitted, not yet confirmed by exchange
    OPEN = "open"             # confirmed open on exchange
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderKind(Enum):
    """What purpose this order serves."""
    MAKER_QUOTE = "maker_quote"          # normal post-only market making
    EMERGENCY_FLATTEN = "emergency_flatten"  # reduce-only IOC on kill


class TIF(Enum):
    """Time-in-force.
    FACT: Hyperliquid supports GTC, IOC, ALO.
    ALO (All-or-None) is the post-only equivalent.
    """
    GTC = "Gtc"
    IOC = "Ioc"
    ALO = "Alo"   # post-only / maker-only


class KillReason(Enum):
    DAILY_LOSS_EXCEEDED = "daily_loss_exceeded"
    INTRADAY_DRAWDOWN_EXCEEDED = "intraday_drawdown_exceeded"
    STALE_MARKET_DATA = "stale_market_data"
    RECONNECT_STORM = "reconnect_storm"
    CONSECUTIVE_REJECTS = "consecutive_rejects"
    ABNORMAL_SPREAD = "abnormal_spread"
    ABRUPT_PRICE_MOVE = "abrupt_price_move"
    BOOK_CORRUPTED = "book_corrupted"
    MANUAL = "manual"


class EventLevel(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class NoQuoteReason(Enum):
    """Reasons why quote_engine returns no quote."""
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    STALE_MARKET_DATA = "stale_market_data"
    ABNORMAL_SPREAD = "abnormal_spread"
    ABRUPT_MOVE = "abrupt_move"
    BOOK_CORRUPTED = "book_corrupted"
    MIN_EDGE_NOT_MET = "min_edge_not_met"
    INVENTORY_LIMIT_HIT = "inventory_limit_hit"
    RISK_CHECK_FAILED = "risk_check_failed"
    INSUFFICIENT_DATA = "insufficient_data"


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cloid:
    """
    Client Order ID for idempotent order tracking.
    FACT: Hyperliquid supports CLOID via Cloid.from_int() in the SDK.
    We generate a monotonic integer internally and expose it as hex string.
    """
    value: int

    def to_hex(self) -> str:
        """Return 32-char hex string as expected by HL SDK."""
        return f"0x{self.value:032x}"

    @classmethod
    def generate(cls) -> "Cloid":
        return cls(value=uuid.uuid4().int & 0xFFFF_FFFF_FFFF_FFFF)

    def __str__(self) -> str:
        return self.to_hex()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass
class BookLevel:
    price: Decimal
    size: Decimal
    num_orders: int = 0


@dataclass
class MarketSnapshot:
    """Processed market state for one symbol."""
    symbol: str
    mid: Decimal
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal           # ask - bid
    spread_bps: Decimal       # spread / mid * 10000
    imbalance: Decimal        # (bid_qty - ask_qty) / (bid_qty + ask_qty), range [-1, 1]
    short_term_vol: Decimal   # short-term realized vol estimate
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    stale: bool = False
    abrupt_move: bool = False
    book_corrupted: bool = False


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


@dataclass
class Quote:
    """A desired bid/ask quote for one symbol."""
    symbol: str
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ActiveQuote:
    """A quote that has been submitted as open orders."""
    symbol: str
    bid_cloid: Optional[Cloid]
    ask_cloid: Optional[Cloid]
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    submitted_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@dataclass
class Order:
    cloid: Cloid
    symbol: str
    side: Side
    price: Decimal
    size: Decimal
    tif: TIF
    reduce_only: bool
    kind: OrderKind
    status: OrderStatus = OrderStatus.PENDING
    exchange_oid: Optional[int] = None    # HL's internal order ID
    filled_size: Decimal = field(default_factory=lambda: Decimal("0"))
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    reject_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------


@dataclass
class Fill:
    fill_id: str
    cloid: Optional[Cloid]
    exchange_oid: Optional[int]
    symbol: str
    side: Side
    price: Decimal
    size: Decimal
    fee: Decimal
    fee_token: str = "USDC"
    filled_at: datetime = field(default_factory=datetime.utcnow)
    is_maker: bool = True


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


@dataclass
class Position:
    symbol: str
    size: Decimal            # positive = long, negative = short
    avg_cost: Decimal
    unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    updated_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# PnL
# ---------------------------------------------------------------------------


@dataclass
class PnLState:
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    fees_paid: Decimal = field(default_factory=lambda: Decimal("0"))
    # day-start snapshot for daily loss calculation
    day_start_equity: Optional[Decimal] = None
    # intraday peak equity for drawdown calculation
    intraday_peak_equity: Optional[Decimal] = None

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl - self.fees_paid


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


@dataclass
class RiskMetrics:
    """Current risk state / utilization rates."""
    daily_loss_pct: Decimal = field(default_factory=lambda: Decimal("0"))
    intraday_drawdown_pct: Decimal = field(default_factory=lambda: Decimal("0"))
    total_exposure_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    stale_data_age_ms: int = 0
    consecutive_rejects: int = 0
    reconnect_count: int = 0        # total reconnects
    reconnect_streak: int = 0       # reconnects in recent window
    abnormal_spread: bool = False
    abrupt_move: bool = False
    book_corrupted: bool = False
    emergency_flatten_pending: bool = False


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass
class BotEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    level: EventLevel = EventLevel.INFO
    event_type: str = ""
    message: str = ""
    symbol: Optional[str] = None
    detail: Optional[dict] = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
