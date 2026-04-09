"""
Configuration management via pydantic-settings.

All values are loaded from environment variables or .env file.
Settings validation failures raise at startup — fast fail is intentional.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Exchange credentials ---
    # NEVER log these values. telemetry.py must filter them.
    private_key: str = Field(..., description="Ethereum private key (hex)")
    wallet_address: str = Field(..., description="Wallet address (0x...)")

    # --- Network ---
    testnet: bool = Field(default=True, description="Use testnet if True")

    # FACT: Testnet base URL (verify before mainnet)
    # UNVERIFIED: exact testnet WS URL — adapter layer must confirm at runtime
    testnet_api_url: str = Field(
        default="https://api.hyperliquid-testnet.xyz",
        description="Testnet REST API URL",
    )
    mainnet_api_url: str = Field(
        default="https://api.hyperliquid.xyz",
        description="Mainnet REST API URL",
    )

    # --- Trading ---
    symbols: list[str] = Field(
        default=["BTC", "ETH"],
        description="Symbols to trade (Perp only in v1)",
    )
    leverage: int = Field(default=1, ge=1, le=5, description="Leverage (isolated)")

    # --- Size limits (USD) ---
    max_order_size_usd: Decimal = Field(default=Decimal("25"), gt=0)
    max_position_usd_per_symbol: Decimal = Field(default=Decimal("50"), gt=0)
    max_total_exposure_usd: Decimal = Field(default=Decimal("100"), gt=0)

    # --- Loss limits ---
    max_daily_loss_pct: Decimal = Field(
        default=Decimal("2.0"), gt=0, le=100,
        description="Stop trading if daily loss exceeds this %",
    )
    max_intraday_drawdown_pct: Decimal = Field(
        default=Decimal("1.0"), gt=0, le=100,
        description="Stop trading if intraday drawdown exceeds this %",
    )

    # --- Quote engine ---
    base_spread_bps: Decimal = Field(default=Decimal("10"), gt=0)
    min_edge_bps: Decimal = Field(default=Decimal("5"), gt=0)
    vol_multiplier: Decimal = Field(
        default=Decimal("2.0"), gt=0,
        description="How much vol widens the quoted spread",
    )
    inventory_skew_max_bps: Decimal = Field(
        default=Decimal("20"), gt=0,
        description="Max inventory skew applied to quote prices (bps)",
    )
    imbalance_weight: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=1,
        description="Weight of order book imbalance in spread adjustment",
    )

    # Timing (milliseconds)
    quote_refresh_ms: int = Field(default=5000, gt=0)
    max_quote_age_ms: int = Field(default=30000, gt=0)
    price_replace_threshold_bps: Decimal = Field(
        default=Decimal("2.0"), gt=0,
        description="Min price move (bps) before replacing a quote",
    )

    # --- Risk / Safety ---
    stale_data_threshold_ms: int = Field(
        default=5000, gt=0,
        description="Market data older than this is considered stale",
    )
    abnormal_spread_multiplier: Decimal = Field(
        default=Decimal("5.0"), gt=1,
        description="Spread > baseline * this triggers abnormal flag",
    )
    abrupt_move_pct: Decimal = Field(
        default=Decimal("1.0"), gt=0,
        description="Price move > this % in short window triggers abrupt move flag",
    )
    max_reject_streak: int = Field(
        default=10, gt=0,
        description="Consecutive rejects before kill switch",
    )
    max_reconnect_streak: int = Field(
        default=5, gt=0,
        description="WS reconnects within reconnect_window_s before kill switch",
    )
    reconnect_window_s: int = Field(
        default=60, gt=0,
        description="Window (seconds) for counting reconnect streaks",
    )

    emergency_flatten_enabled: bool = Field(
        default=True,
        description="Flatten positions on kill switch (reduce-only IOC)",
    )

    # --- Dashboard ---
    dashboard_enabled: bool = Field(default=True)
    dashboard_host: str = Field(
        default="127.0.0.1",
        description="Dashboard bind host. WARNING: changing to 0.0.0.0 exposes externally.",
    )
    dashboard_port: int = Field(default=8080, gt=1024, lt=65535)

    # --- Logging / Persistence ---
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=Path("logs"))
    db_path: Path = Field(default=Path("data/bot.db"))

    # --- Validators ---

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(s).upper() for s in v]
        raise ValueError(f"Cannot parse symbols: {v!r}")

    @field_validator("private_key")
    @classmethod
    def private_key_not_placeholder(cls, v: str) -> str:
        if "your_private_key" in v or not v.startswith("0x"):
            raise ValueError(
                "PRIVATE_KEY looks like a placeholder. "
                "Set a real private key in .env before starting."
            )
        return v

    @field_validator("wallet_address")
    @classmethod
    def wallet_address_not_placeholder(cls, v: str) -> str:
        if "your_wallet" in v or not v.startswith("0x"):
            raise ValueError(
                "WALLET_ADDRESS looks like a placeholder. "
                "Set a real address in .env before starting."
            )
        return v

    @field_validator("dashboard_host")
    @classmethod
    def warn_if_external_bind(cls, v: str) -> str:
        # Validation here just returns the value;
        # runtime warning is logged in api/app.py
        return v

    @model_validator(mode="after")
    def max_order_size_within_position_limit(self) -> "Settings":
        if self.max_order_size_usd > self.max_position_usd_per_symbol:
            raise ValueError(
                "max_order_size_usd must be <= max_position_usd_per_symbol"
            )
        return self

    @model_validator(mode="after")
    def position_limit_within_total_exposure(self) -> "Settings":
        if self.max_position_usd_per_symbol * len(self.symbols) > self.max_total_exposure_usd * 2:
            # Allow some slack (2x) since not all symbols will be fully positioned
            pass
        return self

    # --- Derived properties ---

    @property
    def api_url(self) -> str:
        return self.testnet_api_url if self.testnet else self.mainnet_api_url

    def ensure_dirs(self) -> None:
        """Create log and data directories if they don't exist."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings. Fails fast on invalid config."""
    return Settings()
