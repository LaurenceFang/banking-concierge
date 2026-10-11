# 银小二 · Banking Concierge

银行办事助手：理解中文金融需求，检索业务知识并调用模拟银行工具。目标覆盖转账、账单、理财、卡片、订阅与跨场景任务。

**Banking Concierge** 表达“银小二”的服务角色：帮助用户理解业务、安排步骤并办理银行事务。

A usability-first banking AI agent for Chinese-language workflows. Designed around DeepSeek's official API, tool calling, a business knowledge base, and a simulated bank.

## 当前状态

项目采用 SPEC 驱动。首批三个开发包已集成：模拟银行、知识库检索和 React 前端，另包含 DeepSeek 官方适配与 LangGraph 编排。

现在可查询账户与最近流水、消歧收款人、生成转账预览、确认或拒绝转账，并查看知识问答的引用资料。真实 DeepSeek `deepseek-flash`、PostgreSQL 和浏览器已联调。理财、卡片、订阅目前提供知识解释，其办理功能以及账单分析、跨场景联动尚待实现。

## 本地启动

需要 Python 3.11、[uv](https://docs.astral.sh/uv/)、Node.js 22 与 Docker Desktop。先启动 Docker Desktop，再在仓库目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-local.ps1
```

脚本安装锁定依赖、构建前端、启动 PostgreSQL 和应用。打开 **http://127.0.0.1:8000/**，保持终端运行；按 Ctrl+C 停止应用。数据库保留数据，下次启动不会重置余额。无需 Docker 的开发体验可加 `-SQLite`，其数据库与 PostgreSQL 独立。

第一次配置 DeepSeek 密钥时，复制 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY`。也可通过环境变量提供密钥；Windows 支持读取当前用户本机已保存的 DPAPI 凭据。密钥不进入仓库。模型固定为官方 `deepseek-flash`，请求发往 `https://api.deepseek.com`。

具体按钮与示例见 [使用指南](docs/使用指南.md)。当前固定使用虚构的林小满账户与模拟资金。

## 项目资料

- [SPEC 入口](specs/README.md)
- [架构与技术决策](specs/01-架构与技术决策.md)
- [25 个模块](specs/02-模块规格.md)
- [知识库与 RAG](specs/09-知识库与RAG.md)
- [知识库初始资料](knowledge/README.md)
- [DeepSeek 官方模型配置](specs/08-国产云模型选型.md)
- [实施计划](specs/06-实施计划.md)

## 技术方向

已采用 Python、FastAPI、Pydantic、LangGraph、SQLAlchemy／PostgreSQL 和 React／TypeScript／Vite。前端通过 SSE 展示进度和答复，转账预览与确认结果使用服务端事实。转账采用整数分、原子记账和幂等确认。

首轮 RAG 使用 Markdown 标题分块、中文分词与 BM25 检索，保留文档版本、标题和片段。涉及规则的问答先检索资料，实时余额和流水通过银行工具获取。向量检索、混合召回与重排保留在后续规格中，当前尚未启用。

## 开发与检查

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check backend
```

前端在 `web` 目录运行 `npm ci`、`npm test`、`npm run build`。开发模式分别启动 `uv run python -X utf8 -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000` 和 `npm run dev`；Vite 的 5173 端口代理到后端。

PostgreSQL 并发检查使用单独测试库，见 [首批实施状态](specs/10-首批实施状态.md)。本地应用为单用户演示，尚未接真实银行接口或部署到公网。

## 开源许可

本项目自写内容使用 [MIT License](LICENSE)。论文、开源项目及数据来源见 [来源表](specs/07-论文与开源来源.md)和[第三方说明](THIRD_PARTY_NOTICES.md)。
