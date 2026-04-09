"""
Telemetry: structured + human-readable logging.

Security requirements:
- Private key, wallet address, and signatures MUST NOT appear in logs.
- SecretFilter strips known sensitive field names from all log records.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# Fields that must never appear in logs
_SECRET_PATTERNS = re.compile(
    r"(private.?key|secret|signature|sig|mnemonic)",
    re.IGNORECASE,
)


class SecretFilter(logging.Filter):
    """Drop any log record that contains secret field names in its message."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        if _SECRET_PATTERNS.search(msg):
            # Replace the message rather than dropping entirely so
            # we know a sensitive log was suppressed
            record.msg = "[REDACTED: log contained sensitive field]"
            record.args = ()
        return True


class StructuredFormatter(logging.Formatter):
    """Format log records as newline-delimited JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
        }
        # Attach extra structured fields if present
        for attr in ("symbol", "action", "side", "price", "size",
                     "order_id", "cloid", "inventory", "pnl", "reason"):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[Path] = None,
) -> logging.Logger:
    """
    Configure root logger with:
      - StreamHandler (human-readable, INFO+)
      - FileHandler for bot.log (human-readable, INFO+)
      - FileHandler for bot.jsonl (structured JSON, DEBUG+)

    Returns the root logger.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    secret_filter = SecretFilter()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    console.addFilter(secret_filter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        # Human-readable file
        fh = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        fh.addFilter(secret_filter)
        root.addHandler(fh)

        # Structured JSON file
        jh = logging.FileHandler(log_dir / "bot.jsonl", encoding="utf-8")
        jh.setLevel(logging.DEBUG)
        jh.setFormatter(StructuredFormatter())
        jh.addFilter(secret_filter)
        root.addHandler(jh)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
