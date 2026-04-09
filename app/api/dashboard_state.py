"""
Bridge between BotState (in-memory) and dashboard API responses.

Rules:
- Only reads from BotState — never writes.
- Filters out any sensitive fields before returning.
- Combines in-memory state with SQLite data where needed.
- Bot logic must never depend on anything in this module.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.api.schemas import (
    EventInfo,
    FillInfo,
    MarketInfo,
    OrderInfo,
    OverviewResponse,
    PositionInfo,
    PnLResponse,
    RiskResponse,
)
from app.models import OrderStatus
from app.persistence import Persistence
from app.settings import Settings
from app.state import BotState


class DashboardState:
    """
    Provides formatted snapshots of bot state for API responses.
    All methods are async for compatibility with FastAPI endpoints.
    """

    def __init__(
        self, state: BotState, persistence: Persistence, settings: Settings
    ) -> None:
        self._state = state
        self._db = persistence
        self._settings = settings
        self._started_at: Optional[datetime] = None

    def set_start_time(self, t: datetime) -> None:
        self._started_at = t

    async def _snap(self) -> BotState:
        return await self._state.snapshot()

    async def get_overview(self) -> OverviewResponse:
        s = await self._snap()
        pnl = s.pnl
        now = datetime.now(tz=timezone.utc)
        started = s.started_at
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        uptime_s = (now - started).total_seconds() if started else None
        return OverviewResponse(
            bot_status=s.status.value,
            kill_switch_active=s.kill_switch_active,
            kill_switch_reason=s.kill_switch_reason.value if s.kill_switch_reason else None,
            kill_switch_triggered_at=s.kill_switch_triggered_at,
            ws_connected=s.ws_connected,
            testnet=self._settings.testnet,
            symbols=self._settings.symbols,
            today_realized_pnl=str(pnl.realized_pnl),
            today_fees=str(pnl.fees_paid),
            net_pnl=str(pnl.net_pnl),
            timestamp=now,
        )

    async def get_symbols(self) -> list[MarketInfo]:
        s = await self._snap()
        result = []
        for sym, sym_state in s.symbols.items():
            m = sym_state.market
            aq = sym_state.active_quote
            result.append(MarketInfo(
                symbol=sym,
                mid=str(m.mid) if m else None,
                spread_bps=str(m.spread_bps) if m else None,
                imbalance=str(m.imbalance) if m else None,
                short_term_vol=str(m.short_term_vol) if m else None,
                stale=m.stale if m else True,
                abrupt_move=m.abrupt_move if m else False,
                book_corrupted=m.book_corrupted if m else False,
                quoting=aq is not None,
                bid_quote=str(aq.bid_price) if aq else None,
                ask_quote=str(aq.ask_price) if aq else None,
                bid_size=str(aq.bid_size) if aq else None,
                ask_size=str(aq.ask_size) if aq else None,
                updated_at=m.updated_at if m else None,
            ))
        return result

    async def get_orders(self) -> list[OrderInfo]:
        s = await self._snap()
        orders = []
        for order in list(s.open_orders.values()):
            orders.append(_order_to_schema(order))
        # Also include recent closed orders
        for order in list(s.recent_orders):
            orders.append(_order_to_schema(order))
        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    async def get_fills(self) -> list[FillInfo]:
        # Use DB for fills to get persistent history
        raw_fills = await self._db.get_recent_fills(limit=100)
        result = []
        for row in raw_fills:
            result.append(FillInfo(
                fill_id=row["fill_id"],
                cloid=row.get("cloid"),
                symbol=row["symbol"],
                side=row["side"],
                price=row["price"],
                size=row["size"],
                fee=row["fee"],
                fee_token=row["fee_token"],
                filled_at=datetime.fromisoformat(row["filled_at"]),
                is_maker=bool(row["is_maker"]),
            ))
        return result

    async def get_positions(self) -> list[PositionInfo]:
        s = await self._snap()
        result = []
        for sym, sym_state in s.symbols.items():
            pos = sym_state.position
            market = sym_state.market
            mid = market.mid if market else Decimal("0")
            size = pos.size if pos else Decimal("0")
            avg_cost = pos.avg_cost if pos else Decimal("0")
            unrealized = pos.unrealized_pnl if pos else Decimal("0")
            exposure_usd = abs(size) * mid if mid > 0 else Decimal("0")

            # Skew and limit computation (no inventory_manager here — compute inline)
            max_pos = self._settings.max_position_usd_per_symbol
            ratio = size * mid / max_pos if max_pos > 0 and mid > 0 else Decimal("0")
            ratio = max(Decimal("-1"), min(Decimal("1"), ratio))
            skew_bps = ratio * self._settings.inventory_skew_max_bps

            inv_limit_long = (
                exposure_usd >= max_pos and size > 0
            ) if mid > 0 else False
            inv_limit_short = (
                exposure_usd >= max_pos and size < 0
            ) if mid > 0 else False

            result.append(PositionInfo(
                symbol=sym,
                size=str(size),
                avg_cost=str(avg_cost),
                unrealized_pnl=str(unrealized),
                exposure_usd=str(exposure_usd),
                skew_bps=str(skew_bps),
                inventory_limit_long=inv_limit_long,
                inventory_limit_short=inv_limit_short,
            ))
        return result

    async def get_pnl(self) -> PnLResponse:
        s = await self._snap()
        pnl = s.pnl
        # Daily loss (positive = loss)
        if pnl.day_start_equity and pnl.day_start_equity > 0:
            daily_loss_pct = -(pnl.realized_pnl - pnl.fees_paid) / pnl.day_start_equity * 100
        else:
            daily_loss_pct = Decimal("0")
        # Intraday drawdown
        if pnl.intraday_peak_equity and pnl.intraday_peak_equity > 0:
            current = (pnl.day_start_equity or Decimal("0")) + pnl.realized_pnl - pnl.fees_paid
            drawdown_pct = (pnl.intraday_peak_equity - current) / pnl.intraday_peak_equity * 100
        else:
            drawdown_pct = Decimal("0")
        return PnLResponse(
            realized_pnl=str(pnl.realized_pnl),
            unrealized_pnl=str(pnl.unrealized_pnl),
            fees_paid=str(pnl.fees_paid),
            net_pnl=str(pnl.net_pnl),
            daily_loss_pct=str(daily_loss_pct),
            intraday_drawdown_pct=str(drawdown_pct),
            day_start_equity=str(pnl.day_start_equity) if pnl.day_start_equity else None,
        )

    async def get_risk(self) -> RiskResponse:
        s = await self._snap()
        risk = s.risk
        pnl = s.pnl

        if pnl.day_start_equity and pnl.day_start_equity > 0:
            daily_loss_pct = -(pnl.realized_pnl - pnl.fees_paid) / pnl.day_start_equity * 100
        else:
            daily_loss_pct = Decimal("0")

        if pnl.intraday_peak_equity and pnl.intraday_peak_equity > 0:
            current = (pnl.day_start_equity or Decimal("0")) + pnl.realized_pnl - pnl.fees_paid
            drawdown_pct = (pnl.intraday_peak_equity - current) / pnl.intraday_peak_equity * 100
        else:
            drawdown_pct = Decimal("0")

        total_exposure = sum(
            abs(sym_state.position.size) * sym_state.market.mid
            for sym_state in s.symbols.values()
            if sym_state.position and sym_state.market and sym_state.market.mid > 0
        )

        # Count reconnect streak within window
        from datetime import timedelta
        now = datetime.now(tz=timezone.utc)
        window = timedelta(seconds=self._settings.reconnect_window_s)
        def _tz(t: datetime) -> datetime:
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

        reconnect_streak = sum(
            1 for t in s.ws_reconnect_times
            if now - _tz(t) < window
        )

        stale_age_ms = max(
            (
                int((now - _tz(sym_state.market.updated_at)).total_seconds() * 1000)
                for sym_state in s.symbols.values()
                if sym_state.market
            ),
            default=0,
        )

        return RiskResponse(
            kill_switch_active=s.kill_switch_active,
            kill_switch_reason=s.kill_switch_reason.value if s.kill_switch_reason else None,
            daily_loss_pct=str(daily_loss_pct),
            daily_loss_limit_pct=str(self._settings.max_daily_loss_pct),
            intraday_drawdown_pct=str(drawdown_pct),
            drawdown_limit_pct=str(self._settings.max_intraday_drawdown_pct),
            total_exposure_usd=str(total_exposure),
            total_exposure_limit_usd=str(self._settings.max_total_exposure_usd),
            consecutive_rejects=risk.consecutive_rejects,
            max_reject_streak=self._settings.max_reject_streak,
            reconnect_count=risk.reconnect_count,
            reconnect_streak=reconnect_streak,
            max_reconnect_streak=self._settings.max_reconnect_streak,
            stale_data_age_ms=stale_age_ms,
            stale_data_threshold_ms=self._settings.stale_data_threshold_ms,
            abnormal_spread=risk.abnormal_spread,
            abrupt_move=risk.abrupt_move,
            book_corrupted=risk.book_corrupted,
            emergency_flatten_pending=risk.emergency_flatten_pending,
        )

    async def get_events(self) -> list[EventInfo]:
        s = await self._snap()
        result = [
            EventInfo(
                event_id=e.event_id,
                level=e.level.value,
                event_type=e.event_type,
                message=e.message,
                symbol=e.symbol,
                occurred_at=e.occurred_at,
            )
            for e in reversed(list(s.events))
        ]
        return result


def _order_to_schema(order) -> OrderInfo:
    return OrderInfo(
        cloid=order.cloid.to_hex(),
        symbol=order.symbol,
        side=order.side.value,
        price=str(order.price),
        size=str(order.size),
        filled_size=str(order.filled_size),
        tif=order.tif.value,
        reduce_only=order.reduce_only,
        kind=order.kind.value,
        status=order.status.value,
        exchange_oid=order.exchange_oid,
        created_at=order.created_at,
        updated_at=order.updated_at,
        reject_reason=order.reject_reason,
    )
