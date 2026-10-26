# The King's Hand

一个用自然语言安排日程的 AI 助手。数据存在本地 SQLite，登录用手机号白名单，
管理员配置的 API Key **加密后**入库，可以单机跑，也可以一键部署成在线演示。

> 想看旧的云端架构（Supabase + 邮箱密码 + 云端 RLS），见分支 `archive/supabase`。
> 本分支保留了 Docker + Nginx 的部署方式（见 `DEPLOY.md`），但数据层完全换成了 SQLite。

## 在线演示

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/lla1527901431-create/the_kings_hand)

- 免费实例 **15 分钟无访问会休眠**，下次打开要等 30~50 秒冷启动
- 文件系统是临时的：重新部署/重启后 `data/app.db` 会重置（演示可接受）
- 部署后**第一个登录的手机号自动成为管理员**，进「访问管理」配 DeepSeek / 高德 Key
- 不想被陌生人抢先当管理员？先在本地用 `tools/add_phone.py` 配好号码并设密码，再连 `data/` 一起部署

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

首次启动会自动做三件事，日志里会逐条打印：建库建表、生成加密用的主密钥
`data/.secret_key`、提示白名单是否为空。

### 第一次怎么进去

名单是空的，所以**第一个输入手机号的人会自动成为管理员** ——
相比普通成员多一步：**当场设置一个登录密码**（至少 6 位）。

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

**管理员要做的第二件事**：右上角「访问管理」→ 填一次 DeepSeek API Key
（可选再填高德 Key，用于路况）。填完之后**所有人**都用它 ——
普通成员不需要、也看不到 API Key 入口。两把 Key 都是加密存的，见下一节。

**如果你要部署到公网**，建议**先**用命令行把号码配好，避免"谁先访问谁当管理员"：

```bash
.venv\Scripts\python.exe tools\add_phone.py 13800138000 --name 我 --admin
```

（这个脚本平时用不到，主要是公网部署前的预防手段，以及忘记管理员时的补救。）

---

## 密钥是怎么存的

管理员填的 DeepSeek Key / 高德 Key **不是明文**。这一节说明它是怎么保护起来的，
以及你必须做什么才不会把自己锁在外面。

### 加密本身

| 项 | 实现 |
|---|---|
| 算法 | **AES-256-GCM**（`cryptography` 库），带完整性校验，密文被改一个字节就解不开 |
| 随机数 | 每次加密生成新的 12 字节 nonce，所以同一个 Key 加密两次结果不同 |
| 密文格式 | `enc:v1:<base64(nonce + ciphertext)>`，带版本前缀，以后换算法可按前缀分流 |
| 落库位置 | `app_settings` 表的 `value` 列（只对这两把 Key 生效） |
| 谁负责 | `db/crypto.py`（加解密）+ `db/settings.py`（透明收发，上层代码不感知） |

数据库里其他内容（日程、聊天、记忆）**没有加密**：加密它们会让 SQL 查询和索引失效，
而威胁模型是"库文件被拷走时别泄漏第三方 API Key"，不是磁盘被物理控制。

### 主密钥从哪来（两种方式，二选一）

**方式一：文件（默认，适合本机）**

首次启动自动生成 `data/.secret_key`，32 字节随机数，权限 0600。
它和数据库在同一个目录，已在 `.gitignore` 里。

**方式二：环境变量（推荐给容器 / 托管平台）**

```bash
# 生成一把
python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

# 注入（名字固定）
KH_SECRET_KEY=<上面那串>
```

值是 32 字节的 base64；也可以直接给一段长口令，代码会用 SHA-256 派生。

⚠️ **两种方式别混用**。先用文件跑出一批密文，之后改成 env 注入，
密钥一换，旧密文就解不开了（表现为"Key 未配置"，重填即可，数据不会丢）。
启动日志里会打印主密钥**指纹**（12 位十六进制），可以拿来核对线上和环境里是不是同一把：

```
提示：密钥加密就绪：主密钥来源 file，指纹 d8f3f542a66d
```

### 备份和迁移必须带上主密钥

```
data/app.db          ← 数据（Key 在里面是密文）
data/.secret_key     ← 解密用的主密钥
```

**只备份 app.db 不备份 `.secret_key`，Key 就找不回来了**（其它数据不受影响）。
用环境变量注入的场景则要把 `KH_SECRET_KEY` 存进密钥管理系统，而不是跟着文件走。

