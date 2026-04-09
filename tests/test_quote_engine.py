"""Tests for QuoteEngine."""

from decimal import Decimal

import pytest

from app.models import ActiveQuote, Cloid, MarketSnapshot, NoQuoteReason
from app.quote_engine import QuoteEngine


def make_engine(settings) -> QuoteEngine:
    qe = QuoteEngine(settings)
    qe.set_asset_specs("BTC", tick_size=Decimal("1"), sz_decimals=4)
    qe.set_asset_specs("ETH", tick_size=Decimal("0.1"), sz_decimals=3)
    return qe


class TestBasicQuoteGeneration:
    def test_returns_quote_on_clean_market(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote is not None
        assert result.reason is None
        assert result.quote.bid_price < result.quote.ask_price

    def test_bid_below_mid(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote.bid_price < btc_market.mid

    def test_ask_above_mid(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote.ask_price > btc_market.mid

    def test_bid_ask_positive_sizes(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote.bid_size > 0
        assert result.quote.ask_size > 0


class TestNoQuote:
    def test_stale_market_returns_no_quote(self, settings, btc_market):
        btc_market.stale = True
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote is None
        assert result.reason == NoQuoteReason.STALE_MARKET_DATA

    def test_book_corrupted_returns_no_quote(self, settings, btc_market):
        btc_market.book_corrupted = True
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote is None
        assert result.reason == NoQuoteReason.BOOK_CORRUPTED

    def test_abrupt_move_returns_no_quote(self, settings, btc_market):
        btc_market.abrupt_move = True
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote is None
        assert result.reason == NoQuoteReason.ABRUPT_MOVE

    def test_abnormal_spread_returns_no_quote(self, settings, btc_market):
        # spread_bps > base_spread_bps * abnormal_spread_multiplier
        btc_market.spread_bps = settings.base_spread_bps * settings.abnormal_spread_multiplier + 1
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        assert result.quote is None
        assert result.reason == NoQuoteReason.ABNORMAL_SPREAD

    def test_inventory_limit_bid_zeros_bid_size(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote(
            "BTC", btc_market,
            inventory_skew_bps=Decimal("0"),
            inventory_limit_bid=True,
        )
        # Should still return a quote (ask side only) or no-quote if both zero
        # With only ask side: should return quote with bid_size=0
        assert result.quote is not None
        assert result.quote.bid_size == 0
        assert result.quote.ask_size > 0

    def test_both_inventory_limits_hit_returns_no_quote(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote(
            "BTC", btc_market,
            inventory_skew_bps=Decimal("0"),
            inventory_limit_bid=True,
            inventory_limit_ask=True,
        )
        assert result.quote is None
        assert result.reason == NoQuoteReason.INVENTORY_LIMIT_HIT


class TestTickRounding:
    def test_bid_price_rounded_to_tick(self, settings, btc_market):
        qe = make_engine(settings)  # tick_size=1 for BTC
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        q = result.quote
        # Price should be integer (tick=1)
        assert q.bid_price == q.bid_price.to_integral_value()

    def test_ask_price_rounded_to_tick(self, settings, btc_market):
        qe = make_engine(settings)
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        q = result.quote
        assert q.ask_price == q.ask_price.to_integral_value()

    def test_size_respects_sz_decimals(self, settings, btc_market):
        qe = make_engine(settings)  # sz_decimals=4 for BTC
        result = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        q = result.quote
        # Size should have at most 4 decimal places
        assert abs(q.bid_size.as_tuple().exponent) <= 4


class TestInventorySkew:
    def test_positive_skew_shifts_prices_down(self, settings, btc_market):
        """Long inventory → skew pushes both prices lower to encourage selling."""
        qe = make_engine(settings)
        result_neutral = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        result_long = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("10"))
        assert result_long.quote.bid_price <= result_neutral.quote.bid_price
        assert result_long.quote.ask_price <= result_neutral.quote.ask_price

    def test_negative_skew_shifts_prices_up(self, settings, btc_market):
        """Short inventory → skew pushes prices higher to encourage buying."""
        qe = make_engine(settings)
        result_neutral = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("0"))
        result_short = qe.compute_quote("BTC", btc_market, inventory_skew_bps=Decimal("-10"))
        assert result_short.quote.bid_price >= result_neutral.quote.bid_price
        assert result_short.quote.ask_price >= result_neutral.quote.ask_price


class TestShouldReplace:
    def make_active_quote(self, bid=Decimal("83990"), ask=Decimal("84010")):
        from app.models import ActiveQuote, Cloid
        from datetime import datetime, timezone
        return ActiveQuote(
            symbol="BTC",
            bid_cloid=Cloid.generate(),
            ask_cloid=Cloid.generate(),
            bid_price=bid,
            ask_price=ask,
            bid_size=Decimal("0.0003"),
            ask_size=Decimal("0.0003"),
            submitted_at=datetime.now(tz=timezone.utc),
        )

    def test_replace_when_age_exceeded(self, settings, btc_market):
        qe = make_engine(settings)
        new_quote = qe.compute_quote("BTC", btc_market, Decimal("0")).quote
        active = self.make_active_quote()
        # Elapsed = max_quote_age_ms → should replace
        assert qe.should_replace(new_quote, active, settings.max_quote_age_ms)

    def test_no_replace_when_price_unchanged(self, settings, btc_market):
        qe = make_engine(settings)
        new_quote = qe.compute_quote("BTC", btc_market, Decimal("0")).quote
        # Use same prices as new_quote
        active = self.make_active_quote(
            bid=new_quote.bid_price, ask=new_quote.ask_price
        )
        # Young quote, same price → no replace
        assert not qe.should_replace(new_quote, active, elapsed_ms=100)
