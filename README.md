# The King's Hand（本地演示版）

一个用自然语言安排日程的 AI 助手。**数据全部存在本机 SQLite**，不依赖任何云服务。

> 这是 `the_kings_hand` 的本地演示版。与原项目的区别：
> 数据从 Supabase（云端 Postgres）改为本地 SQLite，登录从邮箱+密码改为**手机号白名单**。
> 原项目一个字节都没动。

---

## 快速开始

```bash
# 1. 准备环境（首次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动
.venv\Scripts\python.exe main.py
```

浏览器打开 **http://127.0.0.1:8000**

### 第一次怎么进去

名单是空的，所以**第一个输入手机号的人会自动成为管理员** ——
比起原项目多了一步：**当场设置一个登录密码**（至少 6 位）。

```
打开 http://127.0.0.1:8000
  -> 页面提示「还没有任何访问名单…并需要设置一个密码」
  -> 输入手机号 -> 页面展开「设置密码 / 确认密码」-> 提交
  -> 成为管理员并进入
  -> 之后可以在右上角「访问管理」里加别人、配 AI Key、改自己的密码
```

之后名单非空了，就只有名单内的号码能登录；
**管理员每次登录都要输密码，普通成员仍只需要手机号。**

> 用命令行预置号码（`tools/add_phone.py 13800138000 --admin`）也一样：
> 这个号码第一次登录时会被要求设置密码。

**管理员要做的第二件事**：右上角「访问管理」→ 填一次 DeepSeek API Key。
填完之后**所有人**都用它 —— 普通成员不需要（也看不到）API Key 入口。

**如果你要部署到公网**，建议**先**用命令行把号码配好，避免"谁先访问谁当管理员"：

```bash
.venv\Scripts\python.exe tools\add_phone.py 13800138000 --name 我 --admin
```

（这个脚本平时用不到，主要是公网部署前的预防手段，以及忘记管理员时的补救。）

---

## 功能

| 功能 | 说明 |
|---|---|
| 自然语言排日程 | 说一句话，AI 识别任务、查天气、判断冲突，放进待确认列表 |
| 待确认 / 待计划 | 时间明确的进 To be confirmed，没定时间的进 To be planned |
| 周视图日历 | 当前周 + Next Week 覆盖层，事件可点开查看/修改/删除 |
| 天气提醒 | 涉及户外的任务（运动、出行、露营）主动查天气并给备选时间 |
| 长期记忆 | 分事实 / 观察台账 / 偏好三块，记错可在「我的记忆」里直接删 |
| 导出 .ics | 下载日历文件，可导入手机自带日历 |
| 手机号白名单 | 只有名单内的人能登录；管理员可在界面上管理名单 |
| **管理员密码** | 管理员登录要验证密码（首次使用时设置），普通成员免密 |
| **统一的 AI Key** | 管理员配一次 DeepSeek Key，所有人共用；接口只返回打码后的样子 |

---

## 技术栈

- Python 3.12 / FastAPI / uvicorn
- **SQLite**（Python 标准库，无需额外服务）
- LangChain（`create_tool_calling_agent` + `AgentExecutor`）
- DeepSeek API（模型推理，Key 由用户自己填）
- Open-Meteo API（天气，免密钥）
- 原生 HTML / CSS / JavaScript

## 项目结构

