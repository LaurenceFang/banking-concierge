# 银小二 · Banking Concierge

银行办事助手：理解中文金融需求，调用工具，在模拟银行中完成转账、账单、理财、卡片、订阅与跨场景任务。

**Banking Concierge** 表达“银小二”的服务角色：帮助用户理解业务、安排步骤并办理银行事务。

A usability-first banking AI agent for Chinese-language workflows. Designed around DeepSeek's official API, tool calling, a business knowledge base, and a simulated bank.

## 当前状态

项目采用 SPEC 驱动。目前仓库包含工程规格、25 个模块的设计、转账接口契约及三份自写知识库种子资料，完整应用尚待实现。

DeepSeek 官方 `deepseek-flash` 已通过基础 JSON 与合成只读工具链验证；这不代表完整银行流程或 RAG 已运行。

## 项目资料

- [SPEC 入口](specs/README.md)
- [架构与技术决策](specs/01-架构与技术决策.md)
- [25 个模块](specs/02-模块规格.md)
- [知识库与 RAG](specs/09-知识库与RAG.md)
- [知识库初始资料](knowledge/README.md)
- [DeepSeek 官方模型配置](specs/08-国产云模型选型.md)
- [实施计划](specs/06-实施计划.md)

## 技术方向

计划采用 Python、FastAPI、Pydantic、LangGraph、PostgreSQL 和 React／TypeScript。可用性和完整用户路径优先，知识问答与业务工具衔接，保留必要身份、确认和资金一致性约束。

运行凭据通过环境变量或本机配置提供，样例见 [.env.example](.env.example)。安装与启动说明将在应用实现后补充。

## 开源许可

本项目自写内容使用 [MIT License](LICENSE)。论文、开源项目及数据来源见 [来源表](specs/07-论文与开源来源.md)和[第三方说明](THIRD_PARTY_NOTICES.md)。