### 旧版本升级过来会怎样

不用手工迁移。库里原来是明文的，第一次被读取时会**就地加密回写**，
并保留原来的"修改人 / 修改时间"，日志里会有一条：

```
INFO | settings | 设置项 deepseek_api_key 已从明文迁移为密文
```

### 解不开的时候

密文被改坏、或者主密钥换了，`decrypt()` 返回空串而不是抛异常，
界面表现为"Key 未配置"，管理员重填一次就被新密钥覆盖。整站不会因此起不来。

---

## 功能

| 功能 | 说明 |
|---|---|
| 自然语言排日程 | 说一句话，AI 识别任务、查天气、判断冲突，放进待确认列表 |
| 待确认 / 待计划 | 时间明确的进 To be confirmed，没定时间的进 To be planned |
| 周视图日历 | 当前周 + Next Week 覆盖层，事件可点开查看/修改/删除 |
| 天气提醒 | 涉及户外的任务（运动、出行、露营）主动查天气并给备选时间 |
| 路况预估 | 配了高德 Key 后，能按实时路况算"几点该出门" |
| 长期记忆 | 事实（AI 自动提取）+ 偏好（**你自己手动维护**）；过往安排做成 `search_history` 工具，AI 按需查 |
| 导出 .ics | 下载日历文件，可导入手机自带日历 |
| 手机号白名单 | 只有名单内的人能登录；管理员可在界面上管理名单 |
| **管理员密码** | 管理员登录要验证密码（首次使用时设置），普通成员免密 |
| **加密的 AI Key** | 管理员配一次 DeepSeek / 高德 Key，所有人共用；AES-256-GCM 加密入库，接口只返回打码后的样子 |

---

## 技术栈

- Python 3.12 / FastAPI / uvicorn
- **SQLite**（Python 标准库，无需额外服务）
- **cryptography**（API Key 的 AES-256-GCM 加密）
- LangChain（`create_tool_calling_agent` + `AgentExecutor`）
- DeepSeek API（模型推理，Key 由管理员统一配置）
- Open-Meteo API（天气，免密钥）+ 高德 Web 服务（路况，可选）
- 原生 HTML / CSS / JavaScript（无构建步骤）

## 项目结构

```
the_kings_hand/
├── main.py                    启动入口
├── config.py                  集中配置与启动自检
├── db/                        数据层（SQLite）
│   ├── schema.sql             表结构（改结构只需改这里）
│   ├── database.py            连接管理、建表、通用查询、旧库补列
│   ├── crypto.py              API Key 的加密/解密、主密钥管理
│   ├── settings.py            全站共享设置（加密的 DeepSeek / 高德 Key）
│   ├── whitelist.py           手机号白名单 + 管理员密码
│   ├── sessions.py            登录会话
│   ├── calendars.py           日程事件
│   ├── pendings.py            待确认/待计划任务
│   ├── memories.py            长期记忆
│   └── histories.py           对话历史
├── server/
│   ├── app.py                 HTTP 路由
│   ├── auth.py                会话与 CSRF
│   └── logging_config.py      日志格式
├── agent/
│   ├── runner.py              Agent 编排与提示词
│   ├── llm.py                 LLM 客户端（按请求构造）
│   ├── date_tools.py          日期/星期换算工具
│   ├── weather_openmeteo.py   天气工具（免密钥）
│   ├── geo_tools.py           定位 / 地点搜索 / 路况工具
│   ├── amap.py                高德接口封装
│   ├── history_tools.py       search_history 工具
│   ├── pending_tools.py       待办更新工具
│   ├── memory_history.py      过期日程 → 记忆沉淀（写 history 后删 events）
│   └── ics_exporter.py        .ics 生成
├── static/                    前端（index.html + app/session/store/api + 两份 css）
├── deploy/nginx.conf          HTTPS 反向代理配置
├── Dockerfile                 生产镜像
├── docker-compose.yml         app + nginx 一键启动
├── tools/
│   ├── add_phone.py           命令行加白名单
│   ├── distill_memory.py      手工补跑「过期日程沉淀」（支持 --dry-run）
│   ├── make_favicon.py        生成 favicon.ico / favicon.svg
│   ├── make_deploy_stage.py   生成干净的「发布副本」
│   └── smoke_*.py             离线回归测试（见下）
└── data/
    ├── app.db                 ← 所有数据都在这个文件里
    └── .secret_key            ← 加密用的主密钥（自动生成）
```

