# 部署到腾讯云（The King's Hand）

本文档面向"从零开始在腾讯云跑起来"。按顺序做即可。

---

## 0. 先看这一节：**服务器买在哪个地域**（很关键）

你的数据存在 **Supabase**（海外机房），而用户和服务器要来回访问它。
所以"服务器选在哪个地域"直接决定快慢：

| 地域 | 到 Supabase 的延迟 | 国内用户访问速度 | 需要 ICP 备案 | 建议 |
|---|---|---|---|---|
| **中国香港** | 低（约 30~80ms） | 较好 | **不需要** | ✅ **推荐** |
| 新加坡 | 较低 | 一般 | 不需要 | 可以 |
| 广州 / 上海 / 北京 | **高（约 400ms 起，跨境）** | 最快 | **必须备案** | ⚠️ 不推荐 |

**为什么强烈建议香港**：

- 我们实测过：从这台机器到 Supabase，**单次数据库往返约 400ms**。如果服务器放在内地，
  每个用户操作都要等这个跨境延迟，而你又不是只打一次数据库（一次页面加载 5 次往返）。
- 香港地域**免备案**，买完就能用域名 + HTTPS。
- 内地地域必须**先完成 ICP 备案**才能解析域名，流程要几天到两周。

> 如果你以后想让内地用户访问更快，可以把数据库迁到腾讯云自己的数据库
> （但那就是另一套改造了，现在不做）。

---

## 1. 准备清单

在开始之前你需要：

- [ ] 腾讯云账号，并购买 **CVM 云服务器**（建议 2 核 2G 起，系统选 **Ubuntu 22.04**）
- [ ] 一个**已实名的域名**（香港地域不需要备案）
- [ ] 本地项目里 `.env` 的这四项内容：
      `SUPABASE_URL`、`SUPABASE_PUBLISHABLE_KEY`、`SECRET_MASTER_KEY`、模型名配置

⚠️ **`SECRET_MASTER_KEY` 必须和本地保持完全一致**。
它是用来加解密用户 API Key 的 —— 换了它，用户已保存的 Key 全部解不开。

---

## 2. 服务器初始化

SSH 登录服务器后（腾讯云控制台可以网页登录，也可以本地 `ssh ubuntu@你的IP`）：

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Docker（官方脚本，会自动装 docker compose 插件）
curl -fsSL https://get.docker.com | sudo sh

# 让当前用户能用 docker（免 sudo），然后**重新登录一次**生效
sudo usermod -aG docker $USER
exit
```

重新 SSH 进来后验证：

```bash
docker --version
docker compose version
```

### 开放防火墙端口

腾讯云控制台 → 你的云服务器 → **防火墙** → 添加规则：

| 端口 | 协议 | 来源 | 用途 |
|---|---|---|---|
| 22 | TCP | 你的 IP（更安全） | SSH |
| 80 | TCP | 0.0.0.0/0 | HTTP（用于跳转 HTTPS 和证书签发） |
| 443 | TCP | 0.0.0.0/0 | HTTPS |

**不要开放 8000** —— 应用只监听本机回环，外部流量一律走 Nginx。

---

## 3. 上传代码

两种方式，选一个：

**方式 A：用 git（推荐，以后更新方便）**

```bash
# 在服务器上
git clone <你的仓库地址> kings-hand
cd kings-hand
```

**方式 B：从本地上传（服务器上没配 git 时）**

在**本地** PowerShell 里执行（排除虚拟环境和密钥）：

```powershell
cd F:\Project_2_langchain
# 先打个包，注意排除 .venv 和 .env
tar --exclude=the_kings_hand/.venv --exclude=the_kings_hand/.env `
    --exclude=the_kings_hand/.git -czf kings-hand.tar.gz the_kings_hand

# 上传（把 IP 换成你的）
scp kings-hand.tar.gz ubuntu@你的服务器IP:~/
```

然后在服务器上解包：

```bash
mkdir -p ~/kings-hand && tar -xzf ~/kings-hand.tar.gz -C ~/kings-hand --strip-components=1
cd ~/kings-hand
```

---

## 4. 配置 `.env`

`.env` **不在代码仓库里**（这是故意的），需要你在服务器上手工创建：

```bash
cd ~/kings-hand
nano .env
```

把下面内容粘进去，**把占位符替换成你自己的值**：

```ini
# ---- DeepSeek 模型（key 由用户自己在页面上填，这里不配 key）----
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MEMORY_MODEL=deepseek-v4-flash

# ---- Supabase ----
SUPABASE_URL=https://你的项目.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_你的key

# ---- 用户密钥加密主密钥（必须与本地一致！）----
SECRET_MASTER_KEY=你的主密钥

# ---- 运行模式 ----
DEBUG=false
COOKIE_SECURE=true
LOG_LEVEL=INFO
```

保存：`Ctrl+O` → 回车 → `Ctrl+X`

