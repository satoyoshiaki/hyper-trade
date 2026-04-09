"""
Risk manager: validates all orders before submission.

Every order MUST pass check_order() before being sent to the exchange.
Returns an OrderRejectedReason string or None (= OK).

Safety principles:
- Fail-closed: any uncertainty → reject
- Never use unrealized PnL as a basis for risk capacity
- All limits configurable via Settings
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app.models import KillReason, NoQuoteReason, Order, Side, TIF
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

log = get_logger(__name__)


class RiskCheckFailed(Exception):
    """Raised when an order fails a risk check. Contains the reason."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RiskManager:
    """
    Pre-flight risk checks for every order.

    Usage:
        risk.check_order(order, exposure_usd)   # raises RiskCheckFailed on failure
    """

    def __init__(self, state: BotState, settings: Settings) -> None:
        self._state = state
        self._settings = settings

    # ------------------------------------------------------------------
    # Primary check — call before every order submission
    # ------------------------------------------------------------------

    def check_order(
        self,
        symbol: str,
        side: Side,
        size_usd: Decimal,
        is_emergency: bool = False,
    ) -> None:
        """
        Validate that a proposed order passes all risk limits.

        Raises RiskCheckFailed with a descriptive reason if any check fails.
        Passing does NOT guarantee the order will succeed on the exchange.

        is_emergency=True skips some checks for reduce-only flatten orders.
        """
        # --- Kill switch always blocks new non-emergency orders ---
        if self._state.kill_switch_active and not is_emergency:
            raise RiskCheckFailed("kill_switch_active")

        # --- Order size ---
        if size_usd > self._settings.max_order_size_usd:
            raise RiskCheckFailed(
                f"order_size_usd={size_usd} exceeds max={self._settings.max_order_size_usd}"
            )

        if not is_emergency:
            # --- Symbol position limit ---
            sym_state = self._state.symbols.get(symbol)
            if sym_state and sym_state.position and sym_state.market:
                pos_usd = abs(sym_state.position.size) * sym_state.market.mid
                if pos_usd + size_usd > self._settings.max_position_usd_per_symbol:
                    raise RiskCheckFailed(
                        f"symbol_position_usd={pos_usd:.2f}+{size_usd:.2f} would exceed "
                        f"max={self._settings.max_position_usd_per_symbol}"
                    )

            # --- Total exposure ---
            total_exposure = self._get_total_exposure_usd()
            if total_exposure + size_usd > self._settings.max_total_exposure_usd:
                raise RiskCheckFailed(
                    f"total_exposure_usd={total_exposure:.2f}+{size_usd:.2f} would exceed "
                    f"max={self._settings.max_total_exposure_usd}"
                )

            # --- Loss limits (realized only — never use unrealized as capacity) ---
            self._check_loss_limits()

            # --- Market quality ---
            self._check_market_quality(symbol)

            # --- Reject streak ---
            if self._state.risk.consecutive_rejects >= self._settings.max_reject_streak:
                raise RiskCheckFailed(
                    f"consecutive_rejects={self._state.risk.consecutive_rejects} "
                    f">= max={self._settings.max_reject_streak}"
                )

    def _check_loss_limits(self) -> None:
        """
        Check daily loss and intraday drawdown limits.
        Uses realized PnL minus fees. Unrealized PnL is never included.
        """
        pnl = self._state.pnl

        # Daily loss
        if pnl.day_start_equity is not None and pnl.day_start_equity > 0:
            # realized_pnl is negative when losing
            daily_loss_pct = (
                -(pnl.realized_pnl - pnl.fees_paid) / pnl.day_start_equity * 100
            )
            if daily_loss_pct >= self._settings.max_daily_loss_pct:
                raise RiskCheckFailed(
                    f"daily_loss_pct={daily_loss_pct:.2f}% >= "
                    f"max={self._settings.max_daily_loss_pct}%"
                )

        # Intraday drawdown
        if pnl.intraday_peak_equity is not None and pnl.intraday_peak_equity > 0:
            current_equity = (
                (pnl.day_start_equity or Decimal("0"))
                + pnl.realized_pnl
                - pnl.fees_paid
                # Note: unrealized NOT included here intentionally
            )
            drawdown_pct = (
                (pnl.intraday_peak_equity - current_equity)
                / pnl.intraday_peak_equity * 100
            )
            if drawdown_pct >= self._settings.max_intraday_drawdown_pct:
                raise RiskCheckFailed(
                    f"intraday_drawdown_pct={drawdown_pct:.2f}% >= "
                    f"max={self._settings.max_intraday_drawdown_pct}%"
                )

    def _check_market_quality(self, symbol: str) -> None:
        """Check stale data, abnormal spread, abrupt move, book corruption."""
        sym_state = self._state.symbols.get(symbol)
        if sym_state is None or sym_state.market is None:
            raise RiskCheckFailed(f"no_market_data for {symbol}")

        market = sym_state.market

        if market.stale:
            raise RiskCheckFailed(f"stale_market_data for {symbol}")
        if market.book_corrupted:
            raise RiskCheckFailed(f"book_corrupted for {symbol}")
        if market.abrupt_move:
            raise RiskCheckFailed(f"abrupt_move for {symbol}")

        # Abnormal spread check against baseline
        baseline = sym_state.baseline_spread_bps
        if baseline and baseline > 0:
            if market.spread_bps > baseline * self._settings.abnormal_spread_multiplier:
                raise RiskCheckFailed(
                    f"abnormal_spread for {symbol}: "
                    f"{market.spread_bps:.1f} bps > "
                    f"{baseline * self._settings.abnormal_spread_multiplier:.1f} bps"
                )

    def _get_total_exposure_usd(self) -> Decimal:
        total = Decimal("0")
        for sym, sym_state in self._state.symbols.items():
            if sym_state.position and sym_state.market:
                total += abs(sym_state.position.size) * sym_state.market.mid
        return total

    # ------------------------------------------------------------------
    # Record outcomes (for reject streak tracking)
    # ------------------------------------------------------------------

    def record_reject(self) -> None:
        self._state.risk.consecutive_rejects += 1
        log.warning(
            "Order rejected (streak=%d)", self._state.risk.consecutive_rejects
        )

    def record_success(self) -> None:
        """Reset reject streak on any successful order."""
        if self._state.risk.consecutive_rejects > 0:
            log.info(
                "Reject streak cleared (was %d)", self._state.risk.consecutive_rejects
            )
        self._state.risk.consecutive_rejects = 0

    # ------------------------------------------------------------------
    # Kill switch condition monitoring
    # ------------------------------------------------------------------

    def compute_kill_conditions(self) -> list[KillReason]:
        """
        Check all kill switch conditions and return a list of triggered reasons.
        Called periodically by kill_switch.py.
        """
        reasons: list[KillReason] = []

        # Daily loss
        pnl = self._state.pnl
        if pnl.day_start_equity and pnl.day_start_equity > 0:
            daily_loss_pct = (
                -(pnl.realized_pnl - pnl.fees_paid) / pnl.day_start_equity * 100
            )
            if daily_loss_pct >= self._settings.max_daily_loss_pct:
                reasons.append(KillReason.DAILY_LOSS_EXCEEDED)

        # Intraday drawdown
        if pnl.intraday_peak_equity and pnl.intraday_peak_equity > 0:
            current_equity = (
                (pnl.day_start_equity or Decimal("0"))
                + pnl.realized_pnl
                - pnl.fees_paid
            )
            drawdown_pct = (
                (pnl.intraday_peak_equity - current_equity)
                / pnl.intraday_peak_equity * 100
            )
            if drawdown_pct >= self._settings.max_intraday_drawdown_pct:
                reasons.append(KillReason.INTRADAY_DRAWDOWN_EXCEEDED)

        # Consecutive rejects
        if self._state.risk.consecutive_rejects >= self._settings.max_reject_streak:
            reasons.append(KillReason.CONSECUTIVE_REJECTS)

        # WS reconnect storm
        from datetime import datetime, timezone, timedelta
        now = datetime.now(tz=timezone.utc)
        window = timedelta(seconds=self._settings.reconnect_window_s)
        recent_reconnects = sum(
            1 for t in self._state.ws_reconnect_times
            if now - t < window
        )
        if recent_reconnects >= self._settings.max_reconnect_streak:
            reasons.append(KillReason.RECONNECT_STORM)

        # Market quality per symbol
        for symbol, sym_state in self._state.symbols.items():
            if sym_state.market is None:
                continue
            m = sym_state.market
            if m.book_corrupted:
                reasons.append(KillReason.BOOK_CORRUPTED)
                break
            if m.abrupt_move:
                reasons.append(KillReason.ABRUPT_PRICE_MOVE)
                break
            if m.stale:
                reasons.append(KillReason.STALE_MARKET_DATA)
                break

        return reasons
