// ============================================================================
// 登录与会话 UI、API Key 面板、我的记忆面板、访问管理面板
// ============================================================================

let currentUser = null;
let deepseekStatus = { configured: false, masked: "", updated_at: "", updated_by: "" };
let amapStatus = { configured: false, masked: "", updated_at: "", updated_by: "" };
let memoryData = { facts: [], preferences: [], last_updated: "" };
let appBooted = false;
let needsBootstrap = false;     // 名单是否为空（为空时第一个登录者成为管理员）

// 登录表单现在处于哪个阶段：
//   "phone"     只填手机号
//   "password"  管理员，要输密码
//   "setup"     首次使用，要设置密码
let loginStage = "phone";

function el(id) {
  return document.getElementById(id);
}

function esc(s) {
  if (typeof escapeHtml === "function") return escapeHtml(s);
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(message, kind = "info", ms = 4200) {
  const box = el("toast");
  if (!box) return alert(message);
  const div = document.createElement("div");
  div.className = "toast-item " + kind;
  div.textContent = message;
  box.appendChild(div);
  setTimeout(() => div.remove(), ms);
}

function maskPhone(phone) {
  if (!phone || phone.length !== 11) return phone || "";
  return `${phone.slice(0, 3)} **** ${phone.slice(-4)}`;
}

// ---------------------------------------------------------------------------
// 视图切换
// ---------------------------------------------------------------------------
function showLoginView() {
  el("login-screen").classList.remove("hidden");
  el("app-root").classList.add("hidden");
  document.querySelectorAll(".modal-overlay").forEach((m) => m.classList.add("hidden"));
  appBooted = false;
  setLoginStage("phone");      // 回到只填手机号的状态
  refreshStartupHint();        // 名单可能已变化，重新取一次提示文案
}

function showAppView() {
  el("login-screen").classList.add("hidden");
  el("app-root").classList.remove("hidden");
}

function showLoginError(message) {
  const box = el("login-error");
  box.textContent = message;
  box.classList.remove("hidden");
}

/** 根据名单是否为空，切换登录页的提示文案。 */
async function refreshStartupHint() {
  try {
    const info = await fetch("/startup", { credentials: "same-origin" }).then((r) => r.json());
    needsBootstrap = !!info.needs_bootstrap;
  } catch (e) {
    needsBootstrap = false;
  }
  const hint = el("login-hint");
  if (needsBootstrap) {
    hint.innerHTML =
      "<strong>还没有任何访问名单</strong><br>" +
      "第一个登录的手机号将成为管理员，并需要设置一个密码。";
  } else {
    hint.textContent = "只有名单内的手机号可以进入。";
  }
  // 按钮文案由 setLoginStage 统一管理，这里不动它
}

// ---------------------------------------------------------------------------
// 登录 / 登出
// ---------------------------------------------------------------------------
/** 切换登录表单的阶段，只显示当前需要的输入框。 */
function setLoginStage(stage, options = {}) {
  loginStage = stage;
  const btn = el("login-submit");
  el("login-password-field").classList.toggle("hidden", stage !== "password");
  el("login-new-password-field").classList.toggle("hidden", stage !== "setup");
  el("login-password").value = "";
  el("login-new-password").value = "";
  el("login-new-password2").value = "";

  if (stage === "setup") {
    btn.textContent = "设置密码并进入";
  } else {
    btn.textContent = "进入";
  }
  if (options.message) el("login-hint").textContent = options.message;
  (stage === "password" ? el("login-password")
    : stage === "setup" ? el("login-new-password") : el("login-phone")).focus();
}

/** 登录是两段式的：先探手机号，后端会告诉前端下一步要什么。 */
async function submitLogin(event) {
  if (event) event.preventDefault();
  const phone = el("login-phone").value.trim();
  const btn = el("login-submit");

  el("login-error").classList.add("hidden");
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    showLoginError("请输入 11 位手机号，例如 13800138000");
    return;
  }

  let payload = { phone };
  if (loginStage === "password") {
    payload.password = el("login-password").value;
    if (!payload.password) {
      showLoginError("请输入密码。");
      return;
    }
  } else if (loginStage === "setup") {
    const pwd = el("login-new-password").value;
    if (pwd.length < 6) {
      showLoginError("密码至少需要 6 位。");
      return;
    }
    if (pwd !== el("login-new-password2").value) {
      showLoginError("两次输入的密码不一致。");
      return;
    }
    payload.new_password = pwd;
  }

  btn.disabled = true;
  btn.textContent = "验证中…";
  try {
    const result = await SessionApi.login(payload);

    if (result.status === "need_setup") {
      setLoginStage("setup", { message: result.message || "" });
      return;
    }
    if (result.status === "need_password") {
      setLoginStage("password", { message: result.message || "" });
      return;
    }

    el("login-phone").value = "";
    setLoginStage("phone");
    await startApp();
  } catch (e) {
    showLoginError(e.status === 403 ? "抱歉，您没有权限访问。" : e.message);
    if (e.authCode === "bad_password") el("login-password").select();
  } finally {
    btn.disabled = false;
    btn.textContent = loginStage === "setup" ? "设置密码并进入" : "进入";
  }
}

