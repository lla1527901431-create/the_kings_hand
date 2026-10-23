"""集中配置。

这个演示项目与 the_kings_hand 的区别：
    * 数据存在本地 SQLite，不用 Supabase
    * 登录用手机号白名单，不用邮箱 + Auth 服务
      —— 管理员额外需要密码，密码在 db/whitelist.py 里用 PBKDF2 存
    * 因此这里几乎没有敏感配置 —— 唯一的密钥是管理员在页面上
      统一配置的那把 DeepSeek API Key（存在 db/app_settings 表里）

所有模块都从这里取配置，不要在别处直接 os.getenv。
"""

import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# .env 只作为兜底：已存在的系统环境变量优先（load_dotenv 默认不覆盖）
load_dotenv(os.path.join(BASE_DIR, ".env"))


def get_env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
# SQLite 文件放在 data/ 下；整个 data 目录可以打包带走
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = get_env("DB_PATH") or os.path.join(DATA_DIR, "app.db")

# ---------------------------------------------------------------------------
# 运行模式
# ---------------------------------------------------------------------------
# 托管环境（PaaS / 部署沙箱 / Docker）的标志是「外部显式注入了 PORT」。
# 本地没人会去设 PORT（默认就是 8000），所以这个判断是可靠的：
#     * 托管部署 -> 必须绑 0.0.0.0，否则反向代理连不进来
#     * 托管部署 -> 必须关掉 reload，否则 uvicorn 会额外起子进程抢端口
_MANAGED = bool(os.getenv("PORT"))

HOST = get_env("HOST") or ("0.0.0.0" if _MANAGED else "127.0.0.1")
PORT = int(get_env("PORT", "8000") or "8000")
DEBUG = as_bool(get_env("DEBUG", "true")) and not _MANAGED   # 本地演示默认开热重载
LOG_LEVEL = get_env("LOG_LEVEL", "INFO")

# 会话 cookie 是否带 Secure。本地 http 必须 false，否则浏览器拒绝保存 cookie。
COOKIE_SECURE = as_bool(get_env("COOKIE_SECURE", "false"))

# 登录会话有效期（天）。白名单登录不需要 refresh 机制，过期就重新输手机号。
SESSION_DAYS = int(get_env("SESSION_DAYS", "30"))

# 对话上下文：保留最近多少轮 + 多久没活动就回收
RECENT_HISTORY_MAX = int(get_env("RECENT_HISTORY_MAX", "10"))
RECENT_HISTORY_IDLE_MINUTES = int(get_env("RECENT_HISTORY_IDLE_MINUTES", "120"))

# ---------------------------------------------------------------------------
# DeepSeek（模型与地址是服务端配置；key 由用户自己在页面上填）
# ---------------------------------------------------------------------------
DEEPSEEK_BASE_URL = get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = get_env("DEEPSEEK_MODEL", "deepseek-v4-flash")
MEMORY_MODEL = get_env("MEMORY_MODEL", "") or DEEPSEEK_MODEL


def startup_check() -> list[str]:
    """启动自检，返回提醒列表。目的是让配置问题在启动时就暴露出来。"""
    warnings: list[str] = []

    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isdir(DATA_DIR):
        raise RuntimeError(f"无法创建数据目录：{DATA_DIR}")

    warnings.append(f"数据库文件：{DB_PATH}")

    # 手机号白名单为空时提示怎么启动（这是最容易卡住的第一步）
    try:
        from db import whitelist

        total = whitelist.count()
        admins = whitelist.count_admins()
        if total == 0:
            warnings.append(
                "访问名单为空：**第一个登录的手机号会自动成为管理员**。"
                "打开页面输入你的手机号即可开始；"
                "也可以先执行 python tools/add_phone.py 手机号 预先添加。"
            )
        else:
            warnings.append(f"访问名单共 {total} 个手机号，其中管理员 {admins} 个")
    except Exception as e:
        warnings.append(f"读取白名单失败（首次启动会自动建表）：{type(e).__name__}: {e}")

    if not COOKIE_SECURE and not _MANAGED:
        warnings.append("COOKIE_SECURE=false：仅适合本机 http。实际 cookie 的 Secure 标记会"
                        "按请求的 X-Forwarded-Proto 自动判断，HTTPS 下线会自动带 Secure。")

    return warnings
