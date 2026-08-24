from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

SENSITIVE_KEYS = {"token", "api_key", "phone", "telegram_id", "transcript", "media_path"}


def mask(value: object) -> str:
    text = str(value)
    if len(text) <= 4:
        return "****"
    return f"***{text[-4:]}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "update_id", "job_id", "publication_id", "error"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = mask(value) if key in SENSITIVE_KEYS else value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
