"""FastAPI 路由层。

职责划分：这里只做"接口"，业务逻辑在 db/ 与 agent/ 里。

鉴权模式（与原项目不同）：
    手机号白名单登录 -> 服务端签发随机 token -> cookie 里只放 token
    第一个成功登录的手机号自动成为管理员。

数据全部存在本地 SQLite，不产生任何跨境请求。
"""

from __future__ import annotations

# ⚠️ 注意别名的必要性：本项目有自己的 "secrets" 模块（db/secrets.py，
# 存用户 API Key），它会遮蔽 Python 标准库的 secrets。所以标准库必须换名导入，
# 否则 secrets.token_hex(...) 会跑到数据库模块里去（已踩过这个坑）。
import json
import secrets as py_secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from agent import memory_history, runner
from agent.ics_exporter import build_ics
from agent.llm import CredentialsMissing
from db import calendars, database, histories, memories, pendings, secrets, sessions, settings, whitelist
from server.auth import (
    CurrentUser,
    admin_user,
    clear_session_cookies,
    csrf_protect,
    current_user,
    set_session_cookies,
)
from server.logging_config import get_logger, set_request_id, set_user_id, setup_logging

BASE_DIR = Path(__file__).parent.parent
STATIC_DIR = BASE_DIR / "static"

setup_logging(config.LOG_LEVEL)
logger = get_logger("app")

app = FastAPI(title="The King's Hand (local demo)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 登录失败节流
# ---------------------------------------------------------------------------
# 加了密码就得防暴力试。这是本机 demo，不需要 redis 之类的，
# 内存里记一个滑动窗口足够：同一个号 N 分钟内错够 5 次就先拒 5 分钟。
# （进程重启会清空，对 demo 无所谓 —— 真要公网部署应该落到数据库里。）
_LOGIN_FAIL_WINDOW = 300          # 统计窗口：5 分钟
_LOGIN_FAIL_LIMIT = 5             # 窗口内允许的错误次数
_login_fails: dict[str, list[float]] = {}
_login_fails_lock = threading.Lock()


def _is_locked_out(phone: str) -> bool:
    now = time.time()
    with _login_fails_lock:
        recent = [t for t in _login_fails.get(phone, []) if now - t < _LOGIN_FAIL_WINDOW]
        _login_fails[phone] = recent
        return len(recent) >= _LOGIN_FAIL_LIMIT


def _note_login_fail(phone: str) -> None:
    with _login_fails_lock:
        _login_fails.setdefault(phone, []).append(time.time())


def _clear_login_fails(phone: str) -> None:
    with _login_fails_lock:
        _login_fails.pop(phone, None)


# ---------------------------------------------------------------------------
# 请求日志与统一错误输出
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_context(request: Request, call_next):
    set_request_id(py_secrets.token_hex(3))
    set_user_id("-")
    quiet = request.url.path.startswith("/static") or request.url.path == "/health"
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.error("请求异常终止 %s %s", request.method, request.url.path)
        raise
    elapsed = (time.perf_counter() - started) * 1000
    if not quiet:
        level = logger.warning if response.status_code >= 400 else logger.info
        level("%s %s -> %s  %.0fms", request.method, request.url.path,
              response.status_code, elapsed)
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    code = (exc.headers or {}).get("X-Auth-Code", "") if exc.headers else ""
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail, "auth_code": code})


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """登录请求。

    登录是**两段式**的，后端会先回答"下一步要什么"：

        第 1 步  只发 phone
                -> need_setup     名单还没人（或这位管理员还没设密码），请设密码
                -> need_password  这是管理员，请输入密码
                -> ok            普通成员，直接进

        第 2 步  补 password（管理员）或 new_password（设置密码）

    这样设计是因为：名单为空时我们根本不知道"下一个来的人"要不要密码，
    只能等他报上手机号之后再决定给他看哪个表单。
    """
    phone: str
    password: str = ""
    new_password: str = ""