/** 名字输错了想重来：清掉密码阶段回到只填手机号。 */
function resetLogin() {
  setLoginStage("phone");
  el("login-error").classList.add("hidden");
  refreshStartupHint();
}

async function startApp() {
  let me = null;
  try {
    me = await SessionApi.me();
  } catch (e) {
    if (e.status === 401 || e.status === 403) {
      showLoginView();
      return;
    }
    toast("加载账号信息失败：" + e.message, "error");
  }

  currentUser = me;
  deepseekStatus = (me && me.deepseek) || deepseekStatus;
  memoryData = (me && me.memory) || memoryData;

  const isAdmin = !!(me && me.is_admin);
  el("user-chip").textContent = maskPhone((me && me.phone) || "");
  el("settings-role").textContent = isAdmin ? "管理员" : "成员";
  el("admin-btn").classList.toggle("hidden", !isAdmin);
  // 全局 Key 由管理员统一配在「访问管理」里，所以这个入口也只对管理员开放
  el("settings-btn").classList.toggle("hidden", !isAdmin);

  showAppView();
  appBooted = true;

  // 界面刚从 display:none 变可见，元素的尺寸到这里才有值，
  // 页签/分段控件的滑盖必须在这一刻之后量，否则会算成 0 宽
  if (typeof refreshGliders === "function") requestAnimationFrame(refreshGliders);

  try {
    await loadPending();
    await loadEvents();
  } catch (e) {
    toast("加载数据失败：" + e.message, "error");
  }

  // AI 服务还没配好时提醒一下：管理员自己去配，普通成员只能干等
  if (!deepseekStatus.configured) {
    toast(isAdmin
      ? "还没有配置 DeepSeek API Key，请在「访问管理」里填一次，之后所有人都用它。"
      : "管理员还没有配置 AI 服务，请稍等或联系管理员。",
      "error", 8000);
  }
}

function onSessionLost() {
  if (!appBooted) return;
  currentUser = null;
  chatContext = [];
  showLoginView();
  showLoginError("登录状态已失效，请重新登录。");
  el("login-password").value = "";
  el("login-new-password").value = "";
  el("login-new-password2").value = "";
}

async function doLogout() {
  try {
    await SessionApi.logout();
  } catch (e) {
    /* 本地照样清干净 */
  }
  chatHistory.innerHTML = "";
  chatContext = [];
  currentUser = null;
  deepseekStatus = { configured: false, masked: "", updated_at: "", updated_by: "" };
  showLoginView();
  el("login-phone").value = "";
}

// ---------------------------------------------------------------------------
// 我的记忆
// ---------------------------------------------------------------------------
async function openMemoryPanel() {
  el("memory-modal").classList.remove("hidden");
  el("memory-body").innerHTML = "<div class='muted'>加载中…</div>";
  try {
    const r = await Store.loadMemory();
    memoryData = r.memory || memoryData;
    renderMemory();
  } catch (e) {
    el("memory-body").innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
  }
}

function closeMemoryPanel() {
  el("memory-modal").classList.add("hidden");
}

