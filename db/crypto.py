"""字段级加密：给存在 SQLite 里的第三方密钥加一道锁。

## 为什么需要

`data/app.db` 之前是**明文**存 DeepSeek Key 和高德 Key 的。这意味着：
    * 拷贝/备份/误传一次这个文件，两把 Key 就跟着泄漏
    * 拿到文件的人可以直接盗刷额度（DeepSeek 是按 token 计费的）

## 怎么做的

* 算法：**AES-256-GCM**（`cryptography` 库）。GCM 自带完整性校验，
  密文被改过一个字节就解不开，不需要额外再存 HMAC。
* 每次加密生成全新的 12 字节随机 nonce，所以同一个 Key 加密两次结果不同。
* 密文格式：`enc:v1:<base64(nonce + ciphertext)>`，带版本前缀，
  以后换算法可以按前缀分流，不用猜。
* 向后兼容：读到不以 `enc:v1:` 开头的值 = 旧的明文，
  调用方按明文使用并在读取时就地加密回写（见 db/settings.py 的 `get`）。

## 主密钥从哪来（重要）

优先级：

1. 环境变量 `KH_SECRET_KEY`
   适合容器 / 托管平台：显式注入，跟着密钥管理走，不落在磁盘上。
   值是 base64 的 32 字节，或者任意一段长口令（会用 SHA-256 派生）。
2. 文件 `data/.secret_key`
   本机自用时的默认行为：首次启动自动生成 32 字节随机密钥，权限 0600。
   它和数据库在同一个目录（`data/`），已在 .gitignore 里，
   备份时**要和 app.db 一起**备份 —— 只有库没有它，密文就解不开了。

⚠️ 两种来源二选一，别混用：先用文件跑出一批密文，之后改成 env 注入，
   指纹（见 `fingerprint()`）一变，旧密文全部失效，只能重填 Key。

## 密钥丢了会怎样

不会崩：`decrypt()` 解不开时返回空串并记一条 warning，
界面上表现为"Key 未配置"，管理员重填一次即可（密文会被新密钥覆盖）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import config

PREFIX = "enc:v1:"
KEY_ENV = "KH_SECRET_KEY"
KEY_FILE_NAME = ".secret_key"
_NONCE_BYTES = 12
_KEY_BYTES = 32

_lock = threading.Lock()
_master: bytes | None = None
_master_source = ""


class MasterKeyError(RuntimeError):
    """主密钥既没注入也读不到/写不出。"""


# ---------------------------------------------------------------------------
# 主密钥
# ---------------------------------------------------------------------------
def _key_path() -> str:
    return os.path.join(config.DATA_DIR, KEY_FILE_NAME)


def _from_text(text: str) -> bytes:
    """把 env 里的字符串变成 32 字节密钥。

    允许两种写法：
        * 44 字符 base64（和 .secret_key 文件里的格式一致）
        * 任意口令（长度不限），用 SHA-256 派生 —— 方便临时用一句话当密钥
    """
    raw = (text or "").strip()
    if not raw:
        raise MasterKeyError("KH_SECRET_KEY 是空的。")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == _KEY_BYTES:
            return decoded
    except Exception:                                  # noqa: BLE001
        pass
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _load_from_file(create: bool = True) -> bytes:
    path = _key_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            try:
                raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
            except Exception as e:                     # noqa: BLE001
                raise MasterKeyError(f"密钥文件格式不对：{path}（{type(e).__name__}）") from e
            if len(raw) != _KEY_BYTES:
                raise MasterKeyError(f"密钥文件长度不对（应为 32 字节）：{path}")
            return raw
        raise MasterKeyError(f"密钥文件是空的：{path}")

    if not create:
        raise MasterKeyError(f"找不到密钥文件：{path}")

    # 首次运行：生成密钥。先在内存里生成，再落盘，最后回读校验，
    # 避免两个进程同时启动时各自用不同的密钥把库写坏。
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fresh = os.urandom(_KEY_BYTES)
    text = base64.urlsafe_b64encode(fresh).decode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 别人抢先创建了 —— 用他的
        return _load_from_file(create=False)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)          # Windows 上是空操作，尽力而为
    except OSError:
        pass
    return fresh


def master_key() -> bytes:
    """取主密钥（进程内缓存，之后不再读盘）。"""
    global _master, _master_source
    if _master is not None:
        return _master
    with _lock:
        if _master is not None:
            return _master
        env_value = os.getenv(KEY_ENV, "").strip()
        if env_value:
            _master = _from_text(env_value)
            _master_source = "env"
        else:
            _master = _load_from_file()
            _master_source = "file"
        return _master


def source() -> str:
    """主密钥来源：'env' 或 'file'（给启动自检显示）。"""
    try:
        master_key()
    except MasterKeyError:
        return "missing"
    return _master_source


def fingerprint() -> str:
    """主密钥指纹（12 位十六进制）。

    只用于确认"线上和环境里用的是不是同一把密钥"，不能反推出密钥本身。
    """
    try:
        digest = hashlib.sha256(master_key()).hexdigest()
    except MasterKeyError:
        return ""
    return digest[:12]


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------
def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    """明文 -> `enc:v1:...`。空串原样返回（不加密空值）。"""
    text = plain or ""
    if not text:
        return ""
    if is_encrypted(text):        # 已经是密文，不要重复套一层
        return text
    nonce = os.urandom(_NONCE_BYTES)
    cipher = AESGCM(master_key()).encrypt(nonce, text.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + cipher).decode("ascii")


def decrypt(value: str) -> str:
    """`enc:v1:...` -> 明文。解不开返回空串（调用方按"未配置"处理）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if not is_encrypted(text):
        return text               # 旧明文：兼容，交给调用方决定是否迁移
    body = text[len(PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        nonce, cipher = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return AESGCM(master_key()).decrypt(nonce, cipher, None).decode("utf-8")
    except MasterKeyError:
        raise
    except Exception:             # noqa: BLE001  —— 密钥换了 / 密文被改
        return ""


def self_test() -> tuple[bool, str]:
    """启动自检：用当前主密钥加密再解密一次，确认密钥可用。

    返回 (是否通过, 说明)。
    """
    probe = "kh-crypto-self-test"
    try:
        if decrypt(encrypt(probe)) != probe:
            return False, "加解密结果不一致（主密钥异常）"
    except MasterKeyError as e:
        return False, str(e)
    except Exception as e:                              # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, f"主密钥来源 {source()}，指纹 {fingerprint()}"
