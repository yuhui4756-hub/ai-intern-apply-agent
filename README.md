# 简历投递 Agent

面向 AI 应用开发、Agent 开发和 AI 后端实习投递的本地网页 Agent。

当前阶段：V0.1 本地求职驾驶舱原型。

核心目标：

- 分析岗位 JD 和公司风险。
- 根据简历和偏好判断岗位是否值得投递。
- 生成礼貌、真诚、不夸大的投递话术。
- 管理投递状态和跳过原因。
- 在收到面试邀请后生成准备计划、题库和复盘材料。

需求规格见 [docs/requirements.md](docs/requirements.md)，自动化路线见 [docs/agent_automation_plan.md](docs/agent_automation_plan.md)，当前完成度见 [docs/status.md](docs/status.md)。

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

然后打开 <http://127.0.0.1:8000>。

API Key 可以在设置页直接填写；应用会保存到本地 `.env` 并立即生效，页面只显示打码状态。真实 `.env` 不要提交。

默认浏览器自动化优先使用本机 Microsoft Edge。只有选择 Chromium 时，才需要额外安装一次 Chromium：

```powershell
.\.venv\Scripts\python -m playwright install chromium
```

## V0.1 Includes

- 本地 SQLite 数据库。
- 候选人画像和多份简历版本管理。
- JD 粘贴分析、岗位链接抓取导入、匹配评分、风险判断和一键复制话术。
- 岗位链接支持普通网页抓取和可选 Playwright 浏览器渲染抓取。
- Edge 浏览器岗位搜索采集：打开招聘平台搜索页，采集候选岗位链接，再逐条导入 JD 分析。
- 多段 JD 批量粘贴导入、自动分档和批量状态更新。
- 岗位状态、跳过原因和公司搜索证据记录。
- 面试准备、转写文本复盘和 Markdown 导出。
- OpenAI-compatible 模型配置档、任务路由和 token 统计。

## Practical Workflow

建议先按这个顺序使用：

1. 在“设置”页配置模型。搜索采集本身不依赖 LLM，但 JD 抽取、匹配解释、话术和面试准备会用到模型。
2. 在“简历”页确认候选人信息和简历版本。先填一版也可以，后续再细化。
3. 进入“岗位搜索”，优先使用“推荐流程：手动确认采集”。
4. 点击“1. 打开 Edge 搜索页”，在 Edge 里登录平台、调整城市/关键词/筛选条件，并停留在岗位列表或搜索结果页。
5. 回到应用点击“2. 采集当前 Edge 页面”，生成候选岗位列表。应用会保留上次打开时的平台、关键词和城市。
6. 在候选列表里逐条选择“导入分析”。应用会抓取 JD，保存评分、分档、公司搜索证据和投递话术。
7. 在岗位详情页确认“必投 / 可冲 / 跳过”，复制话术或更新投递状态。当前不会自动点击投递或发送消息。
8. 收到面试邀请后，在“面试准备”页生成准备计划、题库或保存复盘。

当前不会自动点击投递、发送消息或绕过验证码。

岗位搜索采集是浏览器自动化，不是 LLM：Playwright/Edge 负责打开网页、读取链接和页面文本；LLM 只在后续 JD 抽取、匹配解释、话术生成、面试准备等环节参与。

如果“采集当前 Edge 页面”提示无法连接，请先确认使用的是应用打开的专用 Edge 搜索窗口；普通手动打开的 Edge 没有调试端口，应用无法读取当前页面。

模型配置建议：

- `Temperature` 推荐先用 `0.2`。JD 抽取、评分解释这类任务越低越稳定；话术或面试题可以提高到 `0.4-0.6`，但更容易发散。
- `API Key 环境变量名` 可以留空，应用会默认使用 `OPENAI_COMPATIBLE_API_KEY`。这个字段的作用是告诉应用把 Key 存到 `.env` 的哪个变量名下，以及调用模型时从哪里读取。
- 新增模型配置时，如果环境变量名留空，应用会按服务商或配置名称自动建议，例如 DeepSeek -> `DEEPSEEK_API_KEY`，OpenAI -> `OPENAI_API_KEY`。

## Checks

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m compileall app tests
```

## License

MIT
