"""登录会话。

cookie 里只放一个随机 token，真正的身份（手机号）在 sessions 表里查。
这样做的两个好处：
    * 服务端可以主动注销会话（删掉那一行即可）
    * cookie 被伪造也没用 —— token 是随机的，不在表里就无效
"""

from __future__ import annotations

import secrets

from db import database

TOKEN_BYTES = 32


def issue(phone: str) -> str:
    """给这个手机号签发一个新会话，返回 token。"""
    database.init_db()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    database.execute(
        "INSERT INTO sessions (token, phone, expires_at) VALUES (?, ?, ?)",
        (token, phone, database.new_expires_at()),
    )
    return token


def resolve(token: str) -> str | None:
    """token -> 手机号。无效或过期返回 None（顺手删掉过期行）。"""
    if not token:
        return None
    database.init_db()
    row = database.query_one(
        "SELECT phone, expires_at FROM sessions WHERE token = ?", (token,)
    )
    if row is None:
        return None
    if (row["expires_at"] or "") < database.now_text():
        database.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return row["phone"]


def revoke(token: str) -> None:
    """注销单个会话（退出登录）。"""
    database.execute("DELETE FROM sessions WHERE token = ?", (token,))


def revoke_all(phone: str) -> int:
    """注销该手机号的所有会话（比如被移出白名单时）。"""
    return database.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