### 回归测试

`tools/smoke_*.py` 全部离线：自己建临时库、桩掉外部 API，不需要任何 Key。
CI（`.github/workflows/smoke.yml`）在每次 push / PR 时全跑一遍：

| 脚本 | 覆盖 |
|---|---|
| `smoke_auth.py` | 登录两段式、管理员密码、CSRF、统一 Key、favicon |
| `smoke_crypto.py` | 加解密往返、密文落库、懒迁移、篡改容错、主密钥派生 |
| `smoke_memory.py` | 过期日程沉淀：分桶、事务、幂等、失败不删、库迁移 |
| `smoke_stream.py` | SSE 流式协议（桩 agent，不联网） |
| `smoke_pending.py` | 待办解析与"几点该出门" |
| `smoke_amap.py` | 高德错误码翻译 |
| `smoke_geo.py` | 定位 / 地点搜索 / 路况（桩 HTTP） |

---

## 数据与备份

**除主密钥外，所有数据（日程、待办、记忆、对话、加密后的 API Key）都在一个文件里：**

```
data/app.db += data/.secret_key
```

- **备份**：`app.db` 和 `.secret_key` **两个都要**（连带 `-wal`、`-shm` 更稳妥）；
  用 `KH_SECRET_KEY` 注入的场景则备份那把密钥
- **迁移**：把整个 `data/` 目录拷到新机器即可
- **清空**：删掉 `app.db` 后重启，会重新建一个空库（`.secret_key` 可留可删；
  删了会重新生成，旧库里的 Key 也就解不开了）

⚠️ `data/*.db`、`data/.secret_key` 都在 `.gitignore` 里，**不会被提交到 git**。

### 过期日程会被物理删除

「本周一之前」的日程会在登录后自动沉淀进长期记忆（`memories.history`），
然后把原行从 `events` 删掉——所以**备份是找回历史原文的唯一途径**。

- 用户手动删除的日程**不会**进记忆（删掉的行自然不在沉淀范围内）
- 沉淀粒度随年龄变粗：≤7 天逐条、≤30 天按天、≤180 天按周、更久按月
- 先写记忆、再删日程，同一个事务；摘要调用失败则一条都不删
- 每个用户每天最多跑一次（记在 `app_settings` 的 `memory_distill:last_run:<手机号>`）
- 想先看效果：`python tools/distill_memory.py --phone 138... --dry-run`

### 记忆分三块，职责不同

| 块 | 谁写 | 怎么用 |
|---|---|---|
| `facts` | AI 从对话里自动提取 | 每轮注入提示词 |
| `preferences` | **只有用户自己**在「我的记忆」里添加 | 每轮注入提示词，AI 只读不写 |
| `history` | 过期日程自动沉淀 | **不注入**，AI 通过 `search_history` 工具按需查询 |

`observations`（观察台账）已经取消：偏好改由用户维护后，它没有消费者，
而且实测记的全是一次性日程，与 `history` 重复。

---

## 公网部署

这个项目本身就是一个单端口 HTTP 服务，绑到 `0.0.0.0` 就能对外提供访问。
两种方式都提供：

**A. 一台自己的服务器（Docker + Nginx + HTTPS）** —— 完整步骤见 `DEPLOY.md`：

```bash
mkdir -p data && sudo chown -R 1000:1000 data
docker compose up -d --build
```

**B. 托管平台（Render 免费档）** —— 直接点 README 顶部的 Deploy 按钮，或：

```bash
python tools/make_deploy_stage.py        # 生成干净的发布副本，默认 ../the_kings_hand_deploy
```

这条命令会复制全部源码，**并排除下列内容**：

| 排除项 | 原因 |
|---|---|
| `data/app.db`、`data/.secret_key` 及其 `-wal` / `-shm` | 里面有管理员密码哈希、加密的 Key、聊天记录和主密钥 |
| `.venv` / `__pycache__` | 一百多兆，而且里面的路径是本机 Windows 的 |
| `.env`、`.workbuddy`、`preview/` | 本地配置与过程稿，与运行无关 |

