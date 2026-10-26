"""全站共享设置 —— 由管理员统一配置，所有用户共用。

存在 `app_settings` 表里，目前有两项：DeepSeek API Key、高德 Web 服务 Key。

**密钥是加密存的**（AES-256-GCM，见 db/crypto.py），数据库里落的是
`enc:v1:<base64>`，不是明文。读取时自动解密，调用方拿到的仍然是明文，
所以上层代码不需要感知加密这回事。

对外接口的规则：**只暴露"有没有配 + 打码后的样子"**，
任何接口都不返回明文。
"""

from __future__ import annotations

import logging
from datetime import datetime

from db import crypto, database

logger = logging.getLogger("settings")

# 目前只有这两项；以后要加别的开关，往这里加常量即可
DEEPSEEK_API_KEY = "deepseek_api_key"
AMAP_API_KEY = "amap_api_key"

# 需要加密落库的项。其余（比如每天一次的蒸馏标记）是普通值，不加密，
# 免得为了看一眼"今天跑没跑"还要解一次密。
SENSITIVE_KEYS = frozenset({DEEPSEEK_API_KEY, AMAP_API_KEY})


def _decrypt_or_empty(key: str, raw: str) -> str:
    """解密；解不开时记一条日志并按"未配置"处理，不让整个页面崩掉。"""
    if not raw:
        return ""
    if not crypto.is_encrypted(raw):
        return raw                      # 旧版明文，调用方会触发迁移
    plain = crypto.decrypt(raw)
    if not plain:
        logger.warning("设置项 %s 解密失败（主密钥换过？），已按未配置处理", key)
    return plain


def _migrate_plaintext(key: str, raw: str) -> None:
    """把历史遗留的明文就地加密，**保留原来的 updated_by / updated_at**。

    只在读取时发生一次；之后库里就都是密文了。
    """
    try:
        database.execute(
            "UPDATE app_settings SET value = ? WHERE key = ?",
            (crypto.encrypt(raw), key),
        )
        logger.info("设置项 %s 已从明文迁移为密文", key)
    except Exception as e:                              # noqa: BLE001
        logger.warning("设置项 %s 迁移加密失败：%s: %s", key, type(e).__name__, e)


def get(key: str, default: str = "") -> str:
    """读一项设置（敏感项自动解密）。没配返回 default。"""
    database.init_db()
    row = database.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    if row is None:
        return default

    raw = (row["value"] or "").strip()
    if not raw:
        return default

    if key in SENSITIVE_KEYS:
        plain = _decrypt_or_empty(key, raw)
        if plain and not crypto.is_encrypted(raw):
            _migrate_plaintext(key, raw)
        return plain or default

    return raw or default


def set_value(key: str, value: str, updated_by: str = "") -> None:
    """写一项设置（敏感项先加密再落库）。"""
    database.init_db()
    text = (value or "").strip()
    stored = crypto.encrypt(text) if key in SENSITIVE_KEYS and text else text
    database.execute(
        """
        INSERT INTO app_settings (key, value, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value      = excluded.value,
            updated_by = excluded.updated_by,
            updated_at = excluded.updated_at
        """,
        (key, stored, updated_by, database.now_text()),
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
    """给前端看的状态：有没有配、打码后的样子、谁改的、什么时候改的。

    打码用的是**解密后的明文**（头部 6 位 + 尾部 4 位），
    不能拿库里的密文打码 —— 那样显示的是一片随机字符，管理员认不出是哪把 Key。
    """
    database.init_db()
    raw = get(key)
    row = database.query_one(
        "SELECT updated_by, updated_at FROM app_settings WHERE key = ?", (key,)
    )
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
