"""鉴权：从 HttpOnly cookie 里取出用户 JWT，交给 Supabase 去验证。

一个刻意的设计决定 —— 这里**不自己验证 JWT 签名**：

    真正决定"你是谁"的不是这里，而是数据库。我们把 token 原样转给
    PostgREST，由 Supabase 验签，让 auth.uid() 决定能看到哪些行。
    伪造的 token 会在数据库那层被拒绝（实测返回 401 PGRST301）。

本模块负责：
  1. 从 cookie（或 Authorization 头，便于测试/脚本）取 token；
  2. 解析载荷做**早期失败**（格式不对 / 已过期就直接告诉前端去续期）；
  3. 写/清 cookie（登入登出）；
  4. CSRF 双提交校验 —— cookie 方案必须付的代价。

绝不要相信这里解出来的 user_id 去做数据过滤，过滤交给 RLS。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from contextlib import contextmanager

from fastapi import Header, HTTPException, Request, Response

from agent import db

ACCESS_COOKIE = "kh_access"
REFRESH_COOKIE = "kh_refresh"
CSRF_COOKIE = "kh_csrf"
CSRF_HEADER = "x-csrf-token"

# cookie 路径：refresh 只在续期接口用得到，缩小暴露面
REFRESH_PATH = "/session"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class TokenExpired(Exception):
    """access_token 过期。前端收到这个信号后应当去调 /session/refresh。"""


class TokenInvalid(Exception):
    """token 结构不对 / 不是登录身份。前端应当直接回登录页。"""


class TokenInfo:
    __slots__ = ("token", "user_id", "claims")

    def __init__(self, token: str, user_id: str, claims: dict):
        self.token = token
        self.user_id = user_id
        self.claims = claims

    def __repr__(self) -> str:  # 绝不要把 token 打进日志
        return f"TokenInfo(user_id={self.user_id!r})"


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_claims(token: str) -> dict:
    """解析 JWT 载荷（不验签）。格式不合法时抛 ValueError。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("token 不是合法的 JWT 结构（应为三段）")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as e:
        raise ValueError(f"token 载荷不是合法 JSON：{e}") from e
    if not isinstance(payload, dict):
        raise ValueError("token 载荷不是对象")
    return payload


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _token_from(request: Request, authorization: str | None) -> str:
    """优先 cookie（浏览器路径），其次 Authorization 头（脚本/测试路径）。"""
    token = (request.cookies.get(ACCESS_COOKIE) or "").strip()
    if token:
        return token
    return _extract_bearer(authorization)


async def current_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> TokenInfo:
    """FastAPI 依赖：校验并注入当前用户。

        def endpoint(user: TokenInfo = Depends(current_token)): ...
    """
    token = _token_from(request, authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="未登录。",
            headers={"X-Auth-Code": "no_session"},
        )

    try:
        claims = decode_claims(token)
    except ValueError as e:
        raise HTTPException(
            status_code=401,
            detail=f"登录凭证无法解析：{e}",
            headers={"X-Auth-Code": "invalid_token"},
        ) from e

    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="登录凭证里没有用户标识。",
            headers={"X-Auth-Code": "invalid_token"},
        )

    # 让后续所有日志自动带上这个用户（不用每处手写）
    from server.logging_config import set_user_id

    set_user_id(user_id)

    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        # 交给上层去续期，而不是直接让用户重新登录
        raise HTTPException(
            status_code=401,
            detail="登录已过期，正在续期。",
            headers={"X-Auth-Code": "token_expired"},
        )

    role = claims.get("role")
    if role and role != "authenticated":
        raise HTTPException(status_code=403, detail="该凭证不是登录用户身份。")

    db.set_current_token(token)
    return TokenInfo(token=token, user_id=user_id, claims=claims)


async def csrf_protect(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """CSRF 双提交校验。

    cookie 会自动被浏览器带上，所以别的网站可以诱导你的浏览器发请求。
    防法：登录时额外下发一个**非 HttpOnly** 的随机值 kh_csrf，
    前端把它放进 X-CSRF-Token 头。攻击者的站点读不到这个 cookie
    （同源策略），所以伪造不出这个头。
    """
    if request.method.upper() in SAFE_METHODS:
        return

    cookie_value = (request.cookies.get(CSRF_COOKIE) or "").strip()
    header_value = (x_csrf_token or "").strip()

    if not cookie_value:
        raise HTTPException(
            status_code=401,
            detail="未登录。",
            headers={"X-Auth-Code": "no_session"},
        )
    if not header_value or not secrets.compare_digest(cookie_value, header_value):
        raise HTTPException(
            status_code=403,
            detail="请求校验失败（CSRF）。请刷新页面后重试。",
            headers={"X-Auth-Code": "csrf_failed"},
        )


# ---------------------------------------------------------------------------
# Cookie 读写
# ---------------------------------------------------------------------------
def _secure_flag() -> bool:
    """生产环境（HTTPS）必须开 Secure。本机 http 调试时可通过配置关掉。"""
    import config

    return bool(getattr(config, "COOKIE_SECURE", False))


def set_session_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    access_max_age: int = 3600,    #短期，1 hour
) -> str:
    """登录/续期成功后写入两条 HttpOnly cookie + 一条 CSRF cookie。

    返回生成的 csrf token（前端需要它放进请求头）。
    """
    csrf = secrets.token_urlsafe(24)
    secure = _secure_flag()

    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=access_max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=60 * 60 * 24 * 30,   #长期，1 mouth
        httponly=True,
        samesite="lax",
        secure=secure,
        path=REFRESH_PATH,
    )
    # 这条必须能被 JS 读到，否则前端拿不到它放进请求头
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=60 * 60 * 24 * 30,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return csrf


def clear_session_cookies(response: Response) -> None:
    """清 cookie。

    注意：cookie 的身份是 (名字, 域, 路径)。删除时必须覆盖**所有可能被写过的路径**，
    否则浏览器会留下一份旧 cookie —— 表现为"点了登出，刷新后居然还是登录状态"。
    所以这里把 "/" 和 "/session" 两个路径都清一遍。
    """
    for path in ("/", REFRESH_PATH):
        response.delete_cookie(ACCESS_COOKIE, path=path)
        response.delete_cookie(REFRESH_COOKIE, path=path)
        response.delete_cookie(CSRF_COOKIE, path=path)


@contextmanager
def db_errors():
    """把 SupabaseError 翻译成合适的 HTTP 响应，避免裸 500。"""
    try:
        yield
    except db.SupabaseError as e:
        raise HTTPException(status_code=e.status or 502, detail=e.message) from e
