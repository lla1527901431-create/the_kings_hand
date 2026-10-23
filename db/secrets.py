"""用户自己填的 API Key。

⚠️ 安全说明（按用户明确要求实现）：
    演示版把密钥**明文**存在 SQLite 里，没有加密。这带来两个后果：
      * data/app.db 这个文件被任何人拿到，就等于拿到了明文密钥
      * 备份/拷贝项目文件夹时，密钥跟着走
    仅适合本机自用。如果以后要给别人用或上公网，应改回"服务端加密存储"
    （原项目 the_kings_hand 里的 agent/crypto.py 就是那个实现，可移植回来）。

对外只暴露"有没有配"，任何接口都不返回明文。
"""

from __future__ import annotations

from db import database, settings


def get_key(phone: str) -> str:
    """取 DeepSeek Key（明文）。没配返回空串。"""
    database.init_db()
    row = database.query_one(
        "SELECT deepseek_key FROM user_secrets WHERE phone = ?", (phone,)
    )
    return (row["deepseek_key"] if row else "") or ""


def resolve_deepseek_key(phone: str) -> tuple[str, str]:
    """决定这次对话该用哪把 Key，并说明它的来源。

    优先级：**管理员统一配置的 > 用户自己的**
    —— 演示版的定位是"管理员配好服务，其他人直接能用"，所以全局 Key 优先。
    user_secrets 保留作兜底：管理员还没配时，个人以前填的 Key 仍然有效。

    @return (key, source)，source 取值为 "shared" | "own" | ""
    """
    shared = settings.deepseek_key()
    if shared:
        return shared, "shared"
    own = get_key(phone)
    return (own, "own") if own else ("", "")


def save(phone: str, deepseek_key: str = "", smtp_user: str = "", smtp_pass: str = "") -> dict:
    """合并保存。空字符串表示"不修改该字段"，不是清空。"""
    database.init_db()
    row = database.query_one(
        "SELECT deepseek_key, smtp_user, smtp_pass FROM user_secrets WHERE phone = ?", (phone,)
    )
    current = dict(row) if row else {"deepseek_key": "", "smtp_user": "", "smtp_pass": ""}

    new_key = (deepseek_key or "").strip() or current.get("deepseek_key", "")
    new_user = (smtp_user or "").strip() or current.get("smtp_user", "")
    new_pass = (smtp_pass or "").strip() or current.get("smtp_pass", "")

    database.execute(
        """
        INSERT INTO user_secrets (phone, deepseek_key, smtp_user, smtp_pass, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            deepseek_key = excluded.deepseek_key,
            smtp_user    = excluded.smtp_user,
            smtp_pass    = excluded.smtp_pass,
            updated_at   = excluded.updated_at
        """,
        (phone, new_key, new_user, new_pass, database.now_text()),
    )
    return status(phone)


def status(phone: str) -> dict:
    """给前端看的状态：只告诉"有没有配"，绝不返回明文。"""
    database.init_db()
    row = database.query_one(
        "SELECT deepseek_key, smtp_user, smtp_pass, updated_at FROM user_secrets WHERE phone = ?",
        (phone,),
    )
    if row is None:
        return {"has_deepseek_key": False, "smtp_user": "", "has_smtp_pass": False, "updated_at": ""}
    return {
        "has_deepseek_key": bool((row["deepseek_key"] or "").strip()),
        "smtp_user": row["smtp_user"] or "",
        "has_smtp_pass": bool((row["smtp_pass"] or "").strip()),
        "updated_at": row["updated_at"] or "",
    }


def clear(phone: str) -> None:
    """清空该用户的密钥（删掉整行）。"""
    database.init_db()
    database.execute("DELETE FROM user_secrets WHERE phone = ?", (phone,))
