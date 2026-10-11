# 第一批可用版本接口

后端前缀 `/api`，金额统一整数人民币分。首版使用服务端固定的演示用户，不接受客户端提供任意用户身份。模型通过 DS 官方 API 运行；银行工具只连接模拟账本。

## HTTP

- `GET /api/health` → `{status, model, model_configured, storage, knowledge_documents}`。
- `GET /api/overview` → `{user: {id,name}, accounts: [{id,name,kind,balance_minor,currency}], transactions: [{id,account_id,description,amount_minor,currency,created_at}]}`。流水金额正数为入账、负数为扣款。
- `POST /api/chat`，请求 `{message, conversation_id?}` → `{conversation_id, reply, sources: [{doc_id,title,excerpt,version}], plan?}`。
- `POST /api/chat/stream` 使用同样请求。SSE 事件：`status` 为 `{message}`；`token` 为 `{text}`；`result` 为上述完整 chat 响应；`error` 为 `{message,code}`；`done` 为 `{}`。前端仅在初始 HTTP 404/405 拒绝流式路由时回退非流式接口；已接受的流失败或中断时不自动重发。
- `POST /api/plans/{plan_id}/decision`，请求 `{decision: "approve" | "decline"}` → `{status: "completed" | "declined", message, operation_id?}`。成功后前端重新读取 overview。
- 业务错误统一 `{detail: {code,message}}`，前端显示 message。

plan 为 `{id,source_account_id,recipient_id,recipient_name,destination_account_id,amount_minor,fee_minor,total_minor,currency,status,expires_at}`；等待确认时 status=`pending`。账户来自服务端返回的数据，前端不写死余额。

## 后端包接口

`backend/app/bank/service.py` 暴露 `BankService(database_url)`，提供 `seed()`、`overview()`、`list_accounts()`、`list_transactions(account_id=None)`、`find_recipients(query)`、`create_transfer_plan(source_account_id,recipient_id,amount_minor,conversation_id=None)`、`decide_plan(plan_id,decision)`、`invalidate_pending_plans(conversation_id)`。返回可 JSON 序列化 dict/list；业务异常携带 code/message。

`backend/app/knowledge/service.py` 暴露 `KnowledgeService(knowledge_dir)`，提供 `reload()`、`search(query,limit=5,category=None)`、`document_count`；search 返回 `{doc_id,title,excerpt,version,score}` 列表，尊重 manifest 当前有效文档。

公共依赖、FastAPI 入口、DS 适配与 LangGraph 编排由主集成人维护。银行、知识和 web 各自维护所属目录，禁止并发修改共享配置或替其他包提交。

独立测试使用固定合成数据。网页与真实 DS 联调由主集成人统一运行；不得把静态占位响应当最终可用版本。
