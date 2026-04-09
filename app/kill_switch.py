"""
Kill switch: monitors risk conditions and halts trading when triggered.

Responsibilities:
- Periodically call risk_manager.compute_kill_conditions()
- Activate BotState.kill_switch when any condition is met
- Initiate emergency flatten if configured
- Log every trigger with full reason

This module does NOT make trading decisions — it only calls
execution.emergency_flatten() which is defined in execution.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from app.models import BotEvent, EventLevel, KillReason
from app.settings import Settings
from app.state import BotState
from app.telemetry import get_logger

if TYPE_CHECKING:
    from app.risk_manager import RiskManager

log = get_logger(__name__)

_CHECK_INTERVAL_S = 1.0  # how often to check kill conditions


class KillSwitch:
    """
    Monitors all kill conditions and triggers shutdown when needed.

    Usage:
        ks = KillSwitch(state, settings, risk_manager)
        asyncio.create_task(ks.run())
        # From elsewhere:
        await ks.manual_kill("user requested stop")
    """

    def __init__(
        self,
        state: BotState,
        settings: Settings,
        risk_manager: "RiskManager",
    ) -> None:
        self._state = state
        self._settings = settings
        self._risk = risk_manager
        self._flatten_callback: Optional[asyncio.coroutines] = None  # set by execution.py
        self._running = False

    def set_flatten_callback(self, callback) -> None:
        """
        Register the emergency flatten coroutine factory.
        Called by execution.py at startup.
        flatten_callback() must return a coroutine that flattens positions.
        """
        self._flatten_callback = callback

    # ------------------------------------------------------------------
    # Main monitoring loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        log.info("KillSwitch monitoring started (interval=%.1fs)", _CHECK_INTERVAL_S)

        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL_S)
            if self._state.kill_switch_active:
                continue  # already triggered, nothing to do

            reasons = self._risk.compute_kill_conditions()
            if reasons:
                primary = reasons[0]
                all_reasons = ", ".join(r.value for r in reasons)
                await self._trigger(primary, f"Auto-triggered: {all_reasons}")

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Manual kill
    # ------------------------------------------------------------------

    async def manual_kill(self, reason: str = "manual") -> None:
        await self._trigger(KillReason.MANUAL, f"Manual kill: {reason}")

    # ------------------------------------------------------------------
    # Internal trigger
    # ------------------------------------------------------------------

    async def _trigger(self, reason: KillReason, message: str) -> None:
        """
        Activate kill switch. Idempotent — safe to call multiple times.
        """
        if self._state.kill_switch_active:
            return

        log.critical(
            "KILL SWITCH ACTIVATED: reason=%s message=%s",
            reason.value, message,
        )

        await self._state.activate_kill_switch(reason, message)
        await self._state.add_event(BotEvent(
            level=EventLevel.CRITICAL,
            event_type="kill_switch",
            message=message,
            detail={"reason": reason.value},
        ))

        # Emergency flatten if configured and not just a stale data pause
        if (
            self._settings.emergency_flatten_enabled
            and self._flatten_callback is not None
            and reason in (
                KillReason.MANUAL,
                KillReason.DAILY_LOSS_EXCEEDED,
                KillReason.INTRADAY_DRAWDOWN_EXCEEDED,
            )
        ):
            log.warning("Initiating emergency flatten...")
            try:
                await self._flatten_callback()
            except Exception as exc:
                log.error("Emergency flatten failed: %s", exc)
                # Do NOT re-raise — we are already in kill mode
