"""Request-id log correlation.

Binds a per-request id into a contextvar so every log line emitted during a
generate/modify/execute request carries it. Without this, concurrent requests'
log lines interleave with no way to group them — making prod incidents hard to debug.
"""
import logging
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Injects `request_id` into every LogRecord so the format string can show it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True