class ChatRequest(BaseModel):
    message: str
    events: list[dict] = []          # 前端把当前日程一起发来，供模型判断冲突
    pending: dict = {}
    history: list[dict] = []


class CredentialsRequest(BaseModel):
    deepseek_key: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""


class AddEventRequest(BaseModel):
    title: str
    start: str
    end: str | None = None
    location: str = ""
    note: str = ""


class UpdateEventRequest(BaseModel):
    id: str
    title: str
    start: str
    end: str | None = None
    location: str = ""
    note: str = ""


class IdRequest(BaseModel):
    id: str


class AddPendingRequest(BaseModel):
    id: str
    title: str
    start: str | None = None
    end: str | None = None
    location: str = ""
    note: str = ""


class UpdatePendingRequest(BaseModel):
    id: str
    title: str
    start: str | None = None
    end: str | None = None
    location: str = ""
    note: str = ""


class MemoryUpdateRequest(BaseModel):
    facts: list[str] | None = None
    observations: list[str] | None = None
    preferences: list[str] | None = None


class AddPhoneRequest(BaseModel):
    phone: str
    name: str = ""
    note: str = ""
    is_admin: bool = False


class SetAdminRequest(BaseModel):
    phone: str
    is_admin: bool


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    payload = {"status": "ok"}
    if config.DEBUG:
        payload["db"] = config.DB_PATH
        payload["tables"] = database.stats()
    return payload


# ---------------------------------------------------------------------------
# 登录 / 登出
# ---------------------------------------------------------------------------
@app.post("/session/login")
def login(req: LoginRequest, request: Request, response: Response,
          background: BackgroundTasks):
    """手机号登录（管理员需要密码）。

    三种进入方式：
      1. **白名单为空时**：第一个人进来 -> **必须当场设置密码** -> 成为管理员
      2. **管理员**：校验密码。历史遗留的、还没设过密码的管理员，
         第一次登录会被引导去设置
      3. **普通成员**：名单内即可进入，不需要密码（沿用旧行为）

    ⚠️ 第 1 条意味着：部署到公网且名单为空时，**谁先访问谁就是管理员**。
        所以对外部署前请先用 tools/add_phone.py 把自己配进名单。
    """
    try:
        phone = whitelist.normalize(req.phone)
    except whitelist.PhoneInvalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    database.init_db()
    database.cleanup_expired_sessions()

    password = (req.password or "").strip()
    new_password = (req.new_password or "").strip()

    if _is_locked_out(phone):
        raise HTTPException(status_code=429,
                            detail="密码错误次数过多，请 5 分钟后再试。")

    bootstrapped = False

    # ---------- 分支 1：名单为空，首人引导 ----------
    if whitelist.count() == 0:
        if not new_password:
            return {
                "status": "need_setup",
                "reason": "bootstrap",
                "phone": phone,
                "message": "名单还是空的，你将成为管理员，请设置一个密码。",
                "min_length": whitelist.MIN_PASSWORD_LENGTH,
            }
        if len(new_password) < whitelist.MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"密码至少需要 {whitelist.MIN_PASSWORD_LENGTH} 位。")
        whitelist.add(phone, name="", note="首个登录者，自动成为管理员", is_admin=True)
        whitelist.set_password(phone, new_password)
        bootstrapped = True
        logger.info("白名单为空 -> 首个登录者设为管理员并设置密码：%s", phone)

    # ---------- 分支 2 / 3：名单已有人 ----------
    elif not whitelist.is_allowed(phone):
        logger.warning("拒绝登录（不在白名单）：%s", phone)
        raise HTTPException(status_code=403, detail="抱歉，您没有权限访问。")

    elif whitelist.is_admin(phone):
        if not whitelist.has_password(phone):
            # 早期建好的管理员账号不会有密码，这里兜一下，让他补一个
            if not new_password:
                return {
                    "status": "need_setup",
                    "reason": "admin_without_password",
                    "phone": phone,
                    "message": "你是管理员，但还没有设置密码，请先设置一个。",
                    "min_length": whitelist.MIN_PASSWORD_LENGTH,
                }
            if len(new_password) < whitelist.MIN_PASSWORD_LENGTH:
                raise HTTPException(
                    status_code=400,
                    detail=f"密码至少需要 {whitelist.MIN_PASSWORD_LENGTH} 位。")
            whitelist.set_password(phone, new_password)
            logger.info("管理员 %s 补设了登录密码", phone)
        else:
            if not password:
                return {
                    "status": "need_password",
                    "phone": phone,
                    "message": "请输入管理员密码。",
                }
            if not whitelist.check_password(phone, password):
                _note_login_fail(phone)
                raise HTTPException(status_code=401, detail="密码不正确。",
                                    headers={"X-Auth-Code": "bad_password"})
            _clear_login_fails(phone)

    # 到这里才算认证通过，签发会话
    whitelist.touch_login(phone)
    token = sessions.issue(phone)
    csrf = py_secrets.token_urlsafe(24)
    max_age = config.SESSION_DAYS * 24 * 3600
    set_session_cookies(response, token, csrf, max_age, request)

    user = whitelist.get(phone) or {}
    logger.info("登录成功：%s（管理员=%s%s）", phone, user.get("is_admin"),
                "，首次引导" if bootstrapped else "")

    # 过期日程沉淀：登录后顺手做，放后台，不拖慢登录响应
    background.add_task(_distill_expired_events, phone)

    return {
        "status": "ok",
        "phone": phone,
        "is_admin": bool(user.get("is_admin")),
        "bootstrapped": bootstrapped,
    }


