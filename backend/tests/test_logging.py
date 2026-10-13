import json
import logging

from app.core.logging import JsonFormatter


def test_logs_drop_sensitive_messages() -> None:
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "password-secret", (), None)
    record.event = "request_failed"
    record.request_id = "858b9952-6e19-4db4-96d3-39760e6ed647"
    record.authorization = "Bearer secret"
    result = JsonFormatter().format(record)
    assert "password-secret" not in result
    assert "Bearer secret" not in result
    assert json.loads(result)["event"] == "request_failed"
