document.addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-button");
  if (!button) return;

  const targetId = button.getAttribute("data-copy-target");
  const target = document.getElementById(targetId);
  if (!target) return;

  try {
    await navigator.clipboard.writeText(target.value || target.textContent || "");
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => {
      button.textContent = original;
    }, 1200);
  } catch {
    target.focus();
    target.select();
    document.execCommand("copy");
  }
});

(() => {
  const root = document.querySelector("[data-control-chat]");
  if (!root) return;

  const form = root.querySelector("[data-control-form]");
  const input = root.querySelector("[data-control-input]");
  const submit = root.querySelector("[data-control-submit]");
  const transcript = root.querySelector("[data-control-transcript]");
  const status = root.querySelector("[data-control-status]");
  const turnCount = root.querySelector("[data-turn-count]");

  const addText = (parent, tag, value, className = "") => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value || "";
    parent.appendChild(element);
    return element;
  };

  const addTurn = (role, text, className) => {
    const turn = document.createElement("article");
    turn.className = `chat-turn ${className}`;
    addText(turn, "div", role, "chat-role");
    addText(turn, "p", text);
    transcript.appendChild(turn);
    return turn;
  };

  const addTrace = (turn, evidence = {}) => {
    const events = Array.isArray(evidence.events) ? evidence.events : [];
    if (!evidence.reasoning_summary && !events.length) return;

    const details = document.createElement("details");
    details.className = "chat-trace";
    addText(details, "summary", "决策与执行记录");
    if (evidence.reasoning_summary) addText(details, "p", evidence.reasoning_summary, "trace-summary");
    if (events.length) {
      const list = document.createElement("ul");
      list.className = "trace-events";
      events.forEach((event) => {
        const item = document.createElement("li");
        addText(item, "span", event.kind, "trace-kind");
        addText(item, "span", event.status, "badge trace-status");
        addText(item, "span", event.summary);
        list.appendChild(item);
      });
      details.appendChild(list);
    }
    turn.appendChild(details);
  };

  const addSuggestions = (turn, suggestions = []) => {
    if (!Array.isArray(suggestions) || !suggestions.length) return;
    const actions = document.createElement("div");
    actions.className = "form-actions chat-suggestions";
    suggestions.forEach((suggestion) => {
      if (!suggestion || !suggestion.url || !suggestion.label) return;
      const link = document.createElement("a");
      link.className = "button compact";
      link.href = suggestion.url;
      link.textContent = suggestion.label;
      actions.appendChild(link);
    });
    if (actions.childElementCount) turn.appendChild(actions);
  };

  const scrollToLatest = () => {
    transcript.scrollTop = transcript.scrollHeight;
  };

  const setPending = (pending) => {
    form.classList.toggle("is-pending", pending);
    input.disabled = pending;
    submit.disabled = pending;
    submit.textContent = pending ? "执行中..." : "发送";
    status.textContent = pending
      ? "正在执行受控任务，请等待本轮结果。"
      : "搜索、读取和分析可直接执行；发送、投递和敏感信息需确认。";
  };

  scrollToLatest();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) {
      input.focus();
      return;
    }

    const empty = transcript.querySelector("[data-control-empty]");
    if (empty) empty.remove();
    addTurn("你", message, "user-turn");
    input.value = "";
    setPending(true);
    scrollToLatest();

    try {
      const response = await fetch("/api/control/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok || !payload.conversation) {
        throw new Error(payload.error || "任务执行失败，请稍后重试。");
      }

      const conversation = payload.conversation;
      const turn = addTurn("Agent", conversation.response_text, "agent-turn");
      addTrace(turn, conversation.evidence);
      addSuggestions(turn, conversation.evidence?.suggestions);
      const count = transcript.querySelectorAll(".agent-turn").length;
      turnCount.textContent = `${count} 轮`;
    } catch (error) {
      const turn = addTurn("Agent", error instanceof Error ? error.message : "任务执行失败，请稍后重试。", "agent-turn");
      addTrace(turn, {
        reasoning_summary: "接口未返回有效结果，本轮没有执行提交型动作。",
        events: [{ kind: "接口调用", status: "失败", summary: "未保存本轮任务，请检查本地服务是否正在运行。" }],
      });
    } finally {
      setPending(false);
      input.focus();
      scrollToLatest();
    }
  });
})();