@app.get("/startup")
def startup_info():
    """给登录页用：告诉前端现在名单是不是空的（决定显示哪种提示）。"""
    database.init_db()
    return {
        "whitelist_count": whitelist.count(),
        "needs_bootstrap": whitelist.count() == 0,
    }


@app.post("/session/logout")
def logout(request: Request, response: Response, _: None = Depends(csrf_protect)):
    token = (request.cookies.get("kh_session") or "").strip()
    if token:
        sessions.revoke(token)
    clear_session_cookies(response)
    return {"status": "ok"}


@app.get("/session")
def session_info(request: Request):
    """给前端判断"现在登录着吗"。不抛 401，方便页面初始化。"""
    token = (request.cookies.get("kh_session") or "").strip()
    phone = sessions.resolve(token) if token else None
    if not phone or not whitelist.is_allowed(phone):
        return {"authenticated": False}
    return {
        "authenticated": True,
        "phone": phone,
        "is_admin": whitelist.is_admin(phone),
    }


@app.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    """前端初始化用：我是谁、能不能管名单、AI 服务配好了没。

    deepseek.source 说明这次对话会用到哪把 Key：
        "shared" 管理员统一配置的（优先）
        "own"    这用户自己以前填的（兜底）
    """
    key, source = secrets.resolve_deepseek_key(user.phone)
    return {
        "phone": user.phone,
        "is_admin": user.is_admin,
        "credentials": secrets.status(user.phone),
        "deepseek": settings.deepseek_status(),
        "deepseek_source": source if key else "",
        "memory": memories.load_memory(user.phone),
    }


# ---------------------------------------------------------------------------
# 用户 API Key
# ---------------------------------------------------------------------------
@app.get("/credentials")
def get_credentials(user: CurrentUser = Depends(current_user)):
    return {"credentials": secrets.status(user.phone)}


@app.post("/credentials")
def save_credentials(req: CredentialsRequest, user: CurrentUser = Depends(current_user),
                     _: None = Depends(csrf_protect)):
    if not (req.deepseek_key or req.smtp_user or req.smtp_pass):
        raise HTTPException(status_code=400, detail="没有填写任何内容。")
    return {"status": "ok", "credentials": secrets.save(
        user.phone, req.deepseek_key, req.smtp_user, req.smtp_pass)}


