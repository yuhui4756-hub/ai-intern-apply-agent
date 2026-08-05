# 简历投递 Agent

面向 AI 应用开发、Agent 开发和 AI 后端实习投递的本地网页 Agent。

当前阶段：V0.1 本地求职驾驶舱原型。

核心目标：

- 分析岗位 JD 和公司风险。
- 根据简历和偏好判断岗位是否值得投递。
- 生成礼貌、真诚、不夸大的投递话术。
- 管理投递状态和跳过原因。
- 在收到面试邀请后生成准备计划、题库和复盘材料。

需求规格见 [docs/requirements.md](docs/requirements.md)。

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

然后打开 <http://127.0.0.1:8000>。

API Key 可以在设置页直接填写；应用会保存到本地 `.env` 并立即生效，页面只显示打码状态。真实 `.env` 不要提交。

如果要使用“浏览器渲染”抓取动态招聘页，需要额外安装一次 Chromium：

```powershell
.\.venv\Scripts\python -m playwright install chromium
```

## V0.1 Includes

- 本地 SQLite 数据库。
- 候选人画像和多份简历版本管理。
- JD 粘贴分析、岗位链接抓取导入、匹配评分、风险判断和一键复制话术。
- 岗位链接支持普通网页抓取和可选 Playwright 浏览器渲染抓取。
- 多段 JD 批量粘贴导入、自动分档和批量状态更新。
- 岗位状态、跳过原因和公司搜索证据记录。
- 面试准备、转写文本复盘和 Markdown 导出。
- OpenAI-compatible 模型配置档、任务路由和 token 统计。

## Checks

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m compileall app tests
```

## License

MIT