function renderMemory() {
  const blocks = [
    { key: "facts", title: "事实", hint: "你明确说过的稳定信息，AI 会自动补充；不对就删掉。" },
    { key: "preferences", title: "偏好", hint: "只由你自己维护，AI 只读不写。点「+ 添加」写一条。" },
  ];

  let html = `<div class="memory-updated">最后更新：${esc(memoryData.last_updated || "（暂无）")}</div>`;
  for (const block of blocks) {
    const items = memoryData[block.key] || [];
    html += `<section class="memory-block">`;
    html += `<header><h4>${block.title}<span class="count">${items.length}</span></h4>`;
    html += `<button class="ghost" data-add="${block.key}">+ 添加</button></header>`;
    html += `<p class="hint">${esc(block.hint)}</p>`;
    if (!items.length) {
      html += `<div class="muted">（空）</div>`;
    } else {
      html += `<ul class="memory-list">`;
      items.forEach((item, i) => {
        html += `<li>`;
        html += `<span class="text">${esc(item)}</span>`;
        html += `<button class="del" data-del="${block.key}" data-index="${i}" title="删除">×</button>`;
        html += `</li>`;
      });
      html += `</ul>`;
    }
    html += `</section>`;
  }
  html += `<div class="memory-footer"><button class="danger" id="memory-clear-all">清空全部记忆</button></div>`;
  el("memory-body").innerHTML = html;

  el("memory-body").querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => deleteMemoryItem(btn.dataset.del, Number(btn.dataset.index)));
  });
  el("memory-body").querySelectorAll("[data-add]").forEach((btn) => {
    btn.addEventListener("click", () => addMemoryItem(btn.dataset.add));
  });
  const clearAll = el("memory-clear-all");
  if (clearAll) clearAll.addEventListener("click", clearAllMemory);
}

async function saveMemoryBlock(key, items) {
  try {
    const payload = {};
    payload[key] = items;
    const r = await Store.saveMemory(payload);
    memoryData = r.memory || memoryData;
    renderMemory();
    toast("已更新。", "ok", 2000);
  } catch (e) {
    toast("更新失败：" + e.message, "error", 6000);
  }
}

async function deleteMemoryItem(key, index) {
  const items = (memoryData[key] || []).slice();
  items.splice(index, 1);
  await saveMemoryBlock(key, items);
}

async function addMemoryItem(key) {
  const label = { facts: "事实", preferences: "偏好" }[key] || key;
  const value = prompt(`添加一条${label}：`);
  if (!value || !value.trim()) return;
  const items = (memoryData[key] || []).slice();
  items.push(value.trim());
  await saveMemoryBlock(key, items);
}

async function clearAllMemory() {
  if (!confirm("确定清空全部长期记忆？AI 会忘记关于你的一切。")) return;
  try {
    const r = await Store.clearMemory();
    memoryData = r.memory || { facts: [], preferences: [], last_updated: "" };
    renderMemory();
    toast("已清空全部记忆。", "ok");
  } catch (e) {
    toast("清空失败：" + e.message, "error");
  }
}

// ---------------------------------------------------------------------------
// 访问管理（仅管理员可见）
// ---------------------------------------------------------------------------
async function openAdminPanel() {
  el("admin-modal").classList.remove("hidden");
  el("admin-list").innerHTML = "<div class='muted'>加载中…</div>";
  el("admin-api-key").value = "";
  el("admin-amap-key").value = "";
  el("admin-old-pwd").value = "";
  el("admin-new-pwd").value = "";
  await Promise.all([loadWhitelist(), LoadDeepseekSetting()]);
}

function closeAdminPanel() {
  el("admin-modal").classList.add("hidden");
}

async function loadWhitelist() {
  try {
    const r = await AdminApi.list();
    renderWhitelist(r.phones || [], r.me);
  } catch (e) {
    el("admin-list").innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
  }
}

function renderWhitelist(phones, me) {
  if (!phones.length) {
    el("admin-list").innerHTML = "<div class='muted'>名单为空</div>";
    return;
  }
  let html = `<section class="memory-block"><header><h4>名单<span class="count">${phones.length}</span></h4></header>`;
  html += `<ul class="memory-list">`;
  phones.forEach((p) => {
    const isMe = p.phone === me;
    const tag = p.is_admin ? "（管理员）" : "";
    const name = p.name ? ` ${esc(p.name)}` : "";
    const last = p.last_login ? ` · 最近登录 ${esc(p.last_login)}` : "";
    html += `<li${p.is_admin ? ' class="inferred"' : ""}>`;
    html += `<span class="text">${esc(maskPhone(p.phone))}${name}${tag}<span class="muted">${last}</span></span>`;
    html += `<button class="ghost" data-toggle="${esc(p.phone)}" data-admin="${p.is_admin ? 1 : 0}">${p.is_admin ? "取消管理员" : "设为管理员"}</button>`;
    if (!isMe) {
      html += `<button class="del" data-remove="${esc(p.phone)}" title="移出名单">×</button>`;
    }
    html += `</li>`;
  });
  html += `</ul></section>`;
  el("admin-list").innerHTML = html;

  el("admin-list").querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => toggleAdmin(btn.dataset.toggle, btn.dataset.admin !== "1"));
  });
  el("admin-list").querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", () => removePhone(btn.dataset.remove));
  });
}

