# 银小二 · Banking Concierge

银行办事助手：理解中文金融需求，检索业务知识并调用模拟银行工具。目标覆盖转账、账单、理财、卡片、订阅与跨场景任务。

**Banking Concierge** 表达“银小二”的服务角色：帮助用户理解业务、安排步骤并办理银行事务。

A usability-first banking AI agent for Chinese-language workflows. Designed around DeepSeek's official API, tool calling, a business knowledge base, and a simulated bank.

## 当前状态

项目采用 SPEC 驱动。已接入模拟卡片与理财，并按用户审阅的v2样稿完成四入口工作区：账户、账单、财富、生活。主页显示全部对话，业务页左栏按二级页面显示相关对话，右侧在聊天与确认之间切换；核心流程已通过真实DS与浏览器验收。当前范围见[四入口实施状态](specs/15-四入口工作区实施状态.md)。

现在可查询账户与流水、消歧收款人、预览和确认转账；导入与分析账单；检测周期候选并管理模拟订阅；查询和操作模拟卡片；完成风险测评、比较产品、预览申购／赎回、查看持仓和 T+1 订单结算。会话和引用可刷新恢复，受理及结算回执进入日常助手。AA收款、定时转账及预留／撤销位于“账单 → 收付款”；“生活 → 我的安排”支持预算内鲜花与蛋糕的组合下单、失败退款与原单恢复。详见[AA与定时状态](specs/18-AA与定时前端实施状态.md)及[跨场景联动状态](specs/20-跨场景联动实施状态.md)。全部使用模拟账户和商户。

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

已采用 Python、FastAPI、Pydantic、LangGraph、SQLAlchemy／PostgreSQL 和 React／TypeScript／Vite。前端通过 SSE 展示进度和答复，共享动作抽屉覆盖转账、订阅、卡片与理财。账单统计使用整数分，订阅检测包含周期、漏月和变价。卡片使用状态版本校验；理财订单、现金和持仓在同一数据库事务中更新，受理与结算分开，支持幂等和重启恢复。

首轮 RAG 使用 Markdown 标题分块、中文分词与 BM25 检索，保留文档版本、标题和片段。涉及规则的问答先检索资料，实时余额和流水通过银行工具获取。向量检索、混合召回与重排保留在后续规格中，当前尚未启用。

## 开发与检查

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check backend
```

前端在 `web` 目录运行 `npm.cmd ci`、`npm.cmd test`、`npm.cmd run build`。开发模式分别启动 `uv run python -X utf8 -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000` 和 `npm.cmd run dev`；Vite 的 5173 端口代理到后端。

PostgreSQL 并发检查只允许专用测试库 `127.0.0.1:55432/concierge_test`；设置 `BANK_TEST_DATABASE_URL` 和 `SUBSCRIPTIONS_TEST_DATABASE_URL` 后运行 pytest。测试会重建测试库的银行或订阅表，不能使用应用数据库。本地应用为单用户演示，尚未接真实银行、支付宝、鲜花商户或部署到公网。

## 开源许可

本项目自写内容使用 [MIT License](LICENSE)。论文、开源项目及数据来源见 [来源表](specs/07-论文与开源来源.md)和[第三方说明](THIRD_PARTY_NOTICES.md)。
