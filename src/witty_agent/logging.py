"""统一日志入口。业务和 harness 都从这里取 logger，不要自己 basicConfig。"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar

LOGGER_NAME = "witty_agent"
_TRACE_ID: ContextVar[str | None] = ContextVar("witty_trace_id", default=None)
_CONFIGURED = False

_SECRET_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization")


def set_trace_id(trace_id: str | None) -> None:
    _TRACE_ID.set(trace_id)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def redact(value: object) -> str:
    text = str(value)
    lowered = text.lower()
    if any(key in lowered for key in _SECRET_KEYS):
        return "<redacted>"
    return text


class _TraceAdapter(logging.LoggerAdapter):
    def process(self, msg: object, kwargs: dict) -> tuple[object, dict]:
        trace = get_trace_id() or "-"
        return f"trace={trace} {msg}", kwargs


def setup_logging(*, level: str | None = None, force: bool = False) -> None:
    """只初始化 witty_agent 这一棵 logger，不改根 logger。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    if level:
        name_level = level
    else:
        env_level = os.environ.get("WITTY_LOG_LEVEL")
        if env_level:
            name_level = env_level
        else:
            from witty_agent.runtime import logging_level

            name_level = logging_level()
    name_level = name_level.upper()
    numeric = getattr(logging, name_level, logging.INFO)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(numeric)
    logger.propagate = False
    if force:
        logger.handlers.clear()
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(numeric)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> logging.LoggerAdapter:
    """统一入口。name 用模块短名，例如 get_logger('skills')。"""
    if not _CONFIGURED:
        setup_logging()
    if not name:
        full = LOGGER_NAME
    elif name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}."):
        full = name
    else:
        full = f"{LOGGER_NAME}.{name}"
    return _TraceAdapter(logging.getLogger(full), extra={})
