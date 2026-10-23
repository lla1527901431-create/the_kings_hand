# 部署到一台服务器（SQLite 版）

本文档对应 **2026-10-23 合并后的主干**：数据存本地 SQLite，登录用手机号白名单，
不再依赖 Supabase、也不需要 `SECRET_MASTER_KEY`。

---

## 0. 先建立三个认知

| | 这版的方式 | 意味着什么 |
|---|---|---|
| 数据 | 一个文件 `data/app.db` | 备份 = 复制文件；丢了 = 全丢 |
| 进程 | 单机 **单进程** | 不要开多 worker（见第 11 节） |
| 外部依赖 | 只有 DeepSeek API | 没配 Key 也能启动，只是不能对话 |

---

## 1. 服务器规格

| 项 | 建议 | 说明 |
|---|---|---|
| CPU/内存 | 2 核 2G | 实测常驻 150~250 MB |
| 磁盘 | 40 GB SSD | 数据增长极慢（1000 活跃用户约 100 MB/年） |
| 带宽 | 2 Mbps 起 | SSE 每条消息只传几 KB |
| 地域 | 香港（免备案）或内地（需 ICP 备案） | 这版不再跨境访问数据库，内地也很快 |

---

## 2. 服务器初始化

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# 重新登录一次，让 docker 组生效
```

防火墙只开三个口：

| 端口 | 用途 |
|---|---|
| 22 | SSH（来源限制成你的 IP） |
| 80 | HTTP → HTTPS 跳转、证书签发 |
| 443 | HTTPS |

**不要开放 8000**：应用只绑宿主机回环，外部流量一律走 Nginx。

---

## 3. 上传代码

```bash
# 方式 A：git
git clone <你的仓库地址> kings-hand && cd kings-hand

# 方式 B：本地打包（排除虚拟环境、数据库、密钥）
#   tar --exclude='.venv' --exclude='.env' --exclude='data/*.db*' -czf kh.tar.gz the_kings_hand
#   scp kh.tar.gz ubuntu@你的IP:~/
#   mkdir -p ~/kings-hand && tar -xzf kh.tar.gz -C ~/kings-hand --strip-components=1
```

⚠️ **绝对不要把本地的 `data/app.db` 传到服务器**：里面是管理员密码哈希、
明文 DeepSeek Key 和全部聊天记录。发布副本用 `python tools/make_deploy_stage.py` 生成更稳妥。

---

## 4. 配置 `.env`

`.env` 不在仓库里，需要手工创建（`chmod 600 .env`）：

```ini
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MEMORY_MODEL=deepseek-v4-flash

# ⚠️ 容器里必须显式设 PORT —— 见下面这条“最容易踩的坑”
PORT=8000
DEBUG=false
LOG_LEVEL=INFO
COOKIE_SECURE=true
SESSION_DAYS=30
```

### 最容易踩的坑：`PORT`

`config.py` 的约定是「外部显式注入 `PORT` = 这是托管环境」，这时才会：

- 把 `HOST` 绑到 `0.0.0.0`（否则容器外访问不到）
- 强制关闭 uvicorn 热重载（否则多起一个子进程抢端口）

**所以容器/服务器上必须写 `PORT=8000`**，哪怕默认值就是 8000。少了这一行，
服务只监听 `127.0.0.1`，Nginx 会报 502。

---

## 5. 数据卷

`docker-compose.yml` 已经把 `./data` 挂到容器 `/app/data`：

```yaml
volumes:
  - ./data:/app/data
```

首次启动会自动建库建表（`db/schema.sql`，全部 `IF NOT EXISTS`，可重复执行）。
**这个目录就是你的全部数据**，迁移服务器时整个拷走即可。

⚠️ **首次必须先建目录并交权限**，否则容器里的非 root 用户写不进去，启动报
`unable to open database file`：

```bash
mkdir -p ~/kings-hand/data
sudo chown -R 1000:1000 ~/kings-hand/data
```

---

## 6. 域名与 HTTPS

1. DNS 添加 A 记录指向服务器 IP。
2. 编辑 `deploy/nginx.conf`，把两处 `server_name your-domain.com;` 改成真实域名。
3. 签发证书（用容器跑 certbot）：

```bash
mkdir -p certs www/certbot
sudo docker run -d --name tmp-nginx -p 80:80 \
  -v ~/kings-hand/certs:/etc/nginx/certs -v ~/kings-hand/www:/var/www nginx:1.27-alpine

sudo docker run --rm \
  -v ~/kings-hand/certs:/etc/letsencrypt \
  -v ~/kings-hand/www:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d 你的域名 --email 你的邮箱 --agree-tos --no-eff-email