@app.delete("/credentials")
def delete_credentials(user: CurrentUser = Depends(current_user),
                       _: None = Depends(csrf_protect)):
    secrets.clear(user.phone)
    return {"status": "ok", "credentials": secrets.status(user.phone)}


# ---------------------------------------------------------------------------
# 白名单管理（仅管理员）
# ---------------------------------------------------------------------------
@app.get("/admin/whitelist")
def admin_list(admin: CurrentUser = Depends(admin_user)):
    return {"phones": whitelist.list_all(), "me": admin.phone}


@app.post("/admin/whitelist/add")
def admin_add(req: AddPhoneRequest, admin: CurrentUser = Depends(admin_user),
              _: None = Depends(csrf_protect)):
    try:
        phone = whitelist.normalize(req.phone)
    except whitelist.PhoneInvalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    item = whitelist.add(phone, req.name, req.note, req.is_admin)
    logger.info("管理员 %s 添加白名单：%s", admin.phone, phone)
    return {"status": "ok", "item": item}


@app.post("/admin/whitelist/remove")
def admin_remove(req: AddPhoneRequest, admin: CurrentUser = Depends(admin_user),
                 _: None = Depends(csrf_protect)):
    try:
        phone = whitelist.normalize(req.phone)
    except whitelist.PhoneInvalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if phone == admin.phone:
        raise HTTPException(status_code=400, detail="不能把自己移出名单。")
    # 不允许把最后一个管理员移走，否则没人能管理名单了
    if whitelist.is_admin(phone) and whitelist.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="至少需要保留一个管理员。")

    ok = whitelist.remove(phone)
    if ok:
        sessions.revoke_all(phone)
        logger.info("管理员 %s 移除白名单：%s", admin.phone, phone)
    return {"status": "ok" if ok else "not_found"}


@app.post("/admin/whitelist/set_admin")
def admin_set_admin(req: SetAdminRequest, admin: CurrentUser = Depends(admin_user),
                    _: None = Depends(csrf_protect)):
    try:
        phone = whitelist.normalize(req.phone)
    except whitelist.PhoneInvalid as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not req.is_admin and whitelist.is_admin(phone) and whitelist.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="至少需要保留一个管理员。")

    ok = whitelist.set_admin(phone, req.is_admin)
    # 降级成普通成员后就用不到密码了；下次再提成管理员时会重新要求设置
    if ok and not req.is_admin:
        whitelist.clear_password(phone)
    logger.info("管理员 %s 将 %s 的管理员权限设为 %s", admin.phone, phone, req.is_admin)
    return {"status": "ok" if ok else "not_found"}


# ---------------------------------------------------------------------------
# 全站 DeepSeek API Key（仅管理员）
# ---------------------------------------------------------------------------
# 演示版的定位：管理员配一次，所有人都用这一把。
# 普通成员因此不需要（也看不到）自己的 Key 入口；
# 个人以前填的 Key 仍然保留在 user_secrets 里，作为"管理员还没配"时的兜底。
class ApiKeyRequest(BaseModel):
    api_key: str = ""


class PasswordChangeRequest(BaseModel):
    old_password: str = ""
    new_password: str = ""


@app.get("/admin/settings")
def admin_settings(admin: CurrentUser = Depends(admin_user)):
    """管理员面板用：全站设置（目前只有 DeepSeek Key）的状态。"""
    return {"settings": {"deepseek": settings.deepseek_status()}}


@app.post("/admin/settings/api_key")
def admin_save_api_key(req: ApiKeyRequest, admin: CurrentUser = Depends(admin_user),
                       _: None = Depends(csrf_protect)):
    key = (req.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="请填写 API Key。")
    settings.set_value(settings.DEEPSEEK_API_KEY, key, updated_by=admin.phone)
    logger.info("管理员 %s 更新了全局 DeepSeek API Key", admin.phone)
    return {"status": "ok", "deepseek": settings.deepseek_status()}


