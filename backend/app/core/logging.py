"""Centralised logging configuration for PR•VISION.

Rules enforced here:
- Single structured console format, level driven by LOG_LEVEL.
- Never log API keys, passwords, tokens or other secrets (a redacting filter
  strips common secret-bearing patterns from every record).
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Patterns whose values must never appear in logs.
_SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|access[_-]?token|client[_-]?secret|password|bearer)\s*[=:]\s*)\S+", re.IGNORECASE),
]


class SecretRedactingFilter(logging.Filter):
    """Defence-in-depth filter that masks secret-looking substrings."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = message
            for pattern in _SECRET_PATTERNS:
                redacted = pattern.sub(r"\1***REDACTED***", redacted)
            if redacted != message:
                record.msg = redacted
                record.args = None
        except Exception:  # never break logging
            pass
        return True


_configured = False


def configure_logging(level: Optional[str] = None) -> None:
    """Idempotent root logging setup."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel((level or "INFO").upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)

    # Quiet the noisier third-party loggers.
    for noisy in ("urllib3", "httpx", "httpcore", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
