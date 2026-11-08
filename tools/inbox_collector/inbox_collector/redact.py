"""Logging that cannot carry message bodies or secrets."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_QUERY_SECRET = re.compile(r"([?&](?:key|token|secret|access_token|authorization)=)[^&\s]+", re.IGNORECASE)

LOGGER_NAME = "inbox_collector"
_ALLOWED_FIELDS = {"state", "count", "mode", "alert", "attempt"}


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [item for item in secrets if item]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, "[redacted]")
        message = _BEARER.sub("Bearer [redacted]", message)
        message = _QUERY_SECRET.sub(r"\1[redacted]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(log_path: Path, secrets: list[str] | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(log_path.parent, 0o700)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(RedactFilter(secrets or []))
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.info("logging_started")
    os.chmod(log_path, 0o600)


def log_event(code: str, **fields: object) -> None:
    safe: dict[str, object] = {}
    for key in _ALLOWED_FIELDS:
        value = fields.get(key)
        if isinstance(value, (int, str)) and len(str(value)) <= 64:
            safe[key] = value
    logging.getLogger(LOGGER_NAME).info("%s %s", code, json.dumps(safe, ensure_ascii=False))
