"""
Tests for Settings validation.

Confirms that the bot refuses to start with invalid or dangerous configs.
Each test asserts fast-fail behavior at config load time.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError


def make_valid_kwargs() -> dict:
    """Return a complete set of valid Settings kwargs for baseline tests."""
    return dict(
        private_key="0x" + "a" * 64,
        wallet_address="0x" + "b" * 40,
        testnet=True,
        symbols=["BTC", "ETH"],
        max_order_size_usd=Decimal("25"),
        max_position_usd_per_symbol=Decimal("50"),
        max_total_exposure_usd=Decimal("100"),
        max_daily_loss_pct=Decimal("2.0"),
        max_intraday_drawdown_pct=Decimal("1.0"),
        base_spread_bps=Decimal("10"),
        min_edge_bps=Decimal("5"),
        vol_multiplier=Decimal("2.0"),
        inventory_skew_max_bps=Decimal("20"),
        imbalance_weight=Decimal("0.5"),
        quote_refresh_ms=5000,
        max_quote_age_ms=30000,
        price_replace_threshold_bps=Decimal("2.0"),
        stale_data_threshold_ms=5000,
        abnormal_spread_multiplier=Decimal("5.0"),
        abrupt_move_pct=Decimal("1.0"),
        max_reject_streak=10,
        max_reconnect_streak=5,
        reconnect_window_s=60,
        emergency_flatten_enabled=True,
        dashboard_enabled=False,
        dashboard_host="127.0.0.1",
        dashboard_port=8080,
        log_level="WARNING",
    )


def make_settings(**overrides):
    from app.settings import Settings
    import os
    os.environ.pop("SYMBOLS", None)
    kwargs = make_valid_kwargs()
    kwargs.update(overrides)
    return Settings(**kwargs)


class TestPlaceholderRejection:
    def test_placeholder_private_key_raises(self):
        with pytest.raises(ValidationError, match="placeholder"):
            make_settings(private_key="0xyour_private_key_here")

    def test_non_hex_private_key_raises(self):
        with pytest.raises(ValidationError, match="placeholder"):
            make_settings(private_key="not_starting_with_0x")

    def test_placeholder_wallet_raises(self):
        with pytest.raises(ValidationError, match="placeholder"):
            make_settings(wallet_address="0xyour_wallet_address_here")

    def test_non_hex_wallet_raises(self):
        with pytest.raises(ValidationError, match="placeholder"):
            make_settings(wallet_address="no_0x_prefix")


class TestSizeLimitValidation:
    def test_order_size_exceeds_position_limit_raises(self):
        """max_order_size_usd must be <= max_position_usd_per_symbol."""
        with pytest.raises(ValidationError):
            make_settings(
                max_order_size_usd=Decimal("100"),
                max_position_usd_per_symbol=Decimal("50"),
            )

    def test_order_size_equal_to_position_limit_ok(self):
        """Exactly equal is allowed."""
        s = make_settings(
            max_order_size_usd=Decimal("50"),
            max_position_usd_per_symbol=Decimal("50"),
        )
        assert s.max_order_size_usd == s.max_position_usd_per_symbol

    def test_leverage_above_max_raises(self):
        with pytest.raises(ValidationError):
            make_settings(leverage=6)  # max is 5

    def test_leverage_zero_raises(self):
        with pytest.raises(ValidationError):
            make_settings(leverage=0)


class TestSymbolParsing:
    def test_comma_separated_string_parsed(self):
        import os
        os.environ.pop("SYMBOLS", None)
        from app.settings import Settings
        s = Settings(**{**make_valid_kwargs(), "symbols": "BTC,ETH,SOL"})
        assert s.symbols == ["BTC", "ETH", "SOL"]

    def test_list_input_accepted(self):
        s = make_settings(symbols=["BTC"])
        assert s.symbols == ["BTC"]

    def test_symbols_uppercased(self):
        s = make_settings(symbols=["btc", "eth"])
        assert s.symbols == ["BTC", "ETH"]

    def test_whitespace_stripped(self):
        import os
        os.environ.pop("SYMBOLS", None)
        from app.settings import Settings
        s = Settings(**{**make_valid_kwargs(), "symbols": " BTC , ETH "})
        assert s.symbols == ["BTC", "ETH"]


class TestDailyLossValidation:
    def test_zero_daily_loss_pct_raises(self):
        with pytest.raises(ValidationError):
            make_settings(max_daily_loss_pct=Decimal("0"))

    def test_negative_daily_loss_pct_raises(self):
        with pytest.raises(ValidationError):
            make_settings(max_daily_loss_pct=Decimal("-1"))

    def test_over_100_daily_loss_pct_raises(self):
        with pytest.raises(ValidationError):
            make_settings(max_daily_loss_pct=Decimal("101"))


class TestDerivedProperties:
    def test_api_url_is_testnet_when_testnet_true(self):
        s = make_settings(testnet=True)
        assert "testnet" in s.api_url

    def test_api_url_is_mainnet_when_testnet_false(self):
        s = make_settings(testnet=False)
        assert "testnet" not in s.api_url

    def test_ensure_dirs_creates_directories(self, tmp_path):
        s = make_settings()
        s = s.model_copy(update={
            "log_dir": tmp_path / "logs",
            "db_path": tmp_path / "data" / "bot.db",
        })
        s.ensure_dirs()
        assert (tmp_path / "logs").exists()
        assert (tmp_path / "data").exists()