**保护文件权限**（只有自己能读）：

```bash
chmod 600 .env
```

---

## 5. 配置域名与 HTTPS

### 5.1 解析域名

腾讯云控制台 → **DNS 解析 DNSPod** → 添加记录：

| 主机记录 | 类型 | 记录值 |
|---|---|---|
| `@`（或 `www`） | A | 你的服务器公网 IP |

等几分钟，用 `ping 你的域名` 确认能解析到服务器 IP。

### 5.2 改 Nginx 配置里的域名

```bash
cd ~/kings-hand
nano deploy/nginx.conf
```

把两处 `server_name your-domain.com;` 改成你的真实域名。

### 5.3 签发免费证书（Let's Encrypt）

**先临时用 HTTP 启动 Nginx**，以便通过域名验证：

```bash
mkdir -p certs www/certbot

# 先用一份最简配置只跑 80 端口
sudo docker run -d --name tmp-nginx -p 80:80 \
  -v ~/kings-hand/certs:/etc/nginx/certs \
  -v ~/kings-hand/www:/var/www \
  nginx:1.27-alpine
```

然后用 certbot 签发（**不用装到系统里，用容器跑**）：

```bash
sudo docker run --rm \
  -v ~/kings-hand/certs:/etc/letsencrypt \
  -v ~/kings-hand/www:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d 你的域名 --email 你的邮箱 --agree-tos --no-eff-email
```

成功后证书在 `~/kings-hand/certs/live/你的域名/` 下。把需要的两个文件软链到 nginx 期望的位置：

```bash
cd ~/kings-hand/certs
sudo ln -sf live/你的域名/fullchain.pem fullchain.pem
sudo ln -sf live/你的域名/privkey.pem privkey.pem

# ⚠️ 关键一步：修权限，否则容器里的 nginx 读不到证书、启动直接失败
#    certbot 默认把私钥设成 600、目录 700，只有 root 能读；
#    容器里的 nginx 进程不是 root，会报
#      "cannot load certificate ... Permission denied"
#
#    做法：把属组改成 nginx 容器用的组 id（101 = nginx 用户组），给组读权限。
sudo chgrp -R 101 ~/kings-hand/certs/live ~/kings-hand/certs/archive
sudo chmod 750 ~/kings-hand/certs/live ~/kings-hand/certs/archive
sudo chmod 640 ~/kings-hand/certs/archive/你的域名/*.pem
sudo chmod 644 ~/kings-hand/certs/live/你的域名/*.pem

# 验证：能打印出证书第一行，就说明 nginx 容器读得到了
sudo docker run --rm -v ~/kings-hand/certs:/etc/nginx/certs:ro nginx:1.27-alpine \
  cat /etc/nginx/certs/fullchain.pem | head -1

# 收尾：删掉临时 nginx
sudo docker rm -f tmp-nginx
```

> **如果那条 `cat` 报 `Permission denied`**，说明权限还没对。
> 最省事的兜底是直接把证书复制成普通可读（就不再依赖软链和那个组 id）：
> ```bash
> sudo cp ~/kings-hand/certs/live/你的域名/fullchain.pem ~/kings-hand/certs/
> sudo cp ~/kings-hand/certs/live/你的域名/privkey.pem   ~/kings-hand/certs/
> sudo chmod 644 ~/kings-hand/certs/fullchain.pem
> sudo chmod 600 ~/kings-hand/certs/privkey.pem
> ```
> 但**每次证书续期后都要重新复制一遍**（用软链则不用）。

> **证书 90 天过期，需要自动续期**。在服务器 crontab 里加：
> ```bash
> # 每月 1 号凌晨 3 点尝试续期；成功后重载 nginx 让新证书生效
> 0 3 1 * * cd ~/kings-hand && docker run --rm -v ~/kings-hand/certs:/etc/letsencrypt certbot/certbot renew --quiet --deploy-hook "docker compose exec -T nginx nginx -s reload"
> ```
> ⚠️ **`--deploy-hook ... nginx -s reload` 不能省**：
> 续期只是换了磁盘上的文件，nginx **不会自动重新加载**。
> 不加这一步，文件是新的、nginx 内存里还是旧证书，浏览器会报过期。

> **证书会自动过期（90 天）**，需要定期续期 —— 具体命令见上面 5.3 节末尾的 crontab 说明
> （关键是 `--deploy-hook ... nginx -s reload`，少了它续期不会生效）。

---

## 6. 启动

```bash
cd ~/kings-hand
docker compose up -d --build
```

看启动日志（**这一步很重要，启动自检会在这里报配置问题**）：

```bash
docker compose logs -f app
```

正常应该看到类似：

```
2026-09-28 17:00:00 | INFO  | main | 启动 The King's Hand  host=0.0.0.0 port=8000 debug=False workers=1
2026-09-28 17:00:01 | INFO  | main | Application startup complete
```

