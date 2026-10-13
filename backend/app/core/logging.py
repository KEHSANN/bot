import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Allowlisted metadata only: never serialize arbitrary messages or exception bodies."""

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "application_log"),
        }
        for key in ("request_id", "method", "status", "duration_ms", "exception_type"):
            value = getattr(record, key, None)
            if value is not None:
                fields[key] = value
        return json.dumps(fields, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
