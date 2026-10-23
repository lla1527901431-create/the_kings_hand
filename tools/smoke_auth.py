# 端到端冒烟：在临时数据库上跑一遍「管理员密码登录 + 统一 API Key」全流程。
# 用法（在本项目根目录）：
#     .venv\Scripts\python.exe tools/smoke_auth.py
#
# 刻意用临时 DB_PATH + 临时端口，不碰 data/app.db。
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# 让 `from server import ...` 能找到项目根（脚本在 tools/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DIR = tempfile.mkdtemp(prefix="kh_smoke_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "smoke.db")
os.environ["PORT"] = "8199"
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

import uvicorn  # noqa: E402  （必须在设置完环境变量之后再 import）
from server.app import app  # noqa: E402

# 本机回环别走系统代理（公司代理会对 127.0.0.1 返回 502）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

HOST = "127.0.0.1:8199"
BASE = f"http://{HOST}"

PHONE_ADMIN = "13800000001"
PHONE_MEMBER = "13800000002"
PASSWORD = "demo1234"
PASSWORD_NEW = "newpass5678"
FAKE_KEY = "sk-smoke-test-abcdef123456"

passed = 0
failed = 0


def check(label, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}{(' -> ' + str(extra)) if extra else ''}")
    else:
        failed += 1
        print(f"  FAIL  {label}{(' -> ' + str(extra)) if extra else ''}")


class Client:
    """带 cookie jar 的最小 HTTP 客户端（不读系统代理）。"""

    def __init__(self):
        self.jar = {}
        self.opener = urllib.request.build_opener()
        self.opener.trust_env = False        # 别走公司代理，本机回环

    def _store_cookies(self, resp):
        for header in resp.getheaders():
            if header[0].lower() != "set-cookie":
                continue
            kv = header[1].split(";")[0]
            if "=" in kv:
                name, value = kv.split("=", 1)
                self.jar[name.strip()] = value.strip()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.jar:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        csrf = self.jar.get("kh_csrf")
        if csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", csrf)
        try:
            resp = self.opener.open(req, timeout=15)
            self._store_cookies(resp)
            text = resp.read().decode("utf-8")
            return resp.status, (json.loads(text) if text else None)
        except urllib.error.HTTPError as e:
            self._store_cookies(e)
            text = e.read().decode("utf-8")
            try:
                return e.code, json.loads(text)
            except json.JSONDecodeError:
                return e.code, {"detail": text}


def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=8199, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)

    admin = Client()
    member = Client()

    print("\n[1] 首人引导：名单为空 -> 要求设置密码")
    status, info = admin.request("GET", "/startup")
    check("/startup needs_bootstrap", status == 200 and info.get("needs_bootstrap"), info)

    status, res = admin.request("POST", "/session/login", {"phone": PHONE_ADMIN})
    check("第一次只发手机号 -> need_setup",
          status == 200 and res.get("status") == "need_setup", res.get("reason"))

    status, res = admin.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "new_password": PASSWORD})
    check("设置密码后登录成功且成为管理员",
          status == 200 and res.get("status") == "ok" and res.get("is_admin") is True, res)

    status, res = admin.request("GET", "/session")
    check("会话有效", status == 200 and res.get("is_admin") is True, res)

    print("\n[2] 管理员配置了全站 DeepSeek Key")
    status, res = admin.request("POST", "/admin/settings/api_key", {"api_key": FAKE_KEY})
    check("保存全局 Key", status == 200 and res.get("deepseek", {}).get("configured"), res)

    status, res = admin.request("GET", "/admin/settings")
    masked = res.get("settings", {}).get("deepseek", {}).get("masked", "")
    check("返回打码后的 Key（不含明文）",
          status == 200 and masked and FAKE_KEY not in json.dumps(res, ensure_ascii=False), masked)

    status, res = admin.request("GET", "/me")
    check("/me 报告 Key 来源为 shared", res.get("deepseek_source") == "shared",
          res.get("deepseek_source"))

    print("\n[3] 普通成员：不需要密码，共用同一把 Key")
    status, res = admin.request("POST", "/admin/whitelist/add",
                                {"phone": PHONE_MEMBER, "name": "面试小助手", "is_admin": False})
    check("管理员添加成员", status == 200, res.get("status"))

    status, res = member.request("POST", "/session/login", {"phone": PHONE_MEMBER})
    check("成员免密直接登录", status == 200 and res.get("status") == "ok", res)

    status, res = member.request("GET", "/me")
    check("成员也用 shared Key", res.get("deepseek_source") == "shared" and not res.get("is_admin"),
          res.get("deepseek_source"))

    status, res = member.request("GET", "/admin/settings")
    check("成员看不到管理接口", status == 403, res.get("detail"))

    print("\n[4] 管理员第二次登录必须输密码")
    fresh = Client()
    status, res = fresh.request("POST", "/session/login", {"phone": PHONE_ADMIN})
    check("只发手机号 -> need_password", res.get("status") == "need_password", res)

    status, res = fresh.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "password": "wrong-pass"})
    check("错误密码 -> 401 bad_password", status == 401 and res.get("auth_code") == "bad_password", res)

    status, res = fresh.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "password": PASSWORD})
    check("正确密码 -> 登录成功", status == 200 and res.get("status") == "ok", res)

    print("\n[5] 修改密码 / 取消管理员后清密码")
    status, res = fresh.request("POST", "/admin/password",
                                {"old_password": "bad-old", "new_password": PASSWORD_NEW})
    check("旧密码不对 -> 拒绝", status == 401, res.get("detail"))

    status, res = fresh.request("POST", "/admin/password",
                                {"old_password": PASSWORD, "new_password": PASSWORD_NEW})
    check("改密成功", status == 200, res)

    again = Client()
    status, res = again.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "password": PASSWORD})
    check("旧密码已失效", status == 401, res.get("detail"))
    status, res = again.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "password": PASSWORD_NEW})
    check("新密码可用", status == 200 and res.get("status") == "ok", res)

    status, res = again.request("POST", "/admin/whitelist/set_admin",
                                {"phone": PHONE_ADMIN, "is_admin": False})
    check("只剩一个管理员时不许降级自己", status == 400, res.get("detail"))

    print("\n[6] 清空全局 Key 之后，没人再能用 AI")
    status, res = again.request("DELETE", "/admin/settings/api_key")
    check("清空 Key", status == 200 and not res.get("deepseek", {}).get("configured"), res)

    status, res = member.request("GET", "/me")
    check("成员 /me 的 source 变空", res.get("deepseek_source") == "", res.get("deepseek_source"))

    status, res = member.request("POST", "/chat", {"message": "明天下午三点开会"})
    check("没 Key 时 /chat 报错并提示联系管理员",
          status == 400 and "联系管理员" in (res.get("detail") or ""), res.get("detail"))

    print("\n[7] favicon")
    for url, expect in (("/static/favicon.ico", b"PNG"), ("/static/favicon.svg", b"<svg")):
        try:
            req = urllib.request.Request(BASE + url)
            req.add_header("User-Agent", "smoke")
            body = urllib.request.urlopen(req, timeout=10).read()
            check(f"{url} 可访问", expect in body, f"{len(body)} 字节")
        except Exception as e:
            check(f"{url} 可访问", False, e)

    server.should_exit = True
    print(f"\n结果：{passed} 通过 / {failed} 失败")
    print(f"临时数据库留在 {TMP_DIR}（可以删掉）")


if __name__ == "__main__":
    main()
