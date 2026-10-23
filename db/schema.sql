-- ============================================================================
-- The King's Hand（本地演示版）—— SQLite 表结构
-- ============================================================================
-- 由 db/database.py 在启动时执行（CREATE TABLE IF NOT EXISTS，可重复执行）。
-- 改表结构只需要改这一个文件，然后重启服务。
--
-- 与云端版的结构对应关系：
--   云端（Supabase Postgres）        ->  本地（SQLite）
--   auth.users（Supabase Auth 托管） ->  whitelist（手机号白名单）
--   events                          ->  events
--   pending_tasks                   ->  pending_tasks
--   memories                        ->  memories
--   user_secrets                    ->  user_secrets（改为明文，见 README 说明）
--   （无，对话历史在服务端内存）      ->  histories（新增，落库后可多进程）
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 手机号白名单：能登录的人
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whitelist (
    phone       TEXT PRIMARY KEY,            -- 手机号，作为用户唯一标识
    name        TEXT NOT NULL DEFAULT '',    -- 备注名，方便管理界面辨认
    is_admin    INTEGER NOT NULL DEFAULT 0,  -- 1 = 管理员，可管理白名单
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_login  TEXT,                        -- 最近一次登录时间

    -- 管理员密码：格式 pbkdf2_sha256$<随机 salt>$<哈希>，空串 = 还没设置
    -- ⚠️ 只有管理员用得到；普通成员不设密码（沿用"知道号码即可登录"）。
    --    旧库没有这两列，由 db/database.py 启动时自动 ALTER 补上。
    password_hash    TEXT NOT NULL DEFAULT '',
    password_set_at  TEXT
);

-- ---------------------------------------------------------------------------
-- 登录会话：cookie 里只放随机 token，真正的身份在这里查
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,            -- 随机 token（cookie 里存这个）
    phone       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    expires_at  TEXT NOT NULL,               -- 过期时间，校验时比较
    FOREIGN KEY (phone) REFERENCES whitelist (phone) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_phone ON sessions (phone);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

-- ---------------------------------------------------------------------------
-- 日程事件
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,               -- 归属用户（手机号）
    title       TEXT NOT NULL,
    start_at    TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM'（本地时间文本）
    end_at      TEXT,                        -- 可为空 = 未知时长
    location    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_events_phone_start ON events (phone, start_at);

-- ---------------------------------------------------------------------------
-- 待确认 / 待计划任务
--   bucket = 'confirmed'（有明确开始时间）| 'planned'（还没定时间）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_tasks (
    id          TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    title       TEXT NOT NULL,
    start_at    TEXT,
    end_at      TEXT,
    location    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    bucket      TEXT NOT NULL DEFAULT 'planned',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_pending_phone_bucket ON pending_tasks (phone, bucket);

-- ---------------------------------------------------------------------------
-- 长期记忆：一个用户一行
--   facts        用户明确说过的事实（JSON 数组，LLM 自动蒸馏）
--   preferences  稳定偏好（JSON 数组，**只由用户手动维护**，LLM 只读不写）
--   history      过期日程的沉淀摘要（JSON 数组，见 agent/memory_history.py）
-- 注：observations（观察台账）已取消，偏好不再由 AI 归纳。
--                越老的日程合并得越粗，所以增长远慢于 events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    phone        TEXT PRIMARY KEY,
    facts        TEXT NOT NULL DEFAULT '[]',
    preferences  TEXT NOT NULL DEFAULT '[]',
    history      TEXT NOT NULL DEFAULT '[]',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------------
-- 对话历史：落库后不再依赖进程内存（可多进程、重启不丢）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS histories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    role        TEXT NOT NULL,               -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_histories_phone ON histories (phone, id);

-- ---------------------------------------------------------------------------
-- 用户自己填的 API Key
--   ⚠️ 演示版按用户要求**明文存储**，仅适合本机自用；
--      这个库文件被拷走就等于密钥泄露。详见 README 的安全说明。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_secrets (
    phone        TEXT PRIMARY KEY,
    deepseek_key TEXT NOT NULL DEFAULT '',
    smtp_user    TEXT NOT NULL DEFAULT '',
    smtp_pass    TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------------------
-- 全站共享设置：由管理员统一配置
--   目前只有 deepseek_api_key —— 所有人调用 AI 都用这一份，
--   普通成员不再需要（也不能）自己填 Key。
--   user_secrets 那张表保留作为兜底：管理员没配全局 Key 时，
--   个人自己填的 Key 仍然能用（见 db/secrets.py 的 resolve_deepseek_key）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',    -- 最后修改者的手机号
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