```
the_kings_hand_demo/
├── main.py                    启动入口
├── config.py                  集中配置与启动自检
├── db/                        数据层（SQLite）
│   ├── schema.sql             表结构（改结构只需改这里）
│   ├── database.py            连接管理、建表、通用查询、旧库补列
│   ├── settings.py            全站共享设置（管理员配的 DeepSeek Key）
│   ├── whitelist.py           手机号白名单 + 管理员密码
│   ├── sessions.py            登录会话
│   ├── calendars.py           日程事件
│   ├── pendings.py            待确认/待计划任务
│   ├── memories.py            长期记忆
│   ├── histories.py           对话历史
│   └── secrets.py             用户 API Key
├── server/
│   ├── app.py                 HTTP 路由
│   ├── auth.py                会话与 CSRF
│   └── logging_config.py      日志格式
├── agent/
│   ├── runner.py              Agent 编排与提示词
│   ├── llm.py                 LLM 客户端（按请求构造）
│   ├── date_tools.py          日期/星期换算工具
│   ├── weather_openmeteo.py   天气工具
│   └── ics_exporter.py        .ics 生成
├── static/                    前端
├── tools/add_phone.py         命令行加白名单
├── tools/make_favicon.py      生成 favicon.ico / favicon.svg（纯标准库，不装 Pillow）
├── tools/make_deploy_stage.py 生成干净的「发布副本」（剔除 .venv / 数据库 / 本地痕迹）
├── tools/smoke_auth.py        端到端冒烟：登录/密码/统一 Key（用临时库，不碰真实数据）
└── data/app.db                ← 所有数据都在这个文件里
```

---

## 数据与备份

**所有数据（日程、待办、记忆、对话、API Key）都在一个文件里：**

```
data/app.db
```

- **备份**：直接复制这个文件（连带 `-wal`、`-shm` 一起复制更稳妥）
- **迁移**：把整个 `data/` 目录拷到新机器即可
- **清空**：删掉 `app.db` 后重启，会重新建一个空库

⚠️ `data/*.db` 已在 `.gitignore` 里排除，**不会被提交到 git**。

---

## 公网部署

这个项目本身就是一个单端口 HTTP 服务，绑到 `0.0.0.0` 就能对外提供访问，不需要额外改造。

```bash
python tools/make_deploy_stage.py        # 生成干净的发布副本，默认 ../the_kings_hand_deploy
```

这条命令会复制全部源码，**并排除下列内容**：

| 排除项 | 原因 |
|---|---|
| `data/app.db` 及其 `-wal` / `-shm` | 里面有管理员密码哈希、你自己配的 DeepSeek Key、本地聊天记录 |
| `.venv` / `__pycache__` | 一百多兆，而且里面的路径是本机 Windows 的 |
| `.env`、`.workbuddy`、`preview/` | 本地配置与过程稿，与运行无关 |

**发布副本的 `data/` 是空的**，所以线上第一件要紧事：

```
打开线上地址 -> 输入手机号 -> 提示"你将成为管理员，请设置密码" -> 设密码进入
-> 右上角「访问管理」里填 DeepSeek API Key -> 全站可用
```

> ⚠️ 因为名单是空的，**谁先访问谁就是管理员**。如果不希望这样，
> 部署前先在线下的库里用 `tools/add_phone.py` 把自己加进去，再连数据一起发布。

### 托管环境会自动适配

`config.py` 里有一条约定：**外部显式注入 `PORT` 环境变量 = 这是托管环境**。检测到之后：

| 项 | 本机 | 注入 PORT 时 |
|---|---|---|
| 监听地址 | `127.0.0.1` | `0.0.0.0`（否则反向代理连不进来） |
| uvicorn reload | 跟随 `DEBUG`（默认开） | 强制关闭（否则会额外起子进程抢端口） |
| 会话 cookie Secure | `false` | 按 `X-Forwarded-Proto` 判断：HTTPS 下自动 `true` |

所以本地开发不用改任何东西，直接 `python main.py` 即可；托管时平台只需要做两件事：
注入 `PORT`、执行 `python main.py`。

---

## 安全说明（重要）

这个演示版做了两处**刻意的安全降级**，换来了简单与速度。请了解后果：

### 1. 手机号白名单 = 普通成员"知道号码即可登录"

**没有短信验证码，普通成员也不需要密码。** 也就是说：

- 名单内任何普通成员的手机号被别人知道，对方就能登录
- 适合"自己用 + 少数熟人"的场景（比如给面试官看）
- 如果要在公网给陌生人用，**必须**加短信验证码或改回邮箱+密码

### 1.5 管理员用密码兜底

管理员能改访问名单、能改全站共用的 AI 服务密钥，只靠"知道手机号"保护不够，
所以给管理员加了密码：