@app.delete("/admin/settings/api_key")
def admin_clear_api_key(admin: CurrentUser = Depends(admin_user),
                        _: None = Depends(csrf_protect)):
    settings.delete(settings.DEEPSEEK_API_KEY)
    logger.info("管理员 %s 清空了全局 DeepSeek API Key", admin.phone)
    return {"status": "ok", "deepseek": settings.deepseek_status()}


@app.post("/admin/password")
def admin_change_password(req: PasswordChangeRequest,
                          admin: CurrentUser = Depends(admin_user),
                          _: None = Depends(csrf_protect)):
    """管理员修改自己的登录密码。必须验证旧密码，防止会话被借用后改密。"""
    old = (req.old_password or "").strip()
    new = (req.new_password or "").strip()
    if not whitelist.check_password(admin.phone, old):
        raise HTTPException(status_code=401, detail="当前密码不正确。")
    if len(new) < whitelist.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"新密码至少需要 {whitelist.MIN_PASSWORD_LENGTH} 位。")
    whitelist.set_password(admin.phone, new)
    logger.info("管理员 %s 修改了登录密码", admin.phone)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------
def _friendly_error(e: Exception, is_admin: bool) -> tuple[int, str]:
    """把 agent 层抛出的异常翻译成（HTTP 状态码，人话）。两个 /chat 共用。"""
    name, text = type(e).__name__, str(e)
    if "AuthenticationError" in name or "401" in text:
        return 401, "DeepSeek API Key 无效或已失效，" + (
            "请在「访问管理」里更新。" if is_admin else "请联系管理员更新。")
    if "RateLimit" in name or "429" in text or "insufficient" in text.lower():
        return 402, "DeepSeek 账户余额不足或请求过于频繁。"
    if "timeout" in name.lower() or "timeout" in text.lower():
        return 504, "调用 DeepSeek 超时，请重试。"
    return 502, f"对话处理失败（{name}）。"


def _prepare_chat(user: CurrentUser, req: ChatRequest) -> tuple[str, str, dict, list]:
    """/chat 与 /chat/stream 共用的前置检查与取数。"""
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空。")

    key, source = secrets.resolve_deepseek_key(user.phone)
    if not key:
        hint = ("请在右上角「访问管理」里配置全局 DeepSeek API Key。"
                if user.is_admin else "请联系管理员在「访问管理」里配置 DeepSeek API Key。")
        raise HTTPException(status_code=400, detail="还没有配置 DeepSeek API Key。" + hint)

    logger.info("本次对话使用的 Key 来源：%s", source)
    return (message, key,
            memories.load_memory(user.phone),
            histories.recent(user.phone) or req.history)


def _distill_expired_events(phone: str) -> None:
    """把「本周一之前」的日程沉淀进 memories.history，然后删掉原行。

    三道保险：
      * 每个用户每天只跑一次（记在 settings，键里带手机号，避免一个人跑完
        其他人今天都不跑了）；
      * 没配 Key 就跳过 —— 蒸馏要调 LLM；
      * 任何异常只记日志：沉淀失败绝不能影响登录本身，也不能删数据
        （事务在 memory_history 内部，抛异常时不会提交）。
    """
    try:
        today = database.now_date()
        mark = f"memory_distill:last_run:{phone}"
        if settings.get(mark) == today:
            return

        key, _source = secrets.resolve_deepseek_key(phone)
        if not key:
            logger.info("跳过过期日程沉淀：%s 还没有可用的 DeepSeek Key", phone)
            return

        result = memory_history.distill(phone, key)
        settings.set_value(mark, today, updated_by="system")
        logger.info("过期日程沉淀：%s %s", phone, result)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("过期日程沉淀失败（不影响登录）：%s: %s",
                       type(e).__name__, str(e)[:200])


def _distill_in_background(phone: str, user_msg: str, assistant_msg: str,
                           memory: dict, key: str) -> None:
    """记忆蒸馏。它自己还要再调一次 LLM，所以放后台跑，不占着流式连接。"""
    try:
        updated = runner.distill_memory(user_msg, assistant_msg, memory, key)
        if updated:
            memories.save_memory(phone, updated)
    except Exception as e:
        logger.warning("记忆蒸馏失败：%s: %s", type(e).__name__, str(e)[:160])


