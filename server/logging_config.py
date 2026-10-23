"""日志配置（复制自原项目，去掉与 Supabase 相关的部分）。

为什么不用 print：生产/排障时 print 没有时间戳、级别、请求与用户标识，
出问题无法回答"什么时候、谁、哪一类"。这里统一格式：

    2026-09-28 15:42:11 | WARNING | chat | req=7f3a u=138****0000 | DeepSeek 超时
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

_ctx_request_id: ContextVar[str] = ContextVar("log_request_id", default="-")
_ctx_user_id: ContextVar[str] = ContextVar("log_user_id", default="-")

_configured = False

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-6s | req=%(request_id)s u=%(user_id)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def set_request_id(value: str) -> None:
    _ctx_request_id.set(value or "-")


def set_user_id(value: str) -> None:
    _ctx_user_id.set(value or "-")


def mask_phone(phone: str) -> str:
    """日志里不写完整手机号：138****0000。"""
    phone = phone or "-"
    if len(phone) != 11:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", None) or _ctx_request_id.get()
        record.user_id = mask_phone(getattr(record, "user_id", None) or _ctx_user_id.get())
        return True


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    level = (level or "INFO").upper()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # 这些库在 INFO 级别非常吵
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
