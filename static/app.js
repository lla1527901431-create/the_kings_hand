// ============================================================================
// 主界面逻辑：聊天、任务列表、周视图日历、事件编辑
//
// 数据访问统一走 Store（见 store.js），本文件不直接调用 fetch。
// ============================================================================

// ==================== DOM 引用 ====================
const chatHistory = document.getElementById("chat-history");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const showSteps = document.getElementById("show-steps");
const resetBtn = document.getElementById("reset-btn");
const exportIcsBtn = document.getElementById("export-ics-btn");
const taskList = document.getElementById("task-list");
const confirmAllBtn = document.getElementById("confirm-all-btn");
const tabButtons = document.querySelectorAll(".tab-btn");
const calendarDays = document.querySelector(".calendar-days");
const calendarBody = document.getElementById("calendar-body");
const calendarRange = document.getElementById("calendar-range");

// Next Week
const bottomHandle = document.getElementById("bottom-handle");
const nextWeekOverlay = document.getElementById("next-week-overlay");
const overlayTitle = document.getElementById("overlay-title");
const overlayClose = document.getElementById("overlay-close");
const overlayDaysHeader = document.getElementById("overlay-days-header");
const overlayBody = document.getElementById("overlay-body");

// Modal
const eventModal = document.getElementById("event-modal");
const modalDetails = document.getElementById("modal-details");
const modalAlter = document.getElementById("modal-alter");
const viewTitle = document.getElementById("view-title");
const viewTime = document.getElementById("view-time");
const viewLocation = document.getElementById("view-location");
const viewNote = document.getElementById("view-note");
const editTitle = document.getElementById("edit-title");
const editStart = document.getElementById("edit-start");
const editEnd = document.getElementById("edit-end");
const editLocation = document.getElementById("edit-location");
const editNote = document.getElementById("edit-note");
const modalSaveBtn = document.getElementById("modal-save");
const modalDeleteBtn = document.getElementById("modal-delete");
const modalEditBtn = document.getElementById("modal-edit-btn");
const modalCloseBtn = document.getElementById("modal-close");
const modalCancelBtn = document.getElementById("modal-cancel");

// ==================== 状态 ====================
let pendingData = { confirmed: [], planned: [] };
let activeTab = "confirmed";
let confirmedEvents = [];
let editingEventId = null;
let chatContext = [];        // 本地保留的对话上下文，随 /chat 一起发送
let streaming = false;       // 正在流式输出：期间锁住输入，避免并发提问

const EVENT_COLORS = [
  "var(--event-1)", "var(--event-2)", "var(--event-3)",
  "var(--event-4)", "var(--event-5)",
];

const HOUR_HEIGHT = parseFloat(
  getComputedStyle(document.documentElement).getPropertyValue("--hour-height")
);

// ==================== 工具函数 ====================
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function getMonday(date) {
  const d = new Date(date);
  const day = d.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  return d;
}

function parseDateTime(str) {
  if (!str) return null;
  const m = String(str).match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
}