**发布副本的 `data/` 是空的**，所以线上第一件要紧事：

```
打开线上地址 -> 输入手机号 -> 提示"你将成为管理员，请设置密码" -> 设密码进入
-> 右上角「访问管理」里填 DeepSeek API Key -> 全站可用
```

> ⚠️ 因为名单是空的，**谁先访问谁就是管理员**。如果不希望这样，
> 部署前先在线下的库里用 `tools/add_phone.py` 把自己加进去（并登录设一次密码），
> 再连数据一起发布。

### 托管环境会自动适配

`config.py` 里有一条约定：**外部显式注入 `PORT` 环境变量 = 这是托管环境**。检测到之后：

| 项 | 本机 | 注入 PORT 时 |
|---|---|---|
| 监听地址 | `127.0.0.1` | `0.0.0.0`（否则反向代理连不进来） |
| uvicorn reload | 跟随 `DEBUG`（默认开） | 强制关闭（否则会额外起子进程抢端口） |

**所以本地不要在 `.env` 里写 `PORT`** —— 写了就会被当成托管环境，热重载静默关闭。
本机想换端口用命令行：`PORT=8010 python main.py`。

---

## 安全说明（重要）

这个演示版做了两处**刻意的取舍**，换来了简单与速度。请了解后果：

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

⚠️ **还没有设置密码的管理员，等于没有密码**：此时任何人只要在登录页输入这个号码，
就会被引导"设置密码"并直接登进去。所以预置管理员之后，**务必先用它登录一次把密码设好**，
再开放访问（尤其是公网部署）。

⚠️ 这只是"演示级加固"：单机演示够用，上公网请换成真正的身份认证。

### 2. API Key 是加密存的，但主密钥就在同一台机器上

`app_settings` 里存的是 AES-256-GCM 密文（见「密钥是怎么存的」），
**只拿到 `app.db` 拿不到 Key**。但要清楚它的边界：

- 主密钥文件 `data/.secret_key` 和数据库在同一目录（或同一台机器的环境变量里）
- **谁能读到整个 `data/` 目录（或能执行代码），谁就能解密** —— 防的是"库文件单独被拷走/误传"
- 因此：`data/` 目录权限收紧、备份加密、不要把它放进公开的网盘或仓库

想更进一步（防住"能登机器的人"），就得把主密钥挪到独立的密钥管理服务
（KMS / Vault），本项目没有内置这一层。

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

**Q：配好了 Key，重启后却提示未配置**
多半是主密钥变了：检查 `data/.secret_key` 还在不在、`KH_SECRET_KEY` 有没有被改过。
启动日志里的"指纹"能直接看出来。重填一次 Key 即可恢复。

**Q：换服务器 / 换机器怎么搬？**
整个 `data/` 目录拷过去（`app.db` + `.secret_key`），或者两边都用同一个
`KH_SECRET_KEY`。只拷 `app.db` 的话，Key 需要重填。

**Q：数据库里的 Key 能直接看到吗？**
不能，看到的是 `enc:v1:...`。想确认某把 Key 是否配好，看「访问管理」里的打码值
（头 6 位 + 尾 4 位）。

**Q：怎么改端口 / 允许局域网访问**
命令行设 `HOST` / `PORT`（`HOST=0.0.0.0` 允许局域网访问），或写进 `.env`
—— 但注意 `PORT` 会触发"托管模式"（见「托管环境会自动适配」）。
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

| 方面 | 原项目 the_kings_hand | 本项目 |
|---|---|---|
| 数据存储 | Supabase（云端 Postgres） | 本地 SQLite |
| 登录 | 邮箱 + 密码（Supabase Auth） | 手机号白名单（**管理员额外需要密码**） |
| 多用户隔离 | 数据库 RLS 强制 | 查询时按 phone 过滤 |
| API Key | AES-256-GCM 加密存云端 | **同样 AES-256-GCM**，密文存本地 SQLite，主密钥在本机/环境变量 |
| 对话历史 | 进程内存（有泄漏，后加回收） | 落库，重启不丢 |
| 网络安全 | 跨境 400ms 往返 | 本机回环，毫秒级；公网靠 Nginx + HTTPS |
| 部署 | Docker + Nginx + HTTPS | 同左，另加 Render 一键部署 |