@app.post("/chat")
def chat_endpoint(req: ChatRequest, user: CurrentUser = Depends(current_user),
                  _: None = Depends(csrf_protect)):
    """非流式对话：一次算完，返回一个完整 JSON。

    保留它有两个用处：
      1. 流式出问题时有个行为一致的对照组，方便排查
      2. 冒烟脚本 / 脚本化调用不需要处理 SSE
    """
    message, key, memory, history = _prepare_chat(user, req)

    try:
        result = runner.chat(
            message,
            deepseek_key=key,
            events=req.events,
            pending=req.pending,
            memory_text=memories.format_memory_text(memory),
            history=history,
        )
    except CredentialsMissing as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        name, text = type(e).__name__, str(e)
        logger.warning("对话失败：%s: %s", name, text[:200])
        code, detail = _friendly_error(e, user.is_admin)
        raise HTTPException(status_code=code, detail=detail) from e

    # 落库：对话历史 + 记忆蒸馏结果
    histories.append(user.phone, message, result.get("output", ""))

    updated = runner.distill_memory(message, result.get("output", ""), memory, key)
    if updated:
        memories.save_memory(user.phone, updated)

    return result


# ---------------------------------------------------------------------------
# 流式对话（SSE）
# ---------------------------------------------------------------------------
def _sse(event: dict) -> bytes:
    """把一个事件序列化成 SSE 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


@app.post("/chat/stream")
async def chat_stream_endpoint(req: ChatRequest, user: CurrentUser = Depends(current_user),
                               _: None = Depends(csrf_protect),
                               background: BackgroundTasks = None):
    """流式对话：SSE 逐 token 下发。

    ⚠️ 与 /chat 的关键区别：**响应一旦开始，状态码就改不了了**。
      所以前置校验（消息为空、没配 Key）仍然走 HTTPException —— 它们发生在
      流开始之前；而调用模型过程中出错，只能作为 error 事件发下去。
    """
    message, key, memory, history = _prepare_chat(user, req)

    async def generate():
        output = ""
        try:
            async for event in runner.chat_stream(
                message,
                deepseek_key=key,
                events=req.events,
                pending=req.pending,
                memory_text=memories.format_memory_text(memory),
                history=history,
            ):
                if event.get("type") == "done":
                    output = event.get("output", "")
                yield _sse(event)
        except CredentialsMissing as e:
            yield _sse({"type": "error", "message": str(e)})
            return
        except Exception as e:
            name, text = type(e).__name__, str(e)
            logger.warning("流式对话失败：%s: %s", name, text[:200])
            _, detail = _friendly_error(e, user.is_admin)
            yield _sse({"type": "error", "message": detail})
            return

        histories.append(user.phone, message, output)
        if output:
            # 蒸馏放到响应结束之后，别让用户在"记忆更新中"上多等几秒
            if background is not None:
                background.add_task(
                    _distill_in_background, user.phone, message, output, memory, key)
            else:                      # 理论上不会发生，兜一下免得整段崩掉
                _distill_in_background(user.phone, message, output, memory, key)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 线上有反向代理，不加这条 nginx 会把流缓冲成一次性返回
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/reset")
def reset_chat(user: CurrentUser = Depends(current_user), _: None = Depends(csrf_protect)):
    """只清对话上下文，不动日历与记忆。"""
    histories.clear(user.phone)
    return {"status": "ok", "scope": "chat_history_only"}


# ---------------------------------------------------------------------------
# 日程
# ---------------------------------------------------------------------------
@app.get("/events")
def list_events(user: CurrentUser = Depends(current_user)):
    return {"events": calendars.list_events(user.phone)}


@app.post("/event/add")
def add_event(req: AddEventRequest, user: CurrentUser = Depends(current_user),
              _: None = Depends(csrf_protect)):
    return {"status": "ok", "event": calendars.add_event(
        user.phone, req.title, req.start, req.end, req.location, req.note)}


@app.post("/event/update")
def update_event(req: UpdateEventRequest, user: CurrentUser = Depends(current_user),
                 _: None = Depends(csrf_protect)):
    fields = {"title": req.title, "start": req.start, "end": req.end,
              "location": req.location, "note": req.note}
    result = calendars.update_event(user.phone, req.id, fields)
    return {"status": "ok", "event": result} if result else {"status": "not_found"}


@app.post("/event/delete")
def delete_event(req: IdRequest, user: CurrentUser = Depends(current_user),
                 _: None = Depends(csrf_protect)):
    ok = calendars.delete_event(user.phone, req.id)
    return {"status": "ok" if ok else "not_found"}


# ---------------------------------------------------------------------------
# 待确认 / 待计划
# ---------------------------------------------------------------------------
@app.get("/pending")
def get_pending(user: CurrentUser = Depends(current_user)):
    return pendings.load_pending(user.phone)


@app.post("/pending/add")
def add_pending(req: AddPendingRequest, user: CurrentUser = Depends(current_user),
                _: None = Depends(csrf_protect)):
    pendings.add_pending(user.phone, req.model_dump())
    return {"status": "ok"}


@app.post("/pending/update")
def update_pending(req: UpdatePendingRequest, user: CurrentUser = Depends(current_user),
                   _: None = Depends(csrf_protect)):
    fields = {"title": req.title, "start": req.start, "end": req.end,
              "location": req.location, "note": req.note}
    result = pendings.update_pending(user.phone, req.id, fields)
    return {"status": "ok", "task": result} if result else {"status": "not_found"}


@app.post("/pending/delete")
def delete_pending(req: IdRequest, user: CurrentUser = Depends(current_user),
                   _: None = Depends(csrf_protect)):
    ok = pendings.delete_pending(user.phone, req.id)
    return {"status": "ok" if ok else "not_found"}


@app.post("/pending/confirm")
def confirm_pending(req: IdRequest, user: CurrentUser = Depends(current_user),
                    _: None = Depends(csrf_protect)):
    """确认一条待办：取出来 -> 补默认时长 -> 写进日历。"""
    task = pendings.pop_pending(user.phone, req.id)
    if task is None:
        return {"status": "not_found"}

    start_text = task.get("start")
    if start_text and not task.get("end"):
        try:
            start_dt = datetime.strptime(start_text, "%Y-%m-%d %H:%M")
            task["end"] = (start_dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

    event = calendars.add_event(
        user.phone, task["title"], task["start"], task.get("end"),
        task.get("location", ""), task.get("note", ""),
    )
    return {"status": "ok", "event": event}


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------
@app.get("/memory")
def get_memory(user: CurrentUser = Depends(current_user)):
    return {"memory": memories.load_memory(user.phone)}


@app.post("/memory/update")
def update_memory(req: MemoryUpdateRequest, user: CurrentUser = Depends(current_user),
                  _: None = Depends(csrf_protect)):
    memory = memories.load_memory(user.phone)
    if req.facts is not None:
        memory["facts"] = [f for f in req.facts if str(f).strip()]
    if req.observations is not None:
        memory["observations"] = [o for o in req.observations if str(o).strip()]
    if req.preferences is not None:
        memory["preferences"] = [p for p in req.preferences if str(p).strip()]
    memories.save_memory(user.phone, memory)
    return {"status": "ok", "memory": memories.load_memory(user.phone)}


@app.post("/memory/clear")
def clear_memory(user: CurrentUser = Depends(current_user), _: None = Depends(csrf_protect)):
    memories.clear_memory(user.phone)
    return {"status": "ok", "memory": memories.load_memory(user.phone)}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
@app.get("/export_ics")
def export_ics(user: CurrentUser = Depends(current_user)):
    events = calendars.list_events(user.phone)
    return Response(
        content=build_ics(events),
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="the_kings_hand.ics"'},
    )
