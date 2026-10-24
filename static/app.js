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
const calTodayBtn = document.getElementById("cal-today");

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
  chatHistory.scrollTop = chatHistory.scrollHeight;
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
  chatHistory.scrollTop = chatHistory.scrollHeight;
  return div;
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
 */
function flushSegment(bubble, block) {
  const text = (bubble.textContent || "").trim();
  bubble.textContent = "";
  if (text) appendReasonLine(block, text);
}

function setStreaming(on) {
  streaming = on;
  chatInput.disabled = on;
  sendBtn.disabled = on;
  chatInput.placeholder = on ? "正在回复…" : "输入任务，例如：明天下午三点开会";
}

// 位置：浏览器定位，缓存 5 分钟。用户拒绝或超时就返回 null，
// 服务端会用客户端 IP 做城市级兜底（所以这里失败不影响功能）。
let cachedLocation = { at: 0, value: null };

function browserLocation() {
  if (Date.now() - cachedLocation.at < 5 * 60 * 1000) {
    return Promise.resolve(cachedLocation.value);
  }
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    const timer = setTimeout(() => resolve(null), 3000);   // 授权弹窗没点就不等
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        clearTimeout(timer);
        const value = {
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy: Math.round(pos.coords.accuracy || 0),
          source: "browser",
        };
        cachedLocation = { at: Date.now(), value };
        resolve(value);
      },
      () => {
        clearTimeout(timer);
        cachedLocation = { at: Date.now(), value: null };
        resolve(null);
      },
      { enableHighAccuracy: true, timeout: 3000, maximumAge: 60000 }
    );
  });
}

async function send() {
  const text = chatInput.value.trim();
  if (!text || streaming) return;
  if (handleMemoryCommand(text)) return;

  appendMessage("user", text);
  chatInput.value = "";

  const bubble = appendStreamBubble();
  const reasoning = showSteps.checked ? createReasoningBlock() : null;
  setStreaming(true);

  // 只累积"当前这一段"的文字；下一段开始时会被清空
  let output = "";

  try {
    const location = await browserLocation();
    await Store.chatStream(text, {
      events: confirmedEvents,
      pending: pendingData,
      history: chatContext.slice(-10),
      location,
    }, async (ev) => {
      if (ev.type === "token") {
        output += ev.text;
        bubble.textContent = output;
        chatHistory.scrollTop = chatHistory.scrollHeight;

      } else if (ev.type === "segment") {
        flushSegment(bubble, reasoning);
        output = "";

      } else if (ev.type === "step") {
        if (reasoning) reasoning.summary.textContent = `推理过程 · 正在调用 ${ev.tool}…`;
        appendReasonLine(reasoning,
          `↳ 调用 ${ev.tool}，参数 ${JSON.stringify(ev.args)}`);

      } else if (ev.type === "tasks") {
        // 任务一识别出来就进待确认列表，不用等回答说完
        if (ev.tasks && ev.tasks.length > 0) await addTasks(ev.tasks);

      } else if (ev.type === "done") {
        bubble.classList.remove("streaming");
        if (!output && ev.output) {
          bubble.textContent = ev.output;
          output = ev.output;
        }
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
        bubble.classList.remove("streaming");
        bubble.textContent = "出错了：" + ev.message;
        output = "";
      }
    });

    // 流正常结束但没收到 done（例如连接被提前断开）时的兜底
    if (!bubble.textContent && !output) {
      bubble.classList.remove("streaming");
      bubble.textContent = "（没有收到回复）";
    }
  } catch (e) {
    bubble.classList.remove("streaming");
    bubble.textContent = "出错了：" + e.message;
  } finally {
    setStreaming(false);
    chatHistory.scrollTop = chatHistory.scrollHeight;
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
    empty.style.color = "var(--text-dim)";
    empty.style.fontSize = "12px";
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

calTodayBtn.addEventListener("click", scrollToNow);

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

// ==================== 初始化 ====================
// 不在这里拉数据：要等登录完成后再拉（见 session.js 的 startApp）。
