"""
Tests for telemetry / logging.

Critical requirement: private keys, secrets, and signatures must NEVER
appear in log output.
"""

from __future__ import annotations

import logging

import pytest

from app.telemetry import SecretFilter, setup_logging


class TestSecretFilter:
    def make_record(self, msg: str) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        return record

    def test_plain_message_passes(self):
        f = SecretFilter()
        record = self.make_record("Order submitted BTC BUY 84000")
        assert f.filter(record) is True
        assert "Order submitted" in record.getMessage()

    def test_private_key_in_message_is_redacted(self):
        f = SecretFilter()
        record = self.make_record("private_key=0xdeadbeef")
        f.filter(record)
        assert "REDACTED" in record.getMessage()
        assert "deadbeef" not in record.getMessage()

    def test_secret_word_triggers_redaction(self):
        f = SecretFilter()
        record = self.make_record("Using secret=abc123")
        f.filter(record)
        assert "REDACTED" in record.getMessage()

    def test_signature_word_triggers_redaction(self):
        f = SecretFilter()
        record = self.make_record("signature=0xabcdef")
        f.filter(record)
        assert "REDACTED" in record.getMessage()

    def test_mnemonic_triggers_redaction(self):
        f = SecretFilter()
        record = self.make_record("mnemonic: word1 word2 word3")
        f.filter(record)
        assert "REDACTED" in record.getMessage()

    def test_case_insensitive_matching(self):
        f = SecretFilter()
        record = self.make_record("PRIVATE_KEY=abc")
        f.filter(record)
        assert "REDACTED" in record.getMessage()

    def test_filter_returns_true_even_when_redacting(self):
        """Filter should always return True — we redact, not drop the record."""
        f = SecretFilter()
        record = self.make_record("private_key=xyz")
        result = f.filter(record)
        assert result is True

    def test_kill_switch_reason_passes(self):
        """Kill switch reason should not be filtered."""
        f = SecretFilter()
        record = self.make_record("KILL SWITCH ACTIVATED: reason=daily_loss_exceeded")
        f.filter(record)
        assert "REDACTED" not in record.getMessage()


class TestSetupLogging:
    def test_setup_returns_logger(self, tmp_path):
        logger = setup_logging(log_level="WARNING", log_dir=tmp_path)
        assert logger is not None

    def test_log_files_created(self, tmp_path):
        setup_logging(log_level="DEBUG", log_dir=tmp_path)
        assert (tmp_path / "bot.log").exists()
        assert (tmp_path / "bot.jsonl").exists()

    def test_secret_not_written_to_file(self, tmp_path):
        setup_logging(log_level="DEBUG", log_dir=tmp_path)
        log = logging.getLogger("test_secret_write")
        log.info("private_key=supersecret123")

        log_content = (tmp_path / "bot.log").read_text()
        assert "supersecret123" not in log_content
        assert "REDACTED" in log_content