async function addPhone() {
  const phone = el("admin-phone").value.trim();
  const name = el("admin-name").value.trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    toast("请输入 11 位手机号。", "error");
    return;
  }
  try {
    await AdminApi.add({ phone, name, note: "", is_admin: false });
    el("admin-phone").value = "";
    el("admin-name").value = "";
    toast("已添加。", "ok");
    await loadWhitelist();
  } catch (e) {
    toast("添加失败：" + e.message, "error", 6000);
  }
}

async function removePhone(phone) {
  if (!confirm(`确定把 ${maskPhone(phone)} 移出名单？他将无法再登录。`)) return;
  try {
    await AdminApi.remove(phone);
    toast("已移出。", "ok");
    await loadWhitelist();
  } catch (e) {
    toast("移出失败：" + e.message, "error", 6000);
  }
}

async function toggleAdmin(phone, makeAdmin) {
  try {
    await AdminApi.setAdmin(phone, makeAdmin);
    toast(makeAdmin ? "已设为管理员。" : "已取消管理员。", "ok");
    await loadWhitelist();
  } catch (e) {
    toast("操作失败：" + e.message, "error", 6000);
  }
}

// ---------------------------------------------------------------------------
// 全站 DeepSeek API Key / 管理员密码
// ---------------------------------------------------------------------------
async function LoadDeepseekSetting() {
  let deepseek = { configured: false, masked: "", updated_at: "", updated_by: "" };
  let amap = { configured: false, masked: "", updated_at: "", updated_by: "" };
  try {
    const r = await AdminApi.settings();
    deepseek = (r.settings && r.settings.deepseek) || deepseek;
    amap = (r.settings && r.settings.amap) || amap;
  } catch (e) { /* 保留默认 */ }
  deepseekStatus = deepseek;
  amapStatus = amap;
  renderDeepseekState();
  renderAmapState();
  return deepseek;
}

function renderDeepseekState() {
  const s = deepseekStatus || {};
  el("admin-api-key").placeholder = s.masked || "sk-…";
  const parts = [s.configured ? `已配置：${esc(s.masked)}` : "未配置"];
  if (s.updated_at) parts.push(`更新于 ${esc(s.updated_at)}`);
  if (s.updated_by) parts.push(`由 ${esc(maskPhone(s.updated_by))} 设置`);
  el("admin-api-key-state").textContent = parts.join(" · ");
}

async function saveAdminApiKey() {
  const key = el("admin-api-key").value.trim();
  if (!key) {
    toast("请填写 API Key。", "error");
    return;
  }
  try {
    const r = await AdminApi.saveApiKey(key);
    deepseekStatus = r.deepseek || deepseekStatus;
    renderDeepseekState();
    el("admin-api-key").value = "";
    toast("已保存，之后所有成员都使用这一把 Key。", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "error", 6000);
  }
}

async function clearAdminApiKey() {
  if (!confirm("确定清空全局 API Key？清空后所有人都无法使用 AI 对话。")) return;
  try {
    const r = await AdminApi.clearApiKey();
    deepseekStatus = r.deepseek || deepseekStatus;
    renderDeepseekState();
    toast("已清空。", "ok");
  } catch (e) {
    toast("清空失败：" + e.message, "error");
  }
}

// ---- 高德 Key（只影响路况查询，不配也能正常用其他功能） ----
function renderAmapState() {
  const s = amapStatus || {};
  el("admin-amap-key").placeholder = s.masked || "32 位高德 Key";
  const parts = [s.configured ? `已配置：${esc(s.masked)}` : "未配置（路况功能不可用）"];
  if (s.updated_at) parts.push(`更新于 ${esc(s.updated_at)}`);
  if (s.updated_by) parts.push(`由 ${esc(maskPhone(s.updated_by))} 设置`);
  el("admin-amap-state").textContent = parts.join(" · ");
}

async function saveAdminAmapKey() {
  const key = el("admin-amap-key").value.trim();
  if (!key) {
    toast("请填写高德 Key。", "error");
    return;
  }
  try {
    const r = await AdminApi.saveAmapKey(key);
    amapStatus = r.amap || amapStatus;
    renderAmapState();
    el("admin-amap-key").value = "";
    toast("高德 Key 已保存，路况查询已启用。", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "error", 6000);
  }
}

