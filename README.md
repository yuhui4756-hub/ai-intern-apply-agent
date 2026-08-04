# 简历投递 Agent

面向 AI 应用开发、Agent 开发和 AI 后端实习投递的本地网页 Agent。

当前阶段：需求规格整理与 V0.1 设计。

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

真实 API Key 只填在本地 `.env` 或系统环境变量中，不要提交。

## V0.1 Includes

- 本地 SQLite 数据库。
- 候选人画像和多份简历版本管理。
- JD 粘贴分析、匹配评分、风险判断和一键复制话术。
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
