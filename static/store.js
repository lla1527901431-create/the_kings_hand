// ============================================================================
// 数据访问层
//
// 把散落的接口调用收敛到一处，好处：
//   * 换存储（比如改回云端）只需要改这个文件
//   * 上层逻辑只关心"拿数据/存数据"，不关心走哪个地址
//
// 注意：日程/待办/记忆现在存在服务端本地 SQLite 里，请求走的是本机回环
// （127.0.0.1），不产生跨境网络请求，所以是毫秒级。
// ============================================================================

const Store = {
  // ---- 日程 ----
  async listEvents() {
    const data = await api("/events");
    return data.events || [];
  },
  addEvent(event) {
    return api("/event/add", { method: "POST", body: event });
  },
  updateEvent(event) {
    return api("/event/update", { method: "POST", body: event });
  },
  deleteEvent(id) {
    return api("/event/delete", { method: "POST", body: { id } });
  },

  // ---- 待确认 / 待计划 ----
  loadPending() {
    return api("/pending");
  },
  addPending(task) {
    return api("/pending/add", { method: "POST", body: task });
  },
  updatePending(task) {
    return api("/pending/update", { method: "POST", body: task });
  },
  deletePending(id) {
    return api("/pending/delete", { method: "POST", body: { id } });
  },
  confirmPending(id) {
    return api("/pending/confirm", { method: "POST", body: { id } });
  },

  // ---- 记忆 ----
  loadMemory() {
    return api("/memory");
  },
  saveMemory(payload) {
    return api("/memory/update", { method: "POST", body: payload });
  },
  clearMemory() {
    return api("/memory/clear", { method: "POST" });
  },

  // ---- 对话 ----
  // 日程/待办/记忆随请求一起发出，让模型能判断冲突与重复（服务端不再自行查库）
  // 非流式：一次拿回 { output, steps, tasks }
  chat(message, context) {
    return api("/chat", {
      method: "POST",
      body: this._chatBody(message, context),
    });
  },

  // 流式：每个 SSE 事件回调一次 onEvent
  chatStream(message, context, onEvent) {
    return apiStream("/chat/stream", {
      method: "POST",
      body: this._chatBody(message, context),
    }, onEvent);
  },

  _chatBody(message, context) {
    return {
      message,
      events: (context && context.events) || [],
      pending: (context && context.pending) || {},
      history: (context && context.history) || [],
    };
  },
  resetChat() {
    return api("/reset", { method: "POST" });
  },

  // ---- 凭据 ----
  credentialStatus() {
    return api("/credentials");
  },
  saveCredentials(payload) {
    return api("/credentials", { method: "POST", body: payload });
  },
  clearCredentials() {
    return api("/credentials", { method: "DELETE" });
  },
};