function formatDateTime(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatDateOnly(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ==================== 聊天 ====================
function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  chatHistory.appendChild(div);
  scrollChat();
}

function scrollChat() {
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

// ---- 等待反馈：3×3 像素波加载器 ------------------------------------------
// 波前按 chevron 排：中间行先亮，上下两行各滞后一格。
// 单格周期 650ms，而整条波前扫完要 920ms —— 所以画面上始终有两道波在追。
const PIXEL_WAVE = [90, 180, 270, 0, 90, 180, 90, 180, 270];

function createLoader(label) {
  const row = document.createElement("div");
  row.className = "load-row";
  row.setAttribute("role", "status");

  const grid = document.createElement("span");
  grid.className = "pixel-grid";
  grid.setAttribute("aria-hidden", "true");
  PIXEL_WAVE.forEach((delay) => {
    const px = document.createElement("span");
    px.className = "px";
    px.style.animationDelay = delay + "ms";
    grid.appendChild(px);
  });

  const text = document.createElement("span");
  text.className = "shimmer-label";
  text.textContent = label || "正在思考";

  const timer = document.createElement("span");
  timer.className = "elapsed";
  timer.textContent = "0.0s";

  row.append(grid, text, timer);

  // 计时器：0.1s 粒度；超过一分钟换成 1m 05.2s，和"还要等多久"的直觉一致
  const started = Date.now();
  const tick = setInterval(() => {
    const total = (Date.now() - started) / 1000;
    timer.textContent = total < 60
      ? total.toFixed(1) + "s"
      : `${Math.floor(total / 60)}m ${(total % 60).toFixed(1)}s`;
  }, 100);

  return {
    el: row,
    setLabel(value) { text.textContent = value; },
    stop() { clearInterval(tick); },
  };
}

// ---- 工具调用 → 回答里的引用 chip ----------------------------------------
// 只给"确实查了东西"的工具打标签；emit_tasks / resolve_date 这类内部步骤
// 已经进了「推理过程」，不必在正文里再打扰一次。
const TOOL_CHIPS = {
  get_weather_forecast: { text: "查天气", icon: "cloud" },
  plan_traffic: { text: "算路程", icon: "route" },
  search_place: { text: "找地点", icon: "pin" },
  get_current_location: { text: "定位", icon: "pin" },
  search_history: { text: "查历史日程", icon: "clock" },
  update_pending: { text: "改待办", icon: "check" },
};

const CHIP_ICONS = {
  cloud: '<path d="M17.5 19a4.5 4.5 0 0 0 .5-8.96A6 6 0 0 0 6.3 9.5 4.5 4.5 0 0 0 6.5 19h11Z"></path>',
  route: '<circle cx="6" cy="19" r="2.5"></circle><circle cx="18" cy="5" r="2.5"></circle><path d="M8.5 19h5a4 4 0 0 0 0-8h-3a4 4 0 0 1 0-8h5"></path>',
  pin: '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"></path><circle cx="12" cy="10" r="3"></circle>',
  clock: '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
  check: '<path d="M20 6 9 17l-5-5"></path>',
};

/** 工具 chip 单独一行，挂在气泡和推理过程之间；同一工具只留一个 */
function appendToolChip(bubble, tool) {
  const meta = TOOL_CHIPS[tool];
  if (!meta || !bubble) return;

  // 找现有的 .msg-tools（它只会出现在气泡之后、其它附属块之前）
  let row = bubble.nextElementSibling;
  while (row && row.classList.contains("msg-brief")) row = row.nextElementSibling;
  if (!row || !row.classList.contains("msg-tools")) {
    row = document.createElement("div");
    row.className = "msg-tools";
    insertAfterBubble(bubble, row);
  }
  if (Array.from(row.children).some((c) => c.dataset.tool === tool)) return;

  const chip = document.createElement("span");
  chip.className = "tool-chip";
  chip.dataset.tool = tool;
  chip.title = `这一句参考了 ${tool} 的结果`;
  chip.innerHTML =
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${CHIP_ICONS[meta.icon] || ""}</svg><span></span>`;
  chip.lastElementChild.textContent = meta.text;
  row.appendChild(chip);
}

const ICON_COPY =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<rect x="9" y="9" width="12" height="12" rx="2.5"></rect>' +
  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';

const ICON_RETRY =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"></path></svg>';

// 一条回答的附属块固定按这个顺序排：
//   气泡 → 工具 chip → 事实小字 → 操作行 → 推理过程
// 新增/移除块时都走 insertAfterBubble，别的位置不用关心。
const GROUP_CLASSES = ["msg-tools", "msg-brief", "msg-actions", "steps"];

/** 按 GROUP_CLASSES 的顺序，把 node 插到气泡后面正确的位置 */
function insertAfterBubble(bubble, node) {
  const order = GROUP_CLASSES.indexOf(node.classList[0]);
  let anchor = bubble;
  let sib = anchor.nextElementSibling;
  while (sib) {
    const sibOrder = GROUP_CLASSES.indexOf(sib.classList[0]);
    if (sibOrder === -1 || sibOrder >= order) break;
    anchor = sib;
    sib = sib.nextElementSibling;
  }
  anchor.insertAdjacentElement("afterend", node);
}

/**
 * 复制文本。
 * navigator.clipboard 只在安全上下文（https / localhost）里可用，
 * 局域网用 http 打开时它是 undefined，所以要留一条 execCommand 的老路。
 */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

/** 回答说完后的动作行：复制 / 重新生成。只留图标，文案走 title / aria-label */
function renderActions(bubble, outputText, prompt) {
  if (!outputText) return;

  const row = document.createElement("div");
  row.className = "msg-actions";

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "msg-act";
  copy.title = "复制";
  copy.setAttribute("aria-label", "复制");
  copy.innerHTML = ICON_COPY;
  copy.addEventListener("click", async () => {
    let ok = false;
    try { ok = await copyText(outputText); } catch (e) { ok = false; }
    if (typeof toast === "function") {
      toast(ok ? "已复制" : "复制失败", ok ? "ok" : "error", 1600);
    }
  });

  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "msg-act";
  retry.title = "重新生成";
  retry.setAttribute("aria-label", "重新生成");
  retry.innerHTML = ICON_RETRY;
  retry.addEventListener("click", () => {
    if (streaming) return;
    revertToDraft(bubble, prompt);
  });

  row.append(copy, retry);
  insertAfterBubble(bubble, row);
}

/**
 * 「重新生成」= 回退到发送前：
 * 删掉这一问一答（气泡、工具 chip、事实小字、动作行、推理过程），
 * 把原来的输入放回输入框，让用户改完再发 —— 而不是偷偷重发一遍。
 */
function revertToDraft(bubble, prompt) {
  if (!bubble || !bubble.isConnected) return;

  const userMsg = bubble.previousElementSibling;
  const group = [bubble];
  let sib = bubble.nextElementSibling;
  while (sib && GROUP_CLASSES.includes(sib.classList[0])) {
    group.push(sib);
    sib = sib.nextElementSibling;
  }
  group.forEach((node) => node.remove());
  if (userMsg && userMsg.classList.contains("user")) userMsg.remove();

  // 对话上下文也回退这一轮，避免"删了还记着"
  if (chatContext.length && chatContext[chatContext.length - 1].role === "assistant") chatContext.pop();
  if (chatContext.length && chatContext[chatContext.length - 1].role === "user") chatContext.pop();

  chatInput.value = prompt || "";
  chatInput.focus();
  if (typeof autoGrow === "function") autoGrow();
  scrollChat();
  if (typeof toast === "function") toast("已回到发送前，修改后可直接再发", "info", 2200);
}


function appendSteps(steps) {
  if (!showSteps.checked) return;
  if (!steps || steps.length === 0) return;
  // 默认折叠：工具调用日志很长，铺开会把正事顶出屏幕
  const wrap = document.createElement("details");
  wrap.className = "steps";

  const summary = document.createElement("summary");
  summary.textContent = `推理过程 · ${steps.length} 步`;
  wrap.appendChild(summary);

  const body = document.createElement("div");
  body.className = "steps-body";
  body.textContent = steps
    .map((s) => `↳ 调用 ${s.tool}，参数 ${JSON.stringify(s.args)}`)
    .join("\n");
  wrap.appendChild(body);

  chatHistory.appendChild(wrap);
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

function handleMemoryCommand(text) {
  if (!/^\/memory$/i.test(text.trim())) return false;
  appendMessage("user", text);
  chatInput.value = "";
  appendMessage("assistant", "已打开「我的记忆」面板，你可以查看和修改我记住的内容。");
  if (typeof openMemoryPanel === "function") openMemoryPanel();
  return true;
}

// ---- 流式渲染用的小工具 ----
function appendStreamBubble() {
  const div = document.createElement("div");
  div.className = "msg assistant streaming";
  chatHistory.appendChild(div);
  scrollChat();
  return div;
}

/**
 * 光标只跟在"正在输出的文字"后面。
 * 气泡里没字的时候（加载中、或一段思考刚被挪走）加 caret-off 把它关掉，
 * 免得空气泡里孤零零闪一根紫条。样式见 style.css 的 .caret-off 规则。
 */
function syncCaret(bubble) {
  let hasText = false;
  bubble.querySelectorAll(".tok").forEach((node) => {
    if (!hasText && node.textContent.trim()) hasText = true;
  });
  bubble.classList.toggle("caret-off", !hasText);
}

/** 把一段增量渲染成"从模糊里解析出来"的 span（见 theme.css 的 kh-word-in） */
function appendToken(bubble, text) {
  const span = document.createElement("span");
  span.className = "tok";
  span.textContent = text;
  bubble.appendChild(span);
  syncCaret(bubble);
}

function createReasoningBlock() {
  const wrap = document.createElement("details");
  wrap.className = "steps";
  wrap.open = true;                 // 流式期间保持展开，让用户看到实时进展
  const summary = document.createElement("summary");
  summary.textContent = "推理过程";
  const body = document.createElement("div");
  body.className = "steps-body";
  wrap.appendChild(summary);
  wrap.appendChild(body);
  chatHistory.appendChild(wrap);
  return { wrap, summary, body };
}

function appendReasonLine(block, text) {
  if (!block) return;
  const line = document.createElement("div");
  line.className = "reason-line";
  line.textContent = text;
  block.body.appendChild(line);
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

/**
 * 收到 segment 事件：说明上一个模型轮次结束了。
 * 如果它后面接着调工具，那这段文字是"思考"，挪进推理过程区；
 * 否则（最后一轮）它就是给用户看的回答，留在气泡里。
 * 关掉「显示推理过程」时直接丢弃，避免思考内容混进回答。
 *
 * 注意：气泡里除了文字还有工具引用 chip，所以不能直接读 textContent
 * （那会把 chip 上的"查天气"也算进推理过程），只取 .tok 的文本。
 */
function flushSegment(bubble, block) {
  const text = Array.from(bubble.querySelectorAll(".tok"))
    .map((node) => node.textContent)
    .join("")
    .trim();
  bubble.textContent = "";
  syncCaret(bubble);
  if (text) appendReasonLine(block, text);
}

function setStreaming(on) {
  streaming = on;
  chatInput.disabled = on;
  sendBtn.disabled = on;
  chatInput.placeholder = on ? "正在回复…" : "输入任务，例如：明天下午三点开会";
}

// 位置：浏览器定位。策略是"能用就用，不能用也不拖慢发消息"：
//   * 有坐标时缓存 5 分钟；失败只缓存 30 秒，避免一次失败把后续都拖死
//   * 发消息时最多等 1.5 秒，等不到就先发，后台继续拿，下一条用上
//   * 拿不到坐标时服务端用客户端 IP 做城市级兜底
let cachedLocation = { at: 0, value: null };
let pendingGeo = null;

function requestGeolocation() {
  if (pendingGeo) return pendingGeo;
  pendingGeo = new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const value = {
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: Math.round(pos.coords.accuracy || 0),
          source: "browser",
        };
        console.info("浏览器定位成功：", value);
        cachedLocation = { at: Date.now(), value };
        resolve(value);
      },
      (err) => {
        console.warn("浏览器定位失败：", err && err.code, err && err.message);
        cachedLocation = { at: Date.now(), value: null };
        resolve(null);
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
    );
  }).finally(() => { pendingGeo = null; });
  return pendingGeo;
}

async function browserLocation() {
  const now = Date.now();
  const ttl = cachedLocation.value ? 5 * 60 * 1000 : 30 * 1000;
  if (now - cachedLocation.at < ttl) return cachedLocation.value;

  requestGeolocation();                        // 不 await：先让它在后台跑
  const timeout = new Promise((r) => setTimeout(() => r("timeout"), 1500));
  const result = await Promise.race([pendingGeo || Promise.resolve(null), timeout]);
  return result === "timeout" ? cachedLocation.value : result;
}

// 消息左下角的"事实小字"：只放工具返回的结构化字段，模型不重复念。
// 缺哪个字段就省略哪段，整块没内容就不渲染。
function renderBrief(bubble, brief) {
  if (!brief || !bubble) return;
  const lines = [];

  const w = brief.weather;
  if (w) {
    const parts = [w.date, w.city, w.desc, w.temp];
    if (w.precip !== undefined && w.precip !== null) parts.push(`降水概率 ${w.precip}%`);
    const line = parts.filter(Boolean).join("  ");
    if (line) lines.push(line);
  }

  const t = brief.traffic;
  if (t) {
    const parts = [];
    if (t.distance_km) parts.push(`距目的地约 ${t.distance_km} 公里`);
    if (t.duration_min) parts.push(`预计 ${t.duration_min} 分钟`);
    if (t.leave_by) parts.push(`建议 ${t.leave_by} 前出发`);
    parts.push("路程预估参考当前时间");
    lines.push(parts.join("  "));
  }

  if (!lines.length) return;
  const div = document.createElement("div");
  div.className = "msg-brief";
  div.textContent = lines.join("\n");
  insertAfterBubble(bubble, div);
}

/**
 * 发一轮对话。
 */
async function send() {
  const text = chatInput.value.trim();
  if (!text || streaming) return;
  if (handleMemoryCommand(text)) return;

  appendMessage("user", text);
  chatInput.value = "";

  const bubble = appendStreamBubble();
  bubble.textContent = "";
  bubble.classList.add("streaming", "loading");
  syncCaret(bubble);

  // 首个 token 到达前，气泡里只有这个像素波加载器
  const loader = createLoader();
  bubble.appendChild(loader.el);

  const reasoning = showSteps.checked ? createReasoningBlock() : null;
  setStreaming(true);

  // 只累积"当前这一段"的文字；下一段开始时会被清空
  let output = "";
  let waiting = true;

  const dropLoader = () => {
    if (!loader.el.isConnected) return;
    loader.stop();
    loader.el.remove();
    bubble.classList.remove("loading");
    waiting = false;
  };

  try {
    const location = await browserLocation();
    await Store.chatStream(text, {
      events: confirmedEvents,
      pending: pendingData,
      history: chatContext.slice(-10),
      location,
    }, async (ev) => {
      if (ev.type === "token") {
        if (waiting) dropLoader();
        output += ev.text;
        appendToken(bubble, ev.text);
        scrollChat();

      } else if (ev.type === "segment") {
        flushSegment(bubble, reasoning);
        output = "";

      } else if (ev.type === "step") {
        if (reasoning) reasoning.summary.textContent = `推理过程 · 正在调用 ${ev.tool}…`;
        appendReasonLine(reasoning,
          `↳ 调用 ${ev.tool}，参数 ${JSON.stringify(ev.args)}`);
        // 查了东西就在正文里留一个小标签，让用户知道这句话的依据从哪来
        appendToolChip(bubble, ev.tool);

      } else if (ev.type === "tasks") {
        // 任务一识别出来就进待确认列表，不用等回答说完
        if (ev.tasks && ev.tasks.length > 0) await addTasks(ev.tasks);

      } else if (ev.type === "done") {
        dropLoader();
        bubble.classList.remove("streaming", "loading");
        if (!output && ev.output) {
          bubble.textContent = ev.output;
          output = ev.output;
        }
        // agent 可能用 update_pending_task 改过备注/时间，收尾时刷新一次列表
        try { await loadPending(); } catch (e) { console.error("刷新待办失败", e); }
        renderBrief(bubble, ev.brief);
        renderActions(bubble, output, text);
        if (reasoning) {
          reasoning.summary.textContent =
            `推理过程 · ${(ev.steps || []).length} 步`;
          if (!reasoning.body.children.length) reasoning.wrap.remove();
        }
        if (output) {
          chatContext.push({ role: "user", content: text });
          chatContext.push({ role: "assistant", content: output });
          if (chatContext.length > 10) chatContext = chatContext.slice(-10);
        }

      } else if (ev.type === "error") {
        dropLoader();
        bubble.classList.remove("streaming", "loading");
        bubble.textContent = "出错了：" + ev.message;
        output = "";
      }
    });

    // 流正常结束但没收到 done（例如连接被提前断开）时的兜底
    dropLoader();
    if (!bubble.textContent && !output) {
      bubble.classList.remove("streaming");
      bubble.textContent = "（没有收到回复）";
    }
  } catch (e) {
    dropLoader();
    bubble.classList.remove("streaming", "loading");
    bubble.textContent = "出错了：" + e.message;
  } finally {
    dropLoader();
    setStreaming(false);
    scrollChat();
  }
}

// ==================== 任务列表 ====================
function currentTasks() {
  return activeTab === "confirmed" ? pendingData.confirmed : pendingData.planned;
}

function renderTasks() {
  taskList.innerHTML = "";
  confirmAllBtn.style.display = activeTab === "confirmed" ? "" : "none";

  const tasks = currentTasks();
  if (tasks.length === 0) {
    const empty = document.createElement("div");
    empty.className = "task-empty";
    empty.textContent = activeTab === "confirmed" ? "暂无待确认任务" : "暂无待计划任务";
    taskList.appendChild(empty);
    return;
  }

  tasks.forEach((task) => {
    const card = document.createElement("div");
    card.className = "task-card";
    const timeText = task.start
      ? `${task.start}${task.end ? " ~ " + task.end : ""}`
      : "待计划";

    const actionsHtml = activeTab === "confirmed"
      ? `<button class="confirm">确认</button><button class="modify">修改</button><button class="delete">删除</button>`
      : `<button class="modify">修改</button><button class="delete">删除</button>`;

    const locHtml = task.location ? `<div class="task-location">${escapeHtml(task.location)}</div>` : "";
    const noteHtml = task.note ? `<div class="task-note">${escapeHtml(task.note)}</div>` : "";

    card.innerHTML = `
      <div class="task-title">${escapeHtml(task.title)}</div>
      <div class="task-time">${escapeHtml(timeText)}</div>
      ${locHtml}
      ${noteHtml}
      <div class="task-actions">${actionsHtml}</div>
    `;

    const confirmBtn = card.querySelector(".confirm");
    if (confirmBtn) confirmBtn.addEventListener("click", () => confirmTask(task.id));
    card.querySelector(".modify").addEventListener("click", () => modifyTask(task.id));
    card.querySelector(".delete").addEventListener("click", () => deleteTask(task.id));
    taskList.appendChild(card);
  });
}

async function addTasks(tasks) {
  const failed = [];
  for (const t of tasks) {
    if (!t.id) t.id = "task-" + Math.random().toString(36).slice(2, 8);
    try {
      await Store.addPending(t);
    } catch (e) {
      // 入库失败必须让用户看见：模型在对话里已经说过"已添加"，
      // 这里再静默失败，就会出现"说加了、列表里没有"的错觉。
      console.error("任务入库失败", t, e);
      failed.push(t.title || t.id);
    }
  }
  try {
    await loadPending();
  } catch (e) {
    console.error("刷新待办列表失败", e);
  }
  if (failed.length) {
    toast(`有 ${failed.length} 个任务没能保存：${failed.join("、")}`, "error", 8000);
  }
}

async function deleteTask(id) {
  await Store.deletePending(id);
  await loadPending();
}

async function modifyTask(id) {
  const task =
    pendingData.confirmed.find((t) => t.id === id) ||
    pendingData.planned.find((t) => t.id === id);
  if (!task) return;

  const newTitle = prompt("修改任务标题：", task.title);
  if (newTitle === null) return;
  task.title = newTitle.trim() || task.title;

  const newStart = prompt("修改开始时间（YYYY-MM-DD HH:MM，留空表示待计划）：", task.start || "");
  if (newStart === null) return;
  task.start = newStart.trim() || null;

  const newEnd = prompt("修改结束时间（YYYY-MM-DD HH:MM）：", task.end || "");
  if (newEnd === null) return;
  task.end = newEnd.trim() || null;

  const newLocation = prompt("修改地点（留空表示无）：", task.location || "");
  if (newLocation === null) return;
  task.location = newLocation.trim() || "";

  const newNote = prompt("修改备注（留空表示无）：", task.note || "");
  if (newNote === null) return;
  task.note = newNote.trim() || "";

  await Store.updatePending({
    id: task.id, title: task.title, start: task.start, end: task.end,
    location: task.location, note: task.note,
  });
  await loadPending();
}

async function confirmTask(id) {
  const task =
    pendingData.confirmed.find((t) => t.id === id) ||
    pendingData.planned.find((t) => t.id === id);
  if (!task) return;

  // 只有开始时间就补一小时（后端也会兜底，这里让界面立刻一致）
  if (task.start && !task.end) {
    const start = parseDateTime(task.start);
    if (start) task.end = formatDateTime(new Date(start.getTime() + 3600 * 1000));
  }

  await Store.updatePending({
    id: task.id, title: task.title, start: task.start, end: task.end,
    location: task.location, note: task.note,
  });

  try {
    await Store.confirmPending(id);
  } catch (e) {
    toast("确认失败：" + e.message, "error");
    return;
  }
  await loadPending();
  await loadEvents();
}

async function confirmAll() {
  for (const t of pendingData.confirmed.map((x) => x.id)) {
    await confirmTask(t);
  }
}

// ==================== 数据加载 ====================
async function loadPending() {
  try {
    pendingData = await Store.loadPending();
    renderTasks();
  } catch (e) {
    console.error("加载待办失败：", e);
  }
}

async function loadEvents() {
  try {
    confirmedEvents = await Store.listEvents();
    renderCalendar();
    renderNextWeekOverlay();
  } catch (e) {
    console.error("加载事件失败：", e);
  }
}

// ==================== 日历渲染 ====================
// 今天所在列下标（0 = 周一），今天不在本周时为 -1
let todayColIndex = -1;
// 当前时间线的纵坐标（px），「今天」按钮和初次定位都用它
let nowLineTop = -1;

function renderCalendar() {
  const monday = getMonday(new Date());
  const now = new Date();
  const todayMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const todayIdx = Math.round((todayMidnight - monday) / 86400000);
  todayColIndex = todayIdx >= 0 && todayIdx <= 6 ? todayIdx : -1;

  // ---- 表头：星期在上、日期在下，居中；今天加胶囊 ----
  calendarDays.innerHTML = "<div></div>";
  const wdNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  for (let i = 0; i < 7; i++) {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    const div = document.createElement("div");
    div.innerHTML =
      `<div class="day-cell${i === todayColIndex ? " today" : ""}">` +
      `<span class="dw">${wdNames[i]}</span>` +
      `<span class="dn">${d.getDate()}</span>` +
      `</div>`;
    calendarDays.appendChild(div);
  }

  // ---- 头部日期范围 ----
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  calendarRange.textContent =
    `${monday.getFullYear()}年${monday.getMonth() + 1}月${monday.getDate()}日` +
    ` – ${sunday.getMonth() + 1}月${sunday.getDate()}日`;

  calendarBody.innerHTML = "";

  const timeCol = document.createElement("div");
  timeCol.className = "time-col";
  for (let h = 0; h < 24; h++) {
    const slot = document.createElement("div");
    slot.className = "time-slot";
    slot.textContent = String(h).padStart(2, "0") + ":00";
    timeCol.appendChild(slot);
  }
  calendarBody.appendChild(timeCol);

  const dayCols = [];
  for (let i = 0; i < 7; i++) {
    const col = document.createElement("div");
    col.className = i === todayColIndex ? "day-col today-col" : "day-col";
    for (let h = 0; h < 24; h++) {
      const slot = document.createElement("div");
      slot.className = "hour-slot";
      col.appendChild(slot);
    }
    calendarBody.appendChild(col);
    dayCols.push(col);
  }

  confirmedEvents.forEach((evt, idx) => {
    const start = parseDateTime(evt.start);
    const end = parseDateTime(evt.end) || start;
    if (!start) return;

    const dayIndex = Math.floor((start - monday) / (1000 * 60 * 60 * 24));
    if (dayIndex < 0 || dayIndex > 6) return;

    const startMin = start.getHours() * 60 + start.getMinutes();
    const endMin = end.getHours() * 60 + end.getMinutes();
    const top = (startMin / 60) * HOUR_HEIGHT;
    const height = Math.max(((endMin - startMin) / 60) * HOUR_HEIGHT, 24);

    const card = document.createElement("div");
    card.className = "event-card";
    card.style.top = top + "px";
    card.style.height = height + "px";
    card.style.cursor = "pointer";

    const title = document.createElement("div");
    title.className = "ev-title";
    title.textContent = evt.title;
    card.appendChild(title);

    // 卡片够高才补第二行（时间 + 地点）；半小时那种矮块塞两行会糊成一坨
    if (height >= 46) {
      const hhmm = (v) => String(v).slice(11, 16);
      const span = evt.end ? `${hhmm(evt.start)} – ${hhmm(evt.end)}` : hhmm(evt.start);
      const sub = document.createElement("div");
      sub.className = "ev-sub";
      sub.textContent = evt.location ? `${span} · ${evt.location}` : span;
      card.appendChild(sub);
    }

    card.title = evt.location ? `${evt.title} · ${evt.location}` : evt.title;
    card.addEventListener("click", (e) => {
      e.stopPropagation();
      openEventModal(evt.id);
    });
    dayCols[dayIndex].appendChild(card);
  });

  // ---- 当前时间指示线 ----
  nowLineTop = -1;
  if (todayColIndex >= 0) {
    const minutes = now.getHours() * 60 + now.getMinutes();
    const lineTop = (minutes / 60) * HOUR_HEIGHT;
    const line = document.createElement("div");
    line.className = "now-line";
    line.style.top = lineTop + "px";
    const tip = document.createElement("span");
    tip.textContent = formatDateTime(now).slice(11, 16);
    line.appendChild(tip);
    calendarBody.appendChild(line);
    nowLineTop = lineTop;
  }

  scrollToNow();
}

// 把当前时间线滚到视口中间；今天不在本周就回落到早上 7 点
function scrollToNow() {
  const fallback = 7 * HOUR_HEIGHT;
  if (nowLineTop < 0) {
    calendarBody.scrollTop = fallback;
    return;
  }
  const viewHeight = calendarBody.clientHeight || 600;
  calendarBody.scrollTop = Math.max(0, nowLineTop - viewHeight / 2);
}

// ==================== Next Week 覆盖层 ====================
function renderNextWeekOverlay() {
  const monday = getMonday(new Date());
  const nextMonday = new Date(monday);
  nextMonday.setDate(monday.getDate() + 7);
  const nextSunday = new Date(nextMonday);
  nextSunday.setDate(nextMonday.getDate() + 6);

  overlayTitle.textContent = `Next Week (${nextMonday.getMonth() + 1}/${nextMonday.getDate()} - ${nextSunday.getMonth() + 1}/${nextSunday.getDate()})`;

  const wdNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  overlayDaysHeader.innerHTML = "<div class='overlay-spacer'></div>";
  for (let i = 0; i < 7; i++) {
    const d = new Date(nextMonday);
    d.setDate(nextMonday.getDate() + i);
    const div = document.createElement("div");
    div.textContent = `${wdNames[i]} ${d.getMonth() + 1}/${d.getDate()}`;
    overlayDaysHeader.appendChild(div);
  }

  overlayBody.innerHTML = "<div class='overlay-spacer'></div>";
  for (let i = 0; i < 7; i++) {
    const dayDate = new Date(nextMonday);
    dayDate.setDate(nextMonday.getDate() + i);
    const dayStr = formatDateOnly(dayDate);

    const col = document.createElement("div");
    col.className = "overlay-day-col";

    const dayEvents = confirmedEvents
      .filter((e) => e.start && e.start.startsWith(dayStr))
      .sort((a, b) => (a.start || "").localeCompare(b.start || ""));

    const morning = [], afternoon = [];
    for (const e of dayEvents) {
      (parseInt(e.start.slice(11, 13), 10) < 12 ? morning : afternoon).push(e);
    }

    if (morning.length) {
      const title = document.createElement("div");
      title.className = "overlay-section-title";
      title.textContent = "上午";
      col.appendChild(title);
      morning.forEach((e) => col.appendChild(makeOverlayCard(e, "morning")));
    }
    if (afternoon.length) {
      const title = document.createElement("div");
      title.className = "overlay-section-title";
      title.textContent = "下午";
      col.appendChild(title);
      afternoon.forEach((e) => col.appendChild(makeOverlayCard(e, "afternoon")));
    }
    overlayBody.appendChild(col);
  }
}

function makeOverlayCard(evt, period) {
  const card = document.createElement("div");
  card.className = "overlay-card " + period;
  card.textContent = evt.title;
  card.title = evt.title;
  card.addEventListener("click", (e) => {
    e.stopPropagation();
    openEventModal(evt.id);
  });
  return card;
}

function openNextWeekOverlay() {
  renderNextWeekOverlay();
  nextWeekOverlay.classList.remove("hidden");
}

function closeNextWeekOverlay() {
  nextWeekOverlay.classList.add("hidden");
}

// ==================== Modal ====================
function openEventModal(eventId) {
  const evt = confirmedEvents.find((e) => e.id === eventId);
  if (!evt) return;

  editingEventId = eventId;
  viewTitle.textContent = evt.title || "(无标题)";
  viewTime.textContent = evt.end ? `${evt.start} ~ ${evt.end}` : (evt.start || "");
  viewLocation.textContent = evt.location || "";
  viewNote.textContent = evt.note || "";

  switchToViewMode();
  eventModal.classList.remove("hidden");
}

function switchToViewMode() {
  modalDetails.classList.remove("hidden");
  modalAlter.classList.add("hidden");
}

function switchToEditMode() {
  const evt = confirmedEvents.find((e) => e.id === editingEventId);
  if (!evt) return;
  editTitle.value = evt.title || "";
  editStart.value = evt.start || "";
  editEnd.value = evt.end || "";
  editLocation.value = evt.location || "";
  editNote.value = evt.note || "";
  modalDetails.classList.add("hidden");
  modalAlter.classList.remove("hidden");
}

function closeEventModal() {
  eventModal.classList.add("hidden");
  editingEventId = null;
}

async function saveEventEdit() {
  if (!editingEventId) return;
  const body = {
    id: editingEventId,
    title: editTitle.value.trim(),
    start: editStart.value.trim(),
    end: editEnd.value.trim(),
    location: editLocation.value.trim(),
    note: editNote.value.trim(),
  };
  if (!body.title || !body.start) {
    alert("标题和开始时间不能为空");
    return;
  }
  try {
    await Store.updateEvent(body);
  } catch (e) {
    alert("保存失败：" + e.message);
    return;
  }
  await loadEvents();
  closeEventModal();
}

async function deleteEvent() {
  if (!editingEventId) return;
  if (!confirm("确定删除这个事件？")) return;
  try {
    await Store.deleteEvent(editingEventId);
  } catch (e) {
    alert("删除失败：" + e.message);
    return;
  }
  await loadEvents();
  closeEventModal();
}

// ==================== 事件绑定 ====================
sendBtn.addEventListener("click", send);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") send();
});

exportIcsBtn.addEventListener("click", () => {
  window.open("/export_ics", "_blank");
});

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    activeTab = btn.dataset.tab;
    tabButtons.forEach((b) => b.classList.toggle("active", b === btn));
    renderTasks();
    updateTabGlider();
  });
});

confirmAllBtn.addEventListener("click", confirmAll);

resetBtn.addEventListener("click", async () => {
  try {
    await Store.resetChat();
  } catch (e) {
    toast("清空失败：" + e.message, "error");
    return;
  }
  chatContext = [];
  chatHistory.innerHTML = "";
  appendMessage("assistant", "对话上下文已清空（日历和长期记忆不受影响）。");
});

// Next Week
bottomHandle.addEventListener("click", openNextWeekOverlay);
overlayClose.addEventListener("click", closeNextWeekOverlay);

// Modal
modalEditBtn.addEventListener("click", switchToEditMode);
modalCancelBtn.addEventListener("click", switchToViewMode);
modalCloseBtn.addEventListener("click", closeEventModal);
modalSaveBtn.addEventListener("click", saveEventEdit);
modalDeleteBtn.addEventListener("click", deleteEvent);
eventModal.addEventListener("click", (e) => {
  if (e.target === eventModal) closeEventModal();
});

// ==================== 滑盖指示器 ====================
// 用 getBoundingClientRect 相对容器算偏移：offsetLeft 在不同浏览器里参照的边
// （padding edge / border edge）不一致，容易差几个像素。
function moveGlider(container, active, glider) {
  if (!container || !active || !glider) return;
  const cr = container.getBoundingClientRect();
  const br = active.getBoundingClientRect();
  if (!br.width) return;      // 界面还没显示出来，等下次
  glider.style.width = br.width + "px";
  glider.style.height = br.height + "px";
  glider.style.transform = `translate(${br.left - cr.left}px, ${br.top - cr.top}px)`;
}

const tabGlider = document.getElementById("tab-glider");
const segGlider = document.getElementById("seg-glider");
const taskTabsEl = document.querySelector(".task-tabs");
const mobileSegEl = document.querySelector(".mobile-seg");
const segButtons = document.querySelectorAll(".seg-btn");

function updateTabGlider() {
  moveGlider(taskTabsEl, document.querySelector(".task-tabs .tab-btn.active"), tabGlider);
}

function updateSegGlider() {
  moveGlider(mobileSegEl, document.querySelector(".mobile-seg .seg-btn.active"), segGlider);
}

/**
 * 登录后界面从 display:none 变可见时尺寸才有值 —— 由 session.js 的 startApp
 * 在 showAppView 之后调一次，否则滑盖会算成 0 宽。
 */
function refreshGliders() {
  updateTabGlider();
  updateSegGlider();
}

// ==================== 手机端：左右滑动切换双屏 ====================
const screensEl = document.getElementById("screens");
const MOBILE_MQ = window.matchMedia("(max-width: 860px)");
const isMobileView = () => MOBILE_MQ.matches;

let currentScreen = 0;      // 0 = 对话，1 = 日程

function applyScreen(index) {
  const next = index >= 1 ? 1 : 0;
  const changed = next !== currentScreen;
  currentScreen = next;

  screensEl.classList.remove("dragging");
  screensEl.classList.toggle("show-calendar", next === 1);
  screensEl.style.transform = "";     // 交回 class 控制，顺带清掉拖拽残值

  segButtons.forEach((btn) => {
    const on = Number(btn.dataset.screen) === next;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  updateSegGlider();

  // 切到日历时把周历滚回"现在"附近：手机一屏看不全 24 小时
  if (changed && next === 1) scrollToNow();
}

const swipe = {
  id: null, x0: 0, y0: 0, dx: 0,
  axis: null, moved: false, start: 0, base: 0,
};
let suppressClick = false;

screensEl.addEventListener("pointerdown", (e) => {
  if (!isMobileView() || swipe.id !== null) return;
  if (e.pointerType === "mouse" && e.button !== 0) return;
  swipe.id = e.pointerId;
  swipe.x0 = e.clientX;
  swipe.y0 = e.clientY;
  swipe.dx = 0;
  swipe.axis = null;
  swipe.moved = false;
  swipe.start = currentScreen;
  swipe.base = -currentScreen * window.innerWidth;
});

screensEl.addEventListener("pointermove", (e) => {
  if (e.pointerId !== swipe.id) return;
  const dx = e.clientX - swipe.x0;
  const dy = e.clientY - swipe.y0;

  // 先判方向：9px 以内看不出意图，超过后锁定一次，避免斜向手势来回抢
  if (swipe.axis === null) {
    if (Math.abs(dx) < 9 && Math.abs(dy) < 9) return;
    swipe.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (swipe.axis === "x") {
      screensEl.classList.add("dragging");
      try { screensEl.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    }
  }
  if (swipe.axis !== "x") return;

  swipe.dx = dx;
  swipe.moved = Math.abs(dx) > 6;

  // 已经在第一屏还继续往右 / 第二屏继续往左 —— 橡皮筋阻尼：拖得动但不走
  const atEdge = (currentScreen === 0 && dx > 0) || (currentScreen === 1 && dx < 0);
  const offset = atEdge ? dx * 0.32 : dx;
  screensEl.style.transform = `translateX(${swipe.base + offset}px)`;
});

function endSwipe(e, cancelled) {
  if (e.pointerId !== swipe.id) return;
  const dx = swipe.dx;
  const start = swipe.start;
  const wasDrag = swipe.axis === "x" && swipe.moved;

  swipe.id = null;
  swipe.axis = null;
  swipe.dx = 0;

  if (!wasDrag) return;

  screensEl.classList.remove("dragging");
  // 拖过之后紧跟着的那一下 click 不该落到按钮上
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 0);

  const threshold = window.innerWidth * 0.22;
  let target = start;
  if (!cancelled) {
    if (dx < -threshold) target = start + 1;
    else if (dx > threshold) target = start - 1;
  }
  target = Math.max(0, Math.min(1, target));

  if (target === start) {
    screensEl.style.transform = "";     // 没过阈值：回弹
  } else {
    applyScreen(target);                // 过阈值：吸附到目标屏
  }
}

screensEl.addEventListener("pointerup", (e) => endSwipe(e, false));
screensEl.addEventListener("pointercancel", (e) => endSwipe(e, true));

// 捕获阶段吃掉拖拽后的误点击
screensEl.addEventListener("click", (e) => {
  if (!suppressClick) return;
  e.stopPropagation();
  e.preventDefault();
}, true);

segButtons.forEach((btn) => {
  btn.addEventListener("click", () => applyScreen(Number(btn.dataset.screen)));
});

window.addEventListener("resize", () => {
  updateTabGlider();
  updateSegGlider();
});

function onViewportChange() {
  screensEl.classList.remove("dragging");
  screensEl.style.transform = "";
  if (MOBILE_MQ.matches) {
    screensEl.classList.toggle("show-calendar", currentScreen === 1);
  } else {
    // 回到桌面两栏：滑轨位移留着没有意义，清掉免得下次进来是歪的
    screensEl.classList.remove("show-calendar");
  }
  refreshGliders();
}

// Safari 13 及更早没有 addEventListener，只有旧的 addListener
if (typeof MOBILE_MQ.addEventListener === "function") {
  MOBILE_MQ.addEventListener("change", onViewportChange);
} else if (typeof MOBILE_MQ.addListener === "function") {
  MOBILE_MQ.addListener(onViewportChange);
}

refreshGliders();

// ==================== 初始化 ====================
// 不在这里拉数据：要等登录完成后再拉（见 session.js 的 startApp）。