cd ~/kings-hand/certs
sudo ln -sf live/你的域名/fullchain.pem fullchain.pem
sudo ln -sf live/你的域名/privkey.pem privkey.pem
```

**证书权限**（这一步不做，nginx 容器会启动失败，报 `cannot load certificate ... Permission denied`）：

```bash
sudo chgrp -R 101 ~/kings-hand/certs/live ~/kings-hand/certs/archive
sudo chmod 750 ~/kings-hand/certs/live ~/kings-hand/certs/archive
sudo chmod 640 ~/kings-hand/certs/archive/你的域名/*.pem
sudo chmod 644 ~/kings-hand/certs/live/你的域名/*.pem
sudo docker rm -f tmp-nginx
```

**自动续期**（证书 90 天）：`--deploy-hook` 里的 reload 不能省，否则文件换了、nginx 内存里还是旧证书。

```bash
0 3 1 * * cd ~/kings-hand && docker run --rm -v ~/kings-hand/certs:/etc/letsencrypt \
  certbot/certbot renew --quiet --deploy-hook "docker compose exec -T nginx nginx -s reload"
```

---

## 7. 启动与首次使用

```bash
docker compose up -d --build
docker compose logs -f app      # 启动自检会在这里报配置问题
curl -s http://127.0.0.1:8000/health
```

首次访问 `https://你的域名`：

1. **第一个输入手机号的人自动成为管理员**，并当场设置密码（≥6 位）。
2. 进「访问管理」→ 填全站 DeepSeek API Key（所有人共用）。
3. 之后在「访问管理」里添加其他人的手机号。

> 不想让"第一个访客"当管理员？部署前先在本地用
> `python tools/add_phone.py 13800138000 --admin` 加好号码，再连 `data/` 一起发布。

---

## 8. 备份与恢复（最重要的一节）

### 每日备份

```bash
# 用 SQLite 自带命令，能在服务运行中安全导出一致快照（不要直接 cp，WAL 会丢数据）
mkdir -p ~/backup
sqlite3 ~/kings-hand/data/app.db "VACUUM INTO '/root/backup/app-$(date +%F).db'"
```

写进 crontab：

```bash
0 3 * * * sqlite3 /root/kings-hand/data/app.db "VACUUM INTO '/root/backup/app-$(date +\%F).db'" && find /root/backup -name 'app-*.db' -mtime +14 -delete
```

### 异地

```bash
# 用 rclone 同步到对象存储（oss/s3/坚果云均可）
rclone sync /root/backup remote:kings-hand-backup --max-age 30d
```

### 恢复

```bash
docker compose stop app
cp /root/backup/app-2026-10-20.db ~/kings-hand/data/app.db
rm -f ~/kings-hand/data/app.db-wal ~/kings-hand/data/app.db-shm
docker compose start app
```

### 每月做一次恢复演练

把备份拷到另一个目录、起一个临时实例、登录看一眼数据在不在。
**没验证过的备份等于没有备份。**

---

## 9. 日常运维

| 操作 | 命令 |
|---|---|
| 看日志 | `docker compose logs -f app` |
| 重启 | `docker compose restart app` |
| 更新代码 | `git pull && docker compose up -d --build` |
| 看数据文件大小 | `ls -lh data/` |
| 检查表行数 | `curl -s http://127.0.0.1:8000/health` |

---

## 10. 上线自查清单

- [ ] `https://你的域名` 能打开登录页
- [ ] 第一个手机号设置密码后能进入
- [ ] 「访问管理」里配好全站 DeepSeek Key，能和 AI 正常对话
- [ ] `/health` 返回 200，且能看到各表行数
- [ ] **`data/app.db` 不在 git 里**（`git status` 干净）
- [ ] 每日备份 + 异地同步已经跑过一次
- [ ] 做过一次恢复演练

---

## 11. 常见问题

**Q：`database is locked`**
已经配了 WAL + 5 秒 busy_timeout，正常使用不会出现。出现说明有多个进程在写同一个库
—— 确认 `WORKERS=1`、没有额外起的实例、没有别的机器挂同一个 `data/` 目录。

**Q：能不能开多 worker 提升并发？**
**不要。** 两个原因：SQLite 只有一个写锁，多进程只会互相排队；登录失败计数存在进程内存里，
多进程会各算各的。实测单进程写入上限 1000~2400 次/秒，远超实际需求。

**Q：改了 `.env` 不生效**
`docker compose up -d`（重建）才会重新读 `env_file`，`restart` 不一定。

**Q：登录后刷一下就掉了**
看 `COOKIE_SECURE`。HTTPS 下应为 `true`；本地 http 调试必须 `false`，否则浏览器拒绝保存 cookie。

**Q：管理员忘记密码**
```bash
docker compose exec app python -c "from db import whitelist; whitelist.clear_password('13800138000')"
```
清空后下次登录会重新要求设置。

**Q：数据要不要定期清理**
`histories` 每人只留最近 10 条，自动裁剪；`sessions` 每次登录顺手清过期。
真正持续增长的是 `events`（日程）和 `memories`，但速度很慢。

**Q：从云端版（Supabase）迁过来的数据呢**
**不迁移**。两边的表结构、主键类型、时间格式都不同（uuid/jsonb/timestamptz → text），
云端的老数据只能人工导出成 CSV 再写脚本导入。这版是重新起步。

---

## 12. 安全提醒

| 事项 | 说明 |
|---|---|
| `data/app.db` | 里面有**明文** DeepSeek Key 和管理员密码哈希，不能进 git、不能随便发人 |
| `.env` | `chmod 600`，不进仓库 |
| 8000 端口 | 不对外开放 |
| 登录限流 | 现在只有内存版的 5 次/5 分钟，公网可再加 Nginx 的 `limit_req` |
| 白名单模式 | 普通成员"知道手机号就能登录"，只适合熟人小圈子；要开放注册必须加短信验证码 |
