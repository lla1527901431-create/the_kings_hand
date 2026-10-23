"""鉴权：手机号白名单 + cookie 会话。

与云端版的区别：
    云端：Supabase Auth 签发 JWT，后端只做转手
    这里：后端自己签发随机 token 存 SQLite，cookie 里只放 token

依赖注入用法：
    def endpoint(user: CurrentUser = Depends(current_user)): ...
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, Response

from db import sessions, whitelist
from server.logging_config import set_user_id

SESSION_COOKIE = "kh_session"
CSRF_COOKIE = "kh_csrf"
CSRF_HEADER = "x-csrf-token"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CurrentUser:
    __slots__ = ("phone", "is_admin")

    def __init__(self, phone: str, is_admin: bool):
        self.phone = phone
        self.is_admin = is_admin

    def __repr__(self) -> str:
        return f"CurrentUser(phone={self.phone[:3]}****{self.phone[-4:]}, admin={self.is_admin})"


def _token_from(request: Request, authorization: str | None) -> str:
    """优先 cookie（浏览器），其次 Authorization 头（脚本调试用）。"""
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if token:
        return token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    """校验会话，返回当前用户。"""
    token = _token_from(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="未登录。", headers={"X-Auth-Code": "no_session"})

    phone = sessions.resolve(token)
    if not phone:
        raise HTTPException(
            status_code=401, detail="登录已失效，请重新输入手机号。",
            headers={"X-Auth-Code": "no_session"},
        )

    # 会话有效但人已被移出白名单 -> 立即失效
    if not whitelist.is_allowed(phone):
        sessions.revoke_all(phone)
        raise HTTPException(
            status_code=403, detail="您的访问权限已被移除。",
            headers={"X-Auth-Code": "revoked"},
        )

    set_user_id(phone)
    return CurrentUser(phone=phone, is_admin=whitelist.is_admin(phone))


async def admin_user(request: Request, authorization: str | None = Header(default=None)) -> CurrentUser:
    """管理员依赖：先校验会话，再校验管理员身份。"""
    user = await current_user(request, authorization)
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="只有管理员可以管理访问名单。",
            headers={"X-Auth-Code": "not_admin"},
        )
    return user


# ---------------------------------------------------------------------------
# CSRF：cookie 方案必须付的代价
# ---------------------------------------------------------------------------
async def csrf_protect(request: Request, x_csrf_token: str | None = Header(default=None)) -> None:
    """写操作校验双提交 cookie。

    登录时下发一个**非 HttpOnly** 的随机值 kh_csrf，前端把它放进
    X-CSRF-Token 头。别的站点读不到这个 cookie（同源策略），伪造不出这个头。
    """
    if request.method.upper() in SAFE_METHODS:
        return
    cookie_value = (request.cookies.get(CSRF_COOKIE) or "").strip()
    header_value = (x_csrf_token or "").strip()
    if not cookie_value:
        raise HTTPException(status_code=401, detail="未登录。", headers={"X-Auth-Code": "no_session"})
    if not header_value or cookie_value != header_value:
        raise HTTPException(
            status_code=403, detail="请求校验失败，请刷新页面重试。",
            headers={"X-Auth-Code": "csrf_failed"},
        )


# ---------------------------------------------------------------------------
# cookie 读写
# ---------------------------------------------------------------------------
def set_session_cookies(response: Response, token: str, csrf: str, max_age: int,
                        request: Request | None = None) -> None:
    """下发会话 cookie。

    Secure 标记按**浏览器实际看到的协议**来定，而不是写死的开关：
        * 本机 http://127.0.0.1  -> Secure=false（否则浏览器拒绝保存）
        * 公网 https://域名      -> Secure=true（否则 cookie 有被嗅探的风险）
    托管部署时前面有反向代理，uvicorn 自己看到的是 http，所以要读
    X-Forwarded-Proto 才能知道浏览器的真实协议。
    """
    import config

    secure = config.COOKIE_SECURE
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto")
                 or request.headers.get("x-forwarded-protocol")
                 or request.url.scheme or "").split(",")[0].strip().lower()
        secure = proto == "https"
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age,
        httponly=True, samesite="lax", secure=secure, path="/",
    )
    # 这一条必须能被 JS 读到，否则前端拿不到它放进请求头
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age,
        httponly=False, samesite="lax", secure=secure, path="/",
    )


def clear_session_cookies(response: Response) -> None:
    """清 cookie（路径要覆盖所有可能写过的，否则浏览器会残留一份）。"""
    for path in ("/",):
        response.delete_cookie(SESSION_COOKIE, path=path)
        response.delete_cookie(CSRF_COOKIE, path=path)