async function clearAdminAmapKey() {
  if (!confirm("确定清空高德 Key？清空后无法再查询实时路况。")) return;
  try {
    const r = await AdminApi.clearAmapKey();
    amapStatus = r.amap || amapStatus;
    renderAmapState();
    toast("已清空。", "ok");
  } catch (e) {
    toast("清空失败：" + e.message, "error");
  }
}

async function changeAdminPassword() {
  const oldPwd = el("admin-old-pwd").value;
  const newPwd = el("admin-new-pwd").value;
  if (!oldPwd || !newPwd) {
    toast("请填写当前密码和新密码。", "error");
    return;
  }
  if (newPwd.length < 6) {
    toast("新密码至少需要 6 位。", "error");
    return;
  }
  try {
    await AdminApi.changePassword(oldPwd, newPwd);
    el("admin-old-pwd").value = "";
    el("admin-new-pwd").value = "";
    toast("密码已修改，下次登录请用新密码。", "ok");
  } catch (e) {
    toast("修改失败：" + e.message, "error", 6000);
  }
}

// ---------------------------------------------------------------------------
// 设置弹窗
// 头部原来那 6 个按钮（思考过程 / 我的记忆 / .ics / 访问管理 / API Key / 退出）
// 全部收进这里，头部只留一个齿轮。它本身不承载逻辑，只做入口分发。
// ---------------------------------------------------------------------------
function openSettings() {
  el("settings-modal").classList.remove("hidden");
}

function closeSettings() {
  el("settings-modal").classList.add("hidden");
}

// ---------------------------------------------------------------------------
// 初始化与事件绑定
// ---------------------------------------------------------------------------
async function initSession() {
  el("login-form").addEventListener("submit", submitLogin);
  el("login-phone").addEventListener("input", (e) => {
    e.target.value = e.target.value.replace(/\D/g, "").slice(0, 11);
    // 已经在输密码/设密码阶段时又去改手机号 -> 回到第一步重来
    if (loginStage !== "phone") resetLogin();
  });
  el("logout-btn").addEventListener("click", doLogout);
  // 「API Key 设置」现在指向同一个管理面板：全局 Key 就在那里配
  el("settings-btn").addEventListener("click", openAdminPanel);

  // 齿轮（桌面在聊天头部、手机在顶部导航，两处共用同一个 class）
  document.querySelectorAll(".js-open-settings").forEach((btn) => {
    btn.addEventListener("click", openSettings);
  });
  el("settings-close").addEventListener("click", closeSettings);
  el("settings-modal").addEventListener("click", (e) => {
    if (e.target === el("settings-modal")) {
      closeSettings();
      return;
    }
    // 点了任意一行功能，顺手把设置收起来，避免弹窗叠弹窗
    const row = e.target.closest(".settings-row");
    if (row && !row.classList.contains("static")) setTimeout(closeSettings, 0);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el("settings-modal").classList.contains("hidden")) {
      closeSettings();
    }
  });

  el("admin-api-key-save").addEventListener("click", saveAdminApiKey);
  el("admin-api-key-clear").addEventListener("click", clearAdminApiKey);
  el("admin-api-key").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveAdminApiKey(); }
  });

  el("admin-amap-save").addEventListener("click", saveAdminAmapKey);
  el("admin-amap-clear").addEventListener("click", clearAdminAmapKey);
  el("admin-amap-key").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveAdminAmapKey(); }
  });
  el("admin-pwd-save").addEventListener("click", changeAdminPassword);

  el("memory-btn").addEventListener("click", openMemoryPanel);
  el("memory-close").addEventListener("click", closeMemoryPanel);
  el("memory-modal").addEventListener("click", (e) => {
    if (e.target === el("memory-modal")) closeMemoryPanel();
  });

  el("admin-btn").addEventListener("click", openAdminPanel);
  el("admin-close").addEventListener("click", closeAdminPanel);
  el("admin-add").addEventListener("click", addPhone);
  el("admin-phone").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addPhone(); }
  });
  el("admin-phone").addEventListener("input", (e) => {
    e.target.value = e.target.value.replace(/\D/g, "").slice(0, 11);
  });
  el("admin-modal").addEventListener("click", (e) => {
    if (e.target === el("admin-modal")) closeAdminPanel();
  });

  try {
    const info = await SessionApi.info();
    if (info && info.authenticated) {
      await startApp();
      return;
    }
  } catch (e) { /* 未登录是正常情况 */ }
  showLoginView();
}

document.addEventListener("DOMContentLoaded", initSession);
