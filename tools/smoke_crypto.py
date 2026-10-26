"""密钥加密（AES-256-GCM）的冒烟测试（不联网、不碰真实数据库）。

覆盖：
    1. 加解密往返：中文 / 长串 / 空值
    2. 落库的是密文：库里搜不到明文片段
    3. 随机 nonce：同一个 Key 加密两次结果不同
    4. 读取自动解密：settings.deepseek_key() 拿到明文
    5. 懒迁移：旧版明文值在第一次读取时就地变成密文，且 updated_at 不变
    6. 非敏感项不加密（比如"今天跑过没有"的标记）
    7. 密文被篡改 -> 解不开 -> 按"未配置"处理，不抛异常
    8. 主密钥派生：44 字符 base64 与任意口令都能得到 32 字节，且稳定
    9. 状态打码用的是明文（能认出是哪把 Key），但绝不返回明文

用法：
    .venv\\Scripts\\python.exe tools/smoke_crypto.py
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP_DIR = tempfile.mkdtemp(prefix="kh_crypto_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "crypto.db")
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ.pop("KH_SECRET_KEY", None)          # 用文件里的主密钥，别受本机 .env 影响

from db import crypto, database, settings       # noqa: E402

PASS, FAIL = 0, 0
FAKE = "sk-smoke-1234567890abcdefghij"


def check(label, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}" + (f"  -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {label}  -> {extra}")


def main() -> None:
    database.init_db()

    print("\n[1] 加解密往返")
    for label, plain in (("ASCII", FAKE), ("中文", "这是中文密钥😀"), ("长串", "x" * 500)):
        out = crypto.decrypt(crypto.encrypt(plain))
        check(f"{label} 往返一致", out == plain, len(out))
    check("空值不加密", crypto.encrypt("") == "" and crypto.decrypt("") == "")

    print("\n[2] 密文形态")
    token = crypto.encrypt(FAKE)
    check("带版本前缀", token.startswith(crypto.PREFIX), token[:10])
    check("不含明文片段", FAKE not in token and "smoke" not in token)
    check("两次加密结果不同（随机 nonce）", crypto.encrypt(FAKE) != token)

    print("\n[3] 落库即密文")
    settings.set_value(settings.DEEPSEEK_API_KEY, FAKE, updated_by="smoke")
    row = database.query_one(
        "SELECT value, updated_by FROM app_settings WHERE key = ?",
        (settings.DEEPSEEK_API_KEY,),
    )
    check("库里存的是密文", crypto.is_encrypted(row["value"]), row["value"][:16] + "...")
    check("库里搜不到明文", FAKE not in row["value"])
    check("读取自动解密", settings.deepseek_key() == FAKE)
    check("记录是谁改的", row["updated_by"] == "smoke", row["updated_by"])

    print("\n[4] 状态打码用明文、不给明文")
    status = settings.deepseek_status()
    check("configured=True", status["configured"] is True)
    check("打码保留头尾", status["masked"].startswith("sk-smo") and status["masked"].endswith(FAKE[-4:]),
          status["masked"])
    check("状态里不含明文", FAKE not in str(status))

    print("\n[5] 旧明文懒迁移")
    database.execute(
        "UPDATE app_settings SET value = ?, updated_by = ?, updated_at = ? WHERE key = ?",
        ("sk-legacy-plain-9999", "old-admin", "2026-01-01 08:00", settings.DEEPSEEK_API_KEY),
    )
    check("迁移前库里是明文",
          not crypto.is_encrypted(
              database.query_one("SELECT value FROM app_settings WHERE key = ?",
                                 (settings.DEEPSEEK_API_KEY,))["value"]))
    check("读取得到原值", settings.deepseek_key() == "sk-legacy-plain-9999")
    migrated = database.query_one(
        "SELECT value, updated_by, updated_at FROM app_settings WHERE key = ?",
        (settings.DEEPSEEK_API_KEY,),
    )
    check("读取后自动变密文", crypto.is_encrypted(migrated["value"]))
    check("保留原修改人与时间",
          migrated["updated_by"] == "old-admin" and migrated["updated_at"] == "2026-01-01 08:00",
          f"{migrated['updated_by']} {migrated['updated_at']}")
    check("迁移后仍能解密", crypto.decrypt(migrated["value"]) == "sk-legacy-plain-9999")

    print("\n[6] 非敏感项不加密")
    settings.set_value("memory_distill:last_run:13800000000", "2026-10-26", updated_by="system")
    plain = database.query_one(
        "SELECT value FROM app_settings WHERE key = ?", ("memory_distill:last_run:13800000000",)
    )["value"]
    check("标记保持明文", plain == "2026-10-26", plain)

    print("\n[7] 密文被篡改")
    good = crypto.encrypt(FAKE)
    tampered = crypto.PREFIX + ("A" if good[len(crypto.PREFIX)] != "A" else "B") + good[len(crypto.PREFIX) + 1:]
    database.execute(
        "UPDATE app_settings SET value = ? WHERE key = ?", (tampered, settings.AMAP_API_KEY))
    check("解不开返回空而不是抛异常", settings.amap_key() == "")
    check("状态显示未配置", settings.amap_status()["configured"] is False)

    print("\n[8] 主密钥派生")
    import base64

    raw = os.urandom(32)
    b64 = base64.urlsafe_b64encode(raw).decode()
    k1 = crypto._from_text(b64)
    k2 = crypto._from_text(b64)
    check("base64 的 32 字节直接还原", k1 == raw and len(k1) == 32)
    check("同一输入派生结果稳定", k1 == k2)
    check("任意口令也能派生出 32 字节", len(crypto._from_text("随便一句很长的话")) == 32)
    check("不同口令派生不同密钥", crypto._from_text("a") != crypto._from_text("b"))
    check("空口令报错", _raises(lambda: crypto._from_text(" ")))
    check("指纹是 12 位十六进制", len(crypto.fingerprint()) == 12, crypto.fingerprint())

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    print(f"临时目录留在 {TMP_DIR}（可以删掉）")
    sys.exit(1 if FAIL else 0)


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                                   # noqa: BLE001
        return True
    return False


if __name__ == "__main__":
    main()