如果看到 `配置提醒：COOKIE_SECURE 为 false` 之类的警告，按提示修 `.env` 后
`docker compose up -d` 重启。

### 验证

```bash
# 容器状态
docker compose ps

# 本机自检
curl -s http://127.0.0.1:8000/health

# 走域名（应该能拿到 HTML）
curl -sI https://你的域名/ | head -3
```

浏览器打开 `https://你的域名`，应该看到登录页。

---

## 7. 日常运维

| 操作 | 命令 |
|---|---|
| 看日志（实时） | `docker compose logs -f app` |
| 看最近 200 行 | `docker compose logs --tail=200 app` |
| 重启应用 | `docker compose restart app` |
| 改完代码后重新部署 | `git pull && docker compose up -d --build` |
| 停止全部 | `docker compose down` |
| 查看资源占用 | `docker stats` |

### 日志在哪里

- **应用日志**：`docker compose logs app`（json-file 驱动，已配置 10MB × 5 个上限，不会撑满磁盘）
- **Nginx 日志**：`docker compose logs nginx`
- 每条应用日志都带**时间戳 / 级别 / 模块 / 请求 ID / 用户 ID / key 来源**，
  排查问题时先拿 `X-Request-Id` 响应头去日志里搜。

---

## 8. 上线后必做的检查清单

- [ ] `https://你的域名` 能打开登录页
- [ ] 注册一个账号 → 能收到确认邮件（或已在 Supabase 关掉邮箱验证）
- [ ] 在页面上填自己的 DeepSeek Key → 能正常对话
- [ ] 数据只属于自己的账号（换一个账号登录，看不到前一个的数据）
- [ ] `https://你的域名/health` 返回 200
- [ ] **`COOKIE_SECURE=true`**（浏览器 F12 → Application → Cookies，看 `kh_access` 的 Secure 列是否打勾）
- [ ] 在 Supabase 把 **Site URL / Redirect URLs** 改成的你的域名
      （Authentication → URL Configuration）

---

## 9. 常见问题

**Q：页面能打开但一直提示未登录 / 登录后刷一下就掉了**
多半是 cookie 问题：
- 如果你用 `http://` 访问而 `COOKIE_SECURE=true`，浏览器会**拒绝保存** cookie → 必须用 `https://`
- 反之如果域名是 https 而 `COOKIE_SECURE=false`，某些浏览器策略下 cookie 行为也会异常 → 设成 `true`

**Q：操作很慢（几秒才响应）**
先看是不是服务器地域的问题（第 0 节）。另外确认连接池生效：日志里连续请求的耗时应该在
几百毫秒级；如果每个请求都 5 秒左右，说明连接没被复用（检查是否 `WORKERS` 设得很大）。
另：**首次请求**本来就会慢一次（要建连），这是跨境链路的固有成本。

**Q：`docker compose up` 报端口被占用**
80/443 被别的服务占了（比如系统自带的 nginx/apache）：
```bash
sudo systemctl stop nginx apache2 2>/dev/null
sudo systemctl disable nginx apache2 2>/dev/null
```

**Q：改了 `.env` 不生效**
`docker compose up -d` 会重建容器读取新环境变量；只 `restart` 有时不会重新读取 `env_file`。

**Q：用户说自己填的 Key 没了 / 解不开**
检查 `SECRET_MASTER_KEY` 是不是被换过。它是不可更换的 —— 换了之后用户必须重填 Key。
启动日志里会打印密钥指纹，例如：

```
WARNING | main | 配置提醒：SECRET_MASTER_KEY 指纹：8a38601b000c
```

部署后核对这个指纹和本机是否一致，就能确认没有抄错。
（指纹是 sha256 前 12 位，无法反推出密钥，可以安全地出现在日志里。）

**Q：能不能开多个 worker 提升并发？**
**暂时不要。** 对话上下文目前存在**进程内存**里，开多进程会出现
"同一个人这次请求打到 A 进程、下次打到 B 进程，于是上下文丢失"的现象，
表现为 AI 突然忘记刚才聊了什么。

```yaml
# docker-compose.yml 里保持
WORKERS: "1"
```

如果以后确实需要多进程，得先把对话历史迁到数据库（或引入 Redis），
那是另一项改造。

---

## 10. 安全提醒（上线后）

| 事项 | 说明 |
|---|---|
| **`SECRET_MASTER_KEY`** | 离线备份。丢了 = 所有用户 Key 永久失效 |
| **不要提交 `.env`** | `.gitignore` 已覆盖，但每次 `git add` 后建议 `git status` 确认一下 |
| **不要开放 8000 端口** | 应用只监听 `127.0.0.1`，外部只能经 Nginx |
| **Supabase 的 `service_role` key** | **永远不要**放进这个项目（启动自检会直接拒绝） |
| **测试账号** | 上线前删掉所有测试账号 |
| **登录限流** | 目前没有做。公网开放后建议在 Nginx 对 `/session/login` 加 `limit_req` |