- 存的是 `PBKDF2-HMAC-SHA256`（20 万次迭代）+ 每账号随机 salt，不存在明文
- 第一次使用时设置；之后每次登录都要验证
- 同一个号连续错 5 次 → 锁定 5 分钟（记在内存里，进程重启即清空）
- 在「访问管理」里可以修改自己的密码（要验证旧密码）

⚠️ 这只是"演示级加固"：单机演示够用，上公网请换成真正的身份认证。

### 2. API Key 明文存在 SQLite 里

`data/app.db` 里的 `app_settings` 表存的是**明文** Key（管理员配的那把）。这意味着：

- 这个文件被任何人拿到 = 你的 DeepSeek Key 泄露（可能被盗刷）
- 拷贝/发送项目文件夹时，密钥会跟着走

**建议**：只在本机使用，不要把这个 db 文件发给别人。
如果以后要多人共用或上公网，应改回服务端加密存储
（原项目 `the_kings_hand/agent/crypto.py` 就是那套实现，可以直接移植回来）。

---

## 常见问题

**Q：启动后浏览器提示"抱歉，您没有权限访问"**
说明名单**非空**，但你的号码不在里面。让管理员在「访问管理」里加你，
或自己用 `tools/add_phone.py` 加一个。

**Q：我是第一个用的人，为什么进不去？**
正常情况下名单为空时第一个登录的号码会自动成为管理员。
如果提示无权限，说明名单里已经有别人了（可能你之前已经登过一次，
或数据库里有历史数据）。用 `tools/add_phone.py --list` 看一下名单。

**Q：AI 对话报"还没有配置 DeepSeek API Key"**
说明管理员还没配：**右上角「访问管理」→ 全站 DeepSeek API Key**，填一次，
之后所有成员共用。普通成员看不到这个入口。

**Q：我是普通成员，为什么不能自己填 Key？**
这是刻意的：演示版定位是"管理员把服务配好，其他人直接能用"。
技术上保留了兜底 —— 管理员没配全局 Key 时，你以前自己填的 Key（存在
`user_secrets` 表）仍然有效。

**Q：对话报"API Key 无效或已失效"**
Key 填错了或已过期，让管理员重新填一次。

**Q：怎么改端口 / 允许局域网访问**
改 `.env` 里的 `HOST` 和 `PORT`（`HOST=0.0.0.0` 允许局域网访问）。
详见 `.env.example`。

**Q：数据库报 `database is locked`**
已经配了 WAL 模式和 5 秒 busy_timeout，正常使用不会出现。
如果同时开了多个进程写同一个库，会竞争 —— 保持单进程运行即可。

**Q：管理员忘记密码了怎么办**
没有找回流程（本机 demo），用命令行把密码清掉即可 —— 清完之后下次登录会重新要求设置：

```bash
# 把某个号码的管理员密码清空
.venv\Scripts\python.exe -c "from db import whitelist; whitelist.clear_password('13800138000')"
```

**Q：怎么改数据表结构**
改 `db/schema.sql`，然后删掉 `data/app.db` 重启（或手工执行 ALTER TABLE）。
注意：`init_db()` 用的是 `CREATE TABLE IF NOT EXISTS`，**不会**自动改已存在的表结构
—— 唯一的例外是它会在启动时检查 `_COLUMN_MIGRATIONS` 里登记的旧表缺列并自动补上。

---

## 与原项目的差异对照

| 方面 | 原项目 the_kings_hand | 本项目（演示版） |
|---|---|---|
| 数据存储 | Supabase（云端 Postgres） | 本地 SQLite |
| 登录 | 邮箱 + 密码（Supabase Auth） | 手机号白名单（**管理员额外需要密码**） |
| 多用户隔离 | 数据库 RLS 强制 | 查询时按 phone 过滤 |
| API Key | AES-256-GCM 加密存云端 | 明文存本地 SQLite（管理员统一配一把，全员共用） |
| 对话历史 | 进程内存（有泄漏，后加回收） | 落库，重启不丢 |
| 网络安全 | 跨境 400ms 往返 | 本机回环，毫秒级 |
| 部署 | Docker + Nginx + HTTPS | 直接 `python main.py` |
