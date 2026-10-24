"""全站共享设置 —— 由管理员统一配置，所有用户共用。

和 db/secrets.py（user_secrets）的分工：

    app_settings   全站一份。目前只有 DeepSeek API Key，
                   管理员在「访问管理」里配一次，所有人的 AI 对话都用它。
    user_secrets   每人一份。**现在不再暴露给 UI**，只保留作兜底：
                   管理员还没配全局 Key 时，个人以前自己填的 Key 仍然能用。

对外接口的规则和用户密钥一样：**只暴露"有没有配 + 打码后的样子"，
任何接口都不返回明文。**
"""

from __future__ import annotations

from datetime import datetime

from db import database

# 目前只有这两项；以后要加别的开关，往这里加常量即可
DEEPSEEK_API_KEY = "deepseek_api_key"
AMAP_API_KEY = "amap_api_key"


def get(key: str, default: str = "") -> str:
    """读一项设置。没配返回空串。"""
    database.init_db()
    row = database.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    if row is None:
        return default
    return (row["value"] or "").strip() or default


def set_value(key: str, value: str, updated_by: str = "") -> None:
    """写一项设置。"""
    database.init_db()
    database.execute(
        """
        INSERT INTO app_settings (key, value, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value      = excluded.value,
            updated_by = excluded.updated_by,
            updated_at = excluded.updated_at
        """,
        (key, value.strip(), updated_by, database.now_text()),
    )


def delete(key: str) -> bool:
    """删掉一项设置（等于恢复未配置状态）。"""
    database.init_db()
    return database.execute("DELETE FROM app_settings WHERE key = ?", (key,)) > 0


def mask(text: str) -> str:
    """打码：只留头尾，中间一律打星。给界面显示用，避免泄露整串密钥。"""
    raw = (text or "").strip()
    if len(raw) <= 10:
        return "*" * len(raw)
    return f"{raw[:6]}{'*' * 8}{raw[-4:]}"


def _status(key: str) -> dict:
    """给前端看的状态：有没有配、打码后的样子、谁改的、什么时候改的。"""
    database.init_db()
    row = database.query_one(
        "SELECT value, updated_by, updated_at FROM app_settings WHERE key = ?",
        (key,),
    )
    raw = (row["value"] if row else "") or ""
    return {
        "configured": bool(raw.strip()),
        "masked": mask(raw) if raw.strip() else "",
        "updated_by": (row["updated_by"] if row else "") or "",
        "updated_at": (row["updated_at"] if row else "") or "",
    }


def deepseek_key() -> str:
    """管理员统一配置的那把 Key（明文，只在服务端内部用）。"""
    return get(DEEPSEEK_API_KEY)


def deepseek_status() -> dict:
    return _status(DEEPSEEK_API_KEY)


# ---------------------------------------------------------------------------
# 高德（路况 / 地理编码）
# ---------------------------------------------------------------------------
def amap_key() -> str:
    """管理员配置的高德 Web 服务 Key（明文，只在服务端内部用）。"""
    return get(AMAP_API_KEY)


def amap_status() -> dict:
    return _status(AMAP_API_KEY)
