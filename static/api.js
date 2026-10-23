// ============================================================================
// HTTP 请求封装
//
// 会话是 HttpOnly cookie：JavaScript 读不到会话 token，
// 所以前端不需要（也无法）自己管理 token。只需要：
//   1. 每个请求带 credentials: "same-origin"，让浏览器自动带 cookie
//   2. 写操作带上 X-CSRF-Token 头（值从非 HttpOnly 的 kh_csrf cookie 读）
//
// 收到 401（会话失效）时统一回落到登录页 —— 白名单登录没有续期机制，
// 过期就重新输手机号。
// ============================================================================

const AUTH_CODES = {
  NO_SESSION: "no_session",
  REVOKED: "revoked",
};

function getCookie(name) {
  const target = name + "=";
  for (const part of document.cookie.split(";")) {
    const item = part.trim();
    if (item.startsWith(target)) return decodeURIComponent(item.slice(target.length));
  }
  return "";
}

function getCsrfToken() {
  return getCookie("kh_csrf");
}

async function readError(resp) {
  let detail = "", code = "";
  try {
    const data = await resp.json();
    detail = data.detail || data.message || "";
    code = data.auth_code || "";
  } catch (e) {
    detail = await resp.text().catch(() => "");
  }
  const err = new Error(detail || `请求失败（HTTP ${resp.status}）`);
  err.status = resp.status;
  err.authCode = code;
  return err;
}

/**
 * 统一请求函数。
 * @param {string} path  例如 "/events"
 * @param {object} opts  { method, body }
 */
async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = {};
  const isWrite = !["GET", "HEAD", "OPTIONS"].includes(method);

  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (isWrite) {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });

  if (!resp.ok) {
    const err = await readError(resp);
    // 会话失效/被移出白名单 -> 回登录页
    if (resp.status === 401 || err.authCode === AUTH_CODES.REVOKED) {
      if (typeof onSessionLost === "function") onSessionLost();
    }
    throw err;
  }
  if (resp.status === 204) return null;
  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

/**
 * 流式请求（SSE over fetch）。
 *
 * 为什么不用 EventSource：它只能发 GET，而 /chat/stream 是 POST，
 * 既要带 JSON body 又要带 X-CSRF-Token 头，只能自己读流。
 *
 * @param {string}   path
 * @param {object}   opts     { method, body }
 * @param {function} onEvent  每收到一帧回调一次
 */
async function apiStream(path, opts = {}, onEvent) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = { Accept: "text/event-stream" };
  const isWrite = !["GET", "HEAD", "OPTIONS"].includes(method);

  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (isWrite) {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  const resp = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });

  // 状态码和响应头先到，所以这里还能按老办法处理鉴权失败
  if (!resp.ok) {
    const err = await readError(resp);
    if (resp.status === 401 || err.authCode === AUTH_CODES.REVOKED) {
      if (typeof onSessionLost === "function") onSessionLost();
    }
    throw err;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 一帧以空行结尾；可能一次读到多帧，也可能一帧被拆在两个 chunk 里
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()));
      } catch (e) {
        // 单帧解析失败不应中断整条流
      }
    }
  }
}

// ---------------------------------------------------------------------------
// 各模块的接口
// ---------------------------------------------------------------------------
const SessionApi = {
  // 登录分两段：先只发 phone 问"下一步要什么"，再补 password / new_password
  login: (payload) => api("/session/login", { method: "POST", body: payload }),
  logout: () => api("/session/logout", { method: "POST" }),
  info: () => api("/session"),
  me: () => api("/me"),
};

const CredentialsApi = {
  get: () => api("/credentials"),
  save: (payload) => api("/credentials", { method: "POST", body: payload }),
  clear: () => api("/credentials", { method: "DELETE" }),
};

const MemoryApi = {
  get: () => api("/memory"),
  update: (payload) => api("/memory/update", { method: "POST", body: payload }),
  clear: () => api("/memory/clear", { method: "POST" }),
};

const AdminApi = {
  list: () => api("/admin/whitelist"),
  add: (payload) => api("/admin/whitelist/add", { method: "POST", body: payload }),
  remove: (phone) => api("/admin/whitelist/remove", { method: "POST", body: { phone } }),
  setAdmin: (phone, is_admin) =>
    api("/admin/whitelist/set_admin", { method: "POST", body: { phone, is_admin } }),

  // 全站 DeepSeek Key
  settings: () => api("/admin/settings"),
  saveApiKey: (api_key) => api("/admin/settings/api_key", { method: "POST", body: { api_key } }),
  clearApiKey: () => api("/admin/settings/api_key", { method: "DELETE" }),

  // 管理员自己的登录密码
  changePassword: (old_password, new_password) =>
    api("/admin/password", { method: "POST", body: { old_password, new_password } }),
};
