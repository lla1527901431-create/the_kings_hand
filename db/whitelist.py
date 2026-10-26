"""手机号白名单。

登录规则（按用户要求）：
    * 用户先输手机号，后端核对是否在名单里
    * 不在名单里 -> 提示"抱歉，您没有权限访问"
    * **第一个成功登录的手机号自动成为管理员，并且当场设置密码**
    * 之后管理员每次登录都要验证密码；普通成员沿用"知道号码即可登录"

为什么要给管理员加密码：
    管理员能增删访问名单、能改全站共用的 AI 服务密钥，
    只靠"知道手机号"保护太弱了（这个 demo 是要放到公网给人看的）。

密码怎么存：
    PBKDF2-HMAC-SHA256 + 每账号随机 salt，存成 pbkdf2_sha256$<salt>$<hash>。
    不用 MD5/SHA1 裸哈希，也不自己造轮子；标准库自带足够。

⚠️ 仍然没有短信验证码：这是演示/内部场景，加短信要接服务商、要花钱。
   普通成员那部分依然是"知道号码即可登录"，README 的安全说明里写明了。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime

from db import database

# 中国大陆手机号：1 开头，第二位 3-9，共 11 位
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# PBKDF2 迭代次数：本机登录一次 ~80ms，够用又不拖慢页面
PBKDF2_ROUNDS = 200_000
MIN_PASSWORD_LENGTH = 6


class PhoneInvalid(ValueError):
    """手机号格式不对。"""


class PasswordTooShort(ValueError):
    """密码太短。"""


def normalize(phone: str) -> str:
    """清洗手机号：去掉空格和常见分隔符。格式不对时抛 PhoneInvalid。"""
    text = re.sub(r"[\s\-()（）]", "", phone or "")
    if not PHONE_RE.match(text):
        raise PhoneInvalid("请输入 11 位手机号，例如 13800138000")
    return text


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def count() -> int:
    database.init_db()
    row = database.query_one("SELECT COUNT(*) AS n FROM whitelist")
    return row["n"] if row else 0


def count_admins() -> int:
    database.init_db()
    row = database.query_one("SELECT COUNT(*) AS n FROM whitelist WHERE is_admin = 1")
    return row["n"] if row else 0


def is_allowed(phone: str) -> bool:
    """这个手机号能不能登录。"""
    database.init_db()
    row = database.query_one("SELECT phone FROM whitelist WHERE phone = ?", (phone,))
    return row is not None


def get(phone: str) -> dict | None:
    database.init_db()
    row = database.query_one(
        "SELECT phone, name, is_admin, note, created_at, last_login FROM whitelist WHERE phone = ?",
        (phone,),
    )
    return _row_to_dict(row) if row else None


def is_admin(phone: str) -> bool:
    database.init_db()
    row = database.query_one("SELECT is_admin FROM whitelist WHERE phone = ?", (phone,))
    return bool(row and row["is_admin"])


def list_all() -> list[dict]:
    """管理员用的名单列表。"""
    database.init_db()
    rows = database.query(
        "SELECT phone, name, is_admin, note, created_at, last_login "
        "FROM whitelist ORDER BY is_admin DESC, created_at ASC"
    )
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    return {
        "phone": row["phone"],
        "name": row["name"] or "",
        "is_admin": bool(row["is_admin"]),
        "note": row["note"] or "",
        "created_at": row["created_at"] or "",
        "last_login": row["last_login"] or "",
    }


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def add(phone: str, name: str = "", note: str = "", is_admin: bool = False) -> dict:
    """把一个手机号加入白名单（已存在则更新备注，不会重复插入）。"""
    database.init_db()
    database.execute(
        """
        INSERT INTO whitelist (phone, name, note, is_admin) VALUES (?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            name  = CASE WHEN excluded.name  <> '' THEN excluded.name  ELSE whitelist.name  END,
            note  = CASE WHEN excluded.note  <> '' THEN excluded.note  ELSE whitelist.note  END,
            is_admin = MAX(whitelist.is_admin, excluded.is_admin)
        """,
        (phone, name or "", note or "", 1 if is_admin else 0),
    )
    return get(phone) or {}


def remove(phone: str) -> bool:
    """从白名单移除（会话会被级联删除）。返回是否删掉了。"""
    database.init_db()
    n = database.execute("DELETE FROM whitelist WHERE phone = ?", (phone,))
    return n > 0


def set_admin(phone: str, is_admin: bool) -> bool:
    """设置/取消管理员。返回是否改到了行。

    注意：调用方要先确认"不会把最后一个管理员降级"，否则没人能管理名单了。
    """
    database.init_db()
    n = database.execute(
        "UPDATE whitelist SET is_admin = ? WHERE phone = ?",
        (1 if is_admin else 0, phone),
    )
    return n > 0


def touch_login(phone: str) -> None:
    """记录最近登录时间。"""
    database.execute(
        "UPDATE whitelist SET last_login = ? WHERE phone = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M"), phone),
    )


# ---------------------------------------------------------------------------
# 管理员密码
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """把明文密码变成 pbkdf2_sha256$<salt>$<hash>。每次 salt 都是新的。"""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise PasswordTooShort(f"密码至少需要 {MIN_PASSWORD_LENGTH} 位")
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(stored: str, password: str) -> bool:
    """校验明文密码对不对。用 compare_digest 避免时序侧信道。"""
    try:
        algo, salt, digest = (stored or "").split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256" or not password:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()
    return hmac.compare_digest(candidate, digest)


def get_password_hash(phone: str) -> str:
    database.init_db()
    row = database.query_one("SELECT password_hash FROM whitelist WHERE phone = ?", (phone,))
    return (row["password_hash"] if row else "") or ""


def has_password(phone: str) -> bool:
    """这个账号有没有设过密码。"""
    return bool(get_password_hash(phone))


def set_password(phone: str, password: str) -> None:
    """设置（或重置）密码。"""
    database.init_db()
    database.execute(
        "UPDATE whitelist SET password_hash = ?, password_set_at = ? WHERE phone = ?",
        (hash_password(password), database.now_text(), phone),
    )


def check_password(phone: str, password: str) -> bool:
    """登录校验：hash 存在且密码匹配才算过。"""
    stored = get_password_hash(phone)
    return bool(stored) and verify_password(stored, password)


def clear_password(phone: str) -> None:
    """清掉密码（下次登录会走"设置密码"流程）。取消管理员权限时会用到。"""
    database.execute(
        "UPDATE whitelist SET password_hash = '', password_set_at = NULL WHERE phone = ?",
        (phone,),
    )
