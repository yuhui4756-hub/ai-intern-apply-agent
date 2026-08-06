const APP_BASE_URL = "http://127.0.0.1:8000";
const CAPTURE_API_URL = `${APP_BASE_URL}/api/extension/capture`;

const statusBadge = document.getElementById("statusBadge");
const resultBox = document.getElementById("result");
const captureJobButton = document.getElementById("captureJob");
const captureSearchButton = document.getElementById("captureSearch");
const captureConversationButton = document.getElementById("captureConversation");

captureJobButton.addEventListener("click", () => captureCurrentPage("job"));
captureSearchButton.addEventListener("click", () => captureCurrentPage("search"));
captureConversationButton.addEventListener("click", () => captureCurrentPage("conversation"));

async function captureCurrentPage(captureType) {
  setBusy(true, "采集中");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      throw new Error("没有找到当前标签页。");
    }

    const injectionResults = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: collectPageData,
      args: [captureType],
    });
    const pageData = normalizeInjectionResult(injectionResults);
    const response = await fetch(CAPTURE_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pageData),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error(payload.error || "本地应用没有接受采集结果。");
    }

    renderSuccess(payload);
  } catch (error) {
    renderError(error.message || String(error));
  } finally {
    setBusy(false);
  }
}

function normalizeInjectionResult(injectionResults) {
  if (!Array.isArray(injectionResults) || injectionResults.length === 0) {
    throw new Error("页面采集结果为空，请刷新招聘页面后重试。");
  }
  const result = injectionResults.find((item) => item && item.result && typeof item.result === "object")?.result;
  if (!result || Array.isArray(result)) {
    throw new Error("页面采集结果异常，请重新加载扩展后重试。");
  }
  return result;
}

function collectPageData(captureType) {
  const cleanInline = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const cleanBlock = (value) =>
    String(value || "")
      .replace(/\r/g, "\n")
      .split(/\n+/)
      .map((line) => cleanInline(line))
      .filter(Boolean)
      .join("\n");
  const isJobHref = (href, text = "") => {
    const value = `${href || ""} ${text || ""}`.toLowerCase();
    return /job|jobs|job_detail|intern|zhaopin|zhiwei|position|岗位|职位|实习|开发|agent|ai/.test(value);
  };
  const jobLinkCount = (element) => {
    const links = Array.from(element.querySelectorAll("a[href]"))
      .map((anchor) => ({
        href: anchor.href || "",
        text: cleanInline(anchor.innerText || anchor.textContent || anchor.title || ""),
      }))
      .filter((item) => item.href && isJobHref(item.href, item.text))
      .map((item) => item.href.split("#")[0].split("?")[0]);
    return new Set(links).size;
  };
  const findJobCard = (anchor) => {
    let best = anchor;
    let element = anchor;
    for (let depth = 0; element && depth < 8; depth += 1, element = element.parentElement) {
      const text = cleanBlock(element.innerText || element.textContent || "");
      if (text.length < 8 || text.length > 900) {
        continue;
      }
      if (jobLinkCount(element) > 1) {
        continue;
      }
      best = element;
    }
    return best;
  };
  const collectCandidateCards = () => {
    const seen = new Set();
    return Array.from(document.querySelectorAll("a[href]"))
      .filter((anchor) => isJobHref(anchor.href || "", anchor.innerText || anchor.textContent || anchor.title || ""))
      .map((anchor) => {
        const card = findJobCard(anchor);
        const href = anchor.href || "";
        const text = cleanBlock(card.innerText || card.textContent || "");
        const title = cleanInline(anchor.innerText || anchor.textContent || anchor.title || "");
        return { href, title, text };
      })
      .filter((item) => {
        if (!item.href || item.href.startsWith("javascript:") || item.text.length < 8 || item.text.length > 900) {
          return false;
        }
        const key = item.href.split("#")[0].split("?")[0];
        if (seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      })
      .slice(0, 500);
  };
  const pageText = (document.body && document.body.innerText ? document.body.innerText : "")
    .replace(/\n{3,}/g, "\n\n")
    .slice(0, 20000);
  const links = Array.from(document.querySelectorAll("a[href]"))
    .slice(0, 500)
    .map((anchor) => {
      const container = findJobCard(anchor);
      return {
        href: anchor.href || "",
        text: cleanInline(anchor.innerText || anchor.textContent || ""),
        title: cleanInline(anchor.title || ""),
        context: cleanBlock(container ? container.innerText || "" : ""),
      };
    })
    .filter((item) => item.href && !item.href.startsWith("javascript:"));
  const cards = collectCandidateCards();

  return {
    capture_type: captureType,
    url: location.href,
    title: document.title || "",
    text: pageText,
    links,
    cards,
    captured_at: new Date().toISOString(),
  };
}

function renderSuccess(payload) {
  statusBadge.textContent = "已采集";
  const sourceText = payload.source_count ? `，读取 ${payload.source_count} 条当前页链接/卡片` : "";
  const labels = {
    job: "岗位详情",
    conversation: `当前对话：${payload.message_type || "已分析"}`,
  };
  const label = labels[payload.capture_type] || `搜索结果：${payload.candidate_count || 0} 个候选${sourceText}`;
  const resultVerb = payload.skipped ? "已跳过" : "已保存";
  const targetUrl = `${APP_BASE_URL}${payload.redirect_url}`;
  resultBox.className = "result";
  resultBox.innerHTML = `
    <div>${escapeHtml(label)}${resultVerb}。</div>
    <p><a href="${targetUrl}" target="_blank" rel="noreferrer">打开本地结果</a></p>
  `;
}

function renderError(message) {
  statusBadge.textContent = "失败";
  resultBox.className = "result error";
  resultBox.textContent = message;
}

function setBusy(isBusy, label = "未采集") {
  captureJobButton.disabled = isBusy;
  captureSearchButton.disabled = isBusy;
  captureConversationButton.disabled = isBusy;
  if (isBusy) {
    statusBadge.textContent = label;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
