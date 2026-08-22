(() => {
  const root = document.querySelector("[data-agent-workspace]");
  if (!root) return;

  const bootstrap = JSON.parse(root.dataset.bootstrap || "{}");
  const state = {
    sessions: bootstrap.sessions || [],
    profiles: bootstrap.profiles || [],
    canvas: bootstrap.canvas || [],
    conversation: bootstrap.conversation || null,
    view: "chat",
    canvasFilter: "all",
  };
  const $ = (selector) => root.querySelector(selector);
  const transcript = $("[data-transcript]");
  const sessionList = $("[data-session-list]");
  const canvas = $("[data-canvas]");
  const composer = $("[data-composer]");
  const input = $("[data-composer-input]");
  const send = $("[data-send]");
  const modelSelect = $("[data-model-select]");
  const autoCommunication = $("[data-auto-communication]");
  const manualMenu = $("[data-manual-menu]");

  function text(parent, tag, value, className = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value || "";
    parent.appendChild(element);
    return element;
  }

  function activeSession() { return state.conversation?.session; }

  function formatEvents(events = []) {
    if (!Array.isArray(events) || !events.length) return null;
    const details = document.createElement("details");
    details.className = "agent-events";
    text(details, "summary", "计划与工具记录");
    const list = document.createElement("ul");
    events.forEach((item) => {
      const row = document.createElement("li");
      text(row, "span", item.kind || "事件", "agent-event-kind");
      text(row, "span", item.status || "", "agent-event-status");
      text(row, "span", item.summary || "");
      list.appendChild(row);
    });
    details.appendChild(list);
    return details;
  }

  function renderTranscript() {
    transcript.replaceChildren();
    const messages = state.conversation?.messages || [];
    if (!messages.length) {
      const empty = document.createElement("div");
      empty.className = "agent-welcome";
      text(empty, "h2", "从一个任务开始");
      text(empty, "p", "可以让我搜索岗位、比较机会、检查简历、分析当前受控 Edge 页面，或准备沟通和面试。");
      transcript.appendChild(empty);
      return;
    }
    messages.forEach((message) => {
      const article = document.createElement("article");
      article.className = `agent-message ${message.role === "user" ? "user" : "assistant"}`;
      text(article, "div", message.role === "user" ? "你" : "求职agent", "agent-message-role");
      text(article, "p", message.content);
      const events = formatEvents(message.events);
      if (events) article.appendChild(events);
      transcript.appendChild(article);
    });
    transcript.scrollTop = transcript.scrollHeight;
  }

  function renderSessions() {
    sessionList.replaceChildren();
    const activeId = activeSession()?.id;
    state.sessions.forEach((session) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `agent-session ${session.id === activeId ? "active" : ""}`;
      button.dataset.sessionId = session.id;
      text(button, "strong", session.title || "新任务");
      text(button, "span", session.summary || `${session.message_count || 0} 条消息`);
      sessionList.appendChild(button);
    });
  }

  function renderModels() {
    modelSelect.replaceChildren();
    const activeProfile = activeSession()?.model_profile_id;
    state.profiles.forEach((profile) => {
      const option = document.createElement("option");
      option.value = profile.id;
      option.selected = profile.id === activeProfile;
      option.disabled = !profile.configured;
      option.textContent = `${profile.name}${profile.model ? ` / ${profile.model}` : ""}${profile.configured ? "" : "（未配置）"}`;
      modelSelect.appendChild(option);
    });
  }

  function renderContext() {
    const session = activeSession();
    if (!session) return;
    $("[data-session-title]").textContent = session.title || "新任务";
    $("[data-session-summary]").textContent = session.summary || "用自然语言安排搜索、分析和准备工作。";
    $("[data-model-label]").textContent = session.model_profile_name || "未选择模型";
    autoCommunication.checked = Boolean(session.auto_communication);
    $("[data-policy-note]").textContent = session.auto_communication ? "受限自动沟通已授权，仍受闸门与限额约束" : "关闭时仅生成草稿";
    const task = $("[data-task-context]");
    task.replaceChildren();
    const context = state.conversation?.state || {};
    const rows = [
      ["模型", session.model_profile_name || "未选择"],
      ["自动沟通", session.auto_communication ? "开启" : "关闭"],
      ["受控 Edge", context.edge?.status || "未检查"],
      ["自动化", context.automation?.status_label || "未检查"],
    ];
    rows.forEach(([key, value]) => { text(task, "dt", key); text(task, "dd", value); });
    $("[data-edge-status]").textContent = `受控 Edge ${context.edge?.status || "未连接"}`;
    const jobBox = $("[data-active-job]");
    jobBox.replaceChildren();
    const job = context.active_job;
    if (!job) { jobBox.className = "agent-empty"; jobBox.textContent = "尚未选择岗位。"; }
    else {
      jobBox.className = "agent-job-context";
      text(jobBox, "strong", `${job.company} - ${job.title}`);
      text(jobBox, "p", `${job.match_score} 分 · ${job.recommendation} · 风险 ${job.risk_level}`);
      text(jobBox, "p", `${job.city || "地点待确认"} · ${job.salary_text || "薪资待确认"}`);
      const details = document.createElement("details");
      details.className = "agent-job-details";
      text(details, "summary", "岗位详情");
      const fields = [
        ["技术要求", job.required_skills],
        ["匹配证据", job.matched_skills],
        ["能力缺口", job.missing_skills],
      ];
      fields.forEach(([label, values]) => {
        if (!Array.isArray(values) || !values.length) return;
        const row = document.createElement("p");
        text(row, "strong", `${label}：`);
        row.append(document.createTextNode(values.join("、")));
        details.appendChild(row);
      });
      if (job.skip_reason) {
        const reason = document.createElement("p");
        text(reason, "strong", "归档/跳过原因：");
        reason.append(document.createTextNode(job.skip_reason));
        details.appendChild(reason);
      }
      jobBox.appendChild(details);
    }
    const observations = $("[data-observations]");
    observations.replaceChildren();
    const assistant = [...(state.conversation?.messages || [])].reverse().find((item) => item.role === "assistant");
    const toolEvents = (assistant?.events || []).filter((item) => item.kind === "工具结果");
    if (!toolEvents.length) { observations.className = "agent-empty"; observations.textContent = "等待 Agent 调用工具。"; }
    else {
      observations.className = "agent-observations";
      toolEvents.slice(-4).forEach((item) => {
        const row = document.createElement("div");
        text(row, "strong", item.status || "已完成");
        text(row, "span", item.summary || "");
        observations.appendChild(row);
      });
    }
  }

  function renderCanvas() {
    canvas.replaceChildren();
    const visible = state.canvas.filter((job) => state.canvasFilter === "all" || job.status === state.canvasFilter);
    $("[data-canvas-count]").textContent = `${visible.length} 个岗位`;
    if (!visible.length) { text(canvas, "p", "当前筛选下没有岗位。", "agent-empty"); return; }
    visible.forEach((job) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "agent-job-card";
      button.dataset.jobId = job.id;
      text(button, "span", job.status, "agent-job-status");
      text(button, "h3", `${job.company} - ${job.title}`);
      text(button, "p", `${job.match_score} 分 · ${job.recommendation} · 风险 ${job.risk_level}`);
      text(button, "p", `${job.city || "地点待确认"} · ${job.salary_text || "薪资待确认"}`);
      if (job.summary) text(button, "small", job.summary);
      canvas.appendChild(button);
    });
  }

  function render() { renderSessions(); renderModels(); renderTranscript(); renderContext(); renderCanvas(); }

  function setPending(pending) {
    input.disabled = pending; send.disabled = pending;
    send.textContent = pending ? "执行中" : "发送";
  }

  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "本地 Agent 请求失败。");
    return payload;
  }

  async function loadSession(sessionId) {
    const payload = await request(`/api/agent/sessions/${sessionId}`);
    state.conversation = payload.conversation;
    render();
  }

  root.addEventListener("click", async (event) => {
    const sessionButton = event.target.closest("[data-session-id]");
    if (sessionButton) { await loadSession(sessionButton.dataset.sessionId); return; }
    const switcher = event.target.closest("[data-view-switch]");
    if (switcher) {
      state.view = switcher.dataset.viewSwitch;
      $("[data-chat-view]").hidden = state.view !== "chat";
      $("[data-canvas-view]").hidden = state.view !== "canvas";
      return;
    }
    if (event.target.closest("[data-toggle-manual]") || event.target.closest("[data-open-manual]")) { manualMenu.hidden = !manualMenu.hidden; return; }
    if (event.target.closest("[data-toggle-context]")) { $("[data-context-panel]").classList.toggle("collapsed"); return; }
    if (event.target.closest("[data-new-session]")) {
      const payload = await request("/api/agent/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_profile_id: modelSelect.value || null }) });
      state.sessions = payload.sessions; state.conversation = payload.conversation; state.view = "chat"; $("[data-chat-view]").hidden = false; $("[data-canvas-view]").hidden = true; render(); input.focus(); return;
    }
    const job = event.target.closest("[data-job-id]");
    if (job) {
      const payload = await request(`/api/agent/sessions/${activeSession().id}/active-job`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: Number(job.dataset.jobId) }) });
      state.conversation = payload.conversation; render(); return;
    }
    const clear = event.target.closest("[data-clear-job]");
    if (clear) {
      const payload = await request(`/api/agent/sessions/${activeSession().id}/active-job`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: 0 }) }).catch(() => null);
      if (payload) { state.conversation = payload.conversation; render(); }
    }
    const filter = event.target.closest("[data-canvas-filter]");
    if (filter) { state.canvasFilter = filter.dataset.canvasFilter; root.querySelectorAll("[data-canvas-filter]").forEach((item) => item.classList.toggle("active", item === filter)); renderCanvas(); }
  });

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim(); if (!message) return;
    setPending(true);
    try {
      const payload = await request(`/api/agent/sessions/${activeSession().id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, model_profile_id: Number(modelSelect.value) || null, auto_communication: autoCommunication.checked }) });
      state.conversation = payload.conversation; state.sessions = payload.sessions; state.canvas = payload.canvas; input.value = ""; render();
    } catch (error) { window.alert(error.message || "任务执行失败。"); }
    finally { setPending(false); input.focus(); }
  });

  render();
})();
