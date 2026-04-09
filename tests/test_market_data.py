"""Tests for MarketDataProcessor."""

from decimal import Decimal

import pytest

from app.exchange.normalizer import parse_book_level, parse_l2_book, NormalizerError
from app.market_data import MarketDataProcessor
from app.models import BookLevel


class TestNormalizer:
    def test_parse_book_level_valid(self):
        raw = {"px": "84000.5", "sz": "0.123", "n": 3}
        level = parse_book_level(raw)
        assert level.price == Decimal("84000.5")
        assert level.size == Decimal("0.123")
        assert level.num_orders == 3

    def test_parse_book_level_missing_field_raises(self):
        with pytest.raises(NormalizerError):
            parse_book_level({"sz": "0.1", "n": 1})  # missing "px"

    def test_parse_l2_book_valid(self):
        raw = {
            "levels": [
                [{"px": "83990", "sz": "0.5", "n": 1}],
                [{"px": "84010", "sz": "0.3", "n": 1}],
            ]
        }
        bids, asks = parse_l2_book("BTC", raw)
        assert bids[0].price == Decimal("83990")
        assert asks[0].price == Decimal("84010")

    def test_parse_l2_book_empty_raises(self):
        with pytest.raises(NormalizerError):
            parse_l2_book("BTC", {"levels": []})


class TestMarketDataProcessor:
    @pytest.mark.asyncio
    async def test_book_update_sets_market_snapshot(self, settings, bot_state):
        proc = MarketDataProcessor(bot_state, settings)
        raw = {
            "levels": [
                [{"px": "83990", "sz": "1.0", "n": 2}],
                [{"px": "84010", "sz": "0.8", "n": 1}],
            ]
        }
        await proc.on_book_update("BTC", raw)
        snap = bot_state.symbols["BTC"].market
        assert snap is not None
        assert snap.mid == (Decimal("83990") + Decimal("84010")) / 2

    @pytest.mark.asyncio
    async def test_corrupted_book_sets_flag(self, settings, bot_state):
        proc = MarketDataProcessor(bot_state, settings)
        # bid >= ask → corrupted
        raw = {
            "levels": [
                [{"px": "84020", "sz": "1.0", "n": 1}],  # bid higher than ask
                [{"px": "84000", "sz": "1.0", "n": 1}],
            ]
        }
        await proc.on_book_update("BTC", raw)
        snap = bot_state.symbols["BTC"].market
        assert snap.book_corrupted is True

    @pytest.mark.asyncio
    async def test_stale_detection(self, settings, bot_state):
        proc = MarketDataProcessor(bot_state, settings)
        # No update yet → should be stale
        is_stale, age_ms = proc.is_stale("BTC")
        assert is_stale is True

    @pytest.mark.asyncio
    async def test_fresh_data_not_stale(self, settings, bot_state):
        proc = MarketDataProcessor(bot_state, settings)
        raw = {
            "levels": [
                [{"px": "83990", "sz": "1.0", "n": 1}],
                [{"px": "84010", "sz": "1.0", "n": 1}],
            ]
        }
        await proc.on_book_update("BTC", raw)
        is_stale, age_ms = proc.is_stale("BTC")
        assert is_stale is False
        assert age_ms < 1000  # should be very fresh
