# 端到端冒烟：流式对话 /chat/stream（SSE）+ 非流式 /chat 仍然可用。
#
# 用法（在本项目根目录）：
#     .venv\Scripts\python.exe tools/smoke_stream.py
#
# 刻意用临时 DB_PATH + 临时端口，不碰 data/app.db；
# 并且把 runner.chat_stream 换成一个脚本化的假实现 ——
# 这样既能覆盖"思考过程 / 工具调用 / 最终回答"三种事件的组装与下发，
# 又不会真的去调 DeepSeek、不花额度。
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP_DIR = tempfile.mkdtemp(prefix="kh_stream_")
os.environ["DB_PATH"] = os.path.join(TMP_DIR, "stream.db")
os.environ["PORT"] = "8200"
os.environ["DEBUG"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

import uvicorn  # noqa: E402

from agent import runner  # noqa: E402
from server.app import app  # noqa: E402

urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

BASE = "http://127.0.0.1:8200"
PHONE_ADMIN = "13800000001"
PASSWORD = "demo1234"
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


# ---------------------------------------------------------------------------
# 假的流式 agent：模拟「思考 -> 调工具 -> 回答」的真实事件顺序
# ---------------------------------------------------------------------------
FAKE_EVENTS = [
    {"type": "token", "text": "我先看"},
    {"type": "token", "text": "一下天气。"},
    {"type": "segment"},                       # 上面这段是"思考"，不是回答
    {"type": "step", "tool": "get_weather_forecast", "args": {"date": "2026-10-24"}},
    {"type": "token", "text": "明天"},
    {"type": "segment"},
    {"type": "tasks", "tasks": [{"id": "task-0001", "title": "打球", "start": "2026-10-24 15:00"}]},
    {"type": "token", "text": "有雨，建议改期。"},
    {"type": "done", "output": "有雨，建议改期。",
     "steps": [{"tool": "get_weather_forecast", "args": {"date": "2026-10-24"}, "result": "中雨"}]},
]

seen_calls = {"chat_stream": 0, "chat": 0, "distill": 0}

# 真身留一份：第 [8] 段要用桩 executor 测它自己的事件组装逻辑
REAL_CHAT_STREAM = runner.chat_stream


async def fake_chat_stream(user_input, **kwargs):
    seen_calls["chat_stream"] += 1
    seen_calls["last_input"] = user_input
    assert kwargs.get("deepseek_key") == FAKE_KEY, "流式没拿到统一 Key"
    for ev in FAKE_EVENTS:
        yield ev


def fake_chat(user_input, **kwargs):
    seen_calls["chat"] += 1
    return {"output": "非流式回答", "steps": [], "tasks": []}


def fake_distill(user_msg, assistant_msg, memory, key):
    seen_calls["distill"] += 1
    seen_calls["distilled"] = assistant_msg
    return dict(memory or {})


runner.chat_stream = fake_chat_stream
runner.chat = fake_chat
runner.distill_memory = fake_distill


# ---------------------------------------------------------------------------
# 最小 HTTP 客户端
# ---------------------------------------------------------------------------
class Client:
    def __init__(self):
        self.jar = {}
        self.opener = urllib.request.build_opener()
        self.opener.trust_env = False

    def _store(self, resp):
        for header in resp.getheaders():
            if header[0].lower() != "set-cookie":
                continue
            kv = header[1].split(";")[0]
            if "=" in kv:
                name, value = kv.split("=", 1)
                self.jar[name.strip()] = value.strip()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.jar:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        csrf = self.jar.get("kh_csrf")
        if csrf and method not in ("GET", "HEAD"):
            req.add_header("X-CSRF-Token", csrf)
        return req

    def request(self, method, path, body=None):
        try:
            resp = self.opener.open(self._req(method, path, body), timeout=15)
            self._store(resp)
            text = resp.read().decode("utf-8")
            return resp.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            self._store(e)
            text = e.read().decode("utf-8")
            try:
                return e.code, json.loads(text)
            except json.JSONDecodeError:
                return e.code, {"detail": text}

    def stream(self, method, path, body=None):
        """返回 (status, headers, 原始字节)。故意不解析，用来验证 SSE 帧格式。"""
        try:
            resp = self.opener.open(self._req(method, path, body), timeout=20)
            self._store(resp)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
        except urllib.error.HTTPError as e:
            self._store(e)
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def parse_sse(raw: bytes):
    """把原始 SSE 字节拆成事件列表。"""
    text = raw.decode("utf-8")
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        for line in frame.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


def main():
    config = uvicorn.Config(app, host="127.0.0.1", port=8200, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)

    admin = Client()

    print("\n[1] 准备：首人成为管理员 + 配置统一 Key")
    status, res = admin.request("POST", "/session/login", {"phone": PHONE_ADMIN})
    check("第一次只发手机号 -> need_setup", status == 200 and res.get("status") == "need_setup")
    status, res = admin.request("POST", "/session/login",
                                {"phone": PHONE_ADMIN, "new_password": PASSWORD})
    check("设置密码后成为管理员", status == 200 and res.get("is_admin") is True)
    status, res = admin.request("POST", "/admin/settings/api_key", {"api_key": FAKE_KEY})
    check("配置全站 DeepSeek Key", status == 200 and res.get("deepseek", {}).get("configured"))

    print("\n[2] 没配 Key 时：流式接口也要在流开始前返回 400")
    status, res = admin.request("DELETE", "/admin/settings/api_key")
    check("清空全局 Key", status == 200 and not res.get("deepseek", {}).get("configured"), res)
    status, body = admin.request("POST", "/chat/stream", {"message": "明天打球"})
    check("清空 Key 后 /chat/stream -> 400（不是半截流）",
          status == 400 and isinstance(body, dict) and "API Key" in body.get("detail", ""),
          body.get("detail") if isinstance(body, dict) else body)
    admin.request("POST", "/admin/settings/api_key", {"api_key": FAKE_KEY})

    print("\n[3] 流式对话：响应头与 SSE 帧")
    status, headers, raw = admin.stream("POST", "/chat/stream",
                                        {"message": "明天下午打球"})
    ctype = headers.get("content-type", "")
    check("HTTP 200", status == 200, status)
    check("Content-Type 是 text/event-stream", "text/event-stream" in ctype, ctype)
    check("带 X-Accel-Buffering: no（否则会被反向代理缓冲）",
          headers.get("x-accel-buffering") == "no", headers.get("x-accel-buffering"))
    check("分块传输（没有 Content-Length）",
          headers.get("transfer-encoding") == "chunked" and "content-length" not in headers,
          headers.get("transfer-encoding"))

    events = parse_sse(raw)
    types = [e.get("type") for e in events]
    check("事件顺序与假 agent 完全一致", types == [e["type"] for e in FAKE_EVENTS], types)
    check("每帧都是 data: 开头的合法 JSON", len(events) == len(FAKE_EVENTS), len(events))

    print("\n[4] 关键：思考过程与最终回答必须是分开的段")
    first_segment = types.index("segment")
    thinking = "".join(e["text"] for e in events[:first_segment] if e.get("type") == "token")
    answer = "".join(
        e["text"] for e in events[first_segment:] if e.get("type") == "token")
    check("思考段被单独圈出来", thinking == "我先看" + "一下天气。", thinking)
    check("回答段是最后一段", "有雨" in answer and "我先看" not in answer, answer)
    done = next(e for e in events if e["type"] == "done")
    check("done 带的 output 是最终回答（不含思考）",
          done["output"] == "有雨，建议改期。", done["output"])

    print("\n[5] 落库与后台蒸馏")
    check("收到用户消息", seen_calls.get("last_input") == "明天下午打球", seen_calls.get("last_input"))
    time.sleep(1.5)   # 蒸馏在响应结束后才跑
    check("记忆蒸馏在后台执行了", seen_calls["distill"] == 1, seen_calls["distill"])
    check("蒸馏用的是最终回答，不是思考内容",
          seen_calls.get("distilled") == "有雨，建议改期。", seen_calls.get("distilled"))

    print("\n[6] 非流式 /chat 仍然照旧")
    status, res = admin.request("POST", "/chat", {"message": "问个问题"})
    check("/chat 返回完整 JSON",
          status == 200 and res.get("output") == "非流式回答", res)
    check("非流式没有走 chat_stream", seen_calls["chat"] == 1 and seen_calls["chat_stream"] == 1,
          f'chat={seen_calls["chat"]} stream={seen_calls["chat_stream"]}')

    print("\n[7] 未登录不能流式对话")
    anon = Client()
    status, headers, body = anon.stream("POST", "/chat/stream", {"message": "hi"})
    check("没会话 -> 401", status == 401, status)

    print("\n[8] chat_stream 自身的事件组装（桩 executor，不联网）")

    class StubExecutor:
        """按 run_id 产出 on_chat_model_stream，中间夹一次工具调用。"""

        async def astream_events(self, inputs, version="v2"):
            assert version == "v2"
            yield {"event": "on_chat_model_stream", "run_id": "r1",
                   "data": {"chunk": type("C", (), {"content": "先查天气"})()}}
            yield {"event": "on_tool_start", "name": "get_weather_forecast",
                   "data": {"input": {"date": "2026-10-24"}}}
            yield {"event": "on_tool_end", "name": "get_weather_forecast",
                   "data": {"output": "中雨"}}
            yield {"event": "on_chat_model_stream", "run_id": "r2",
                   "data": {"chunk": type("C", (), {"content": "有雨"})()}}
            yield {"event": "on_chat_model_stream", "run_id": "r2",
                   "data": {"chunk": type("C", (), {"content": "，改期吧"})()}}
            yield {"event": "on_tool_start", "name": "emit_tasks",
                   "data": {"input": {"tasks": [{"title": "打球", "start": "2026-10-24 15:00"}]}}}
            yield {"event": "on_tool_end", "name": "emit_tasks", "data": {"output": "已捕获 1 个任务"}}
            yield {"event": "on_chain_end", "name": "AgentExecutor",
                   "data": {"output": {"output": "有雨，改期吧",
                                       "intermediate_steps": []}}}

    runner._build_executor = lambda key, phone="", location=None: StubExecutor()

    async def collect():
        return [ev async for ev in REAL_CHAT_STREAM(
            "明天打球", deepseek_key=FAKE_KEY, phone="13800000001")]

    got = asyncio.run(collect())
    types = [e["type"] for e in got]
    check("第 2 个模型轮次处插了 segment", types.count("segment") == 1, types)
    check("两次 token 都转发了", types.count("token") == 3, types)
    check("emit_tasks 不进推理过程，转成 tasks 事件",
          types.count("step") == 1 and types.count("tasks") == 1, types)
    tasks_evt = next(e for e in got if e["type"] == "tasks")
    check("任务带了 id", bool(tasks_evt["tasks"][0].get("id")), tasks_evt["tasks"][0])
    check("工具结果回填进 steps",
          got[-1]["steps"][0]["result"] == "中雨", got[-1]["steps"])
    check("done 的 output 取权威结果", got[-1]["output"] == "有雨，改期吧", got[-1]["output"])

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    print(f"临时数据库留在 {TMP_DIR}（可以删掉）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
