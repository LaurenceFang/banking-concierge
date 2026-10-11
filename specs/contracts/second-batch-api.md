# 第二批接口与开发包契约

日期：2026-10-12。P2 账单与订阅，附带 P1 会话/通知和前端改版。后端固定当前演示用户，金额为整数人民币分；统计/导入不改变银行余额。

## 开发所有权

- billing agent：`backend/app/billing/**`、`backend/tests/test_billing*.py`。
- subscriptions agent：`backend/app/subscriptions/**`、`backend/tests/test_subscriptions*.py`。
- frontend agent：`web/**`（Astra，medium）。不得修改根配置/后端/正式 SPEC。
- 主集成：正式 SPEC、依赖、`main.py`、`agent.py`、`conversations.py`、银行只读补充方法、会话/通知/API 测试、知识资料及文档。
- 禁止替其他包提交；由主集成统一检查、提交和推送。

## 会话与用户可见历史

Summary：`{id,title,kind:"main"|"temporary",updated_at,unread_count}`。主会话唯一且常驻；新事项创建独立 temporary。

UIMessage：`{id,role:"user"|"assistant"|"notification",text,created_at,sources:[],plan?,action?,receipt?}`。不返回内部工具消息或模型提示词。

Action 为标记联合：

- 转账：原首批 Plan 字段，加 `kind:"transfer"`、`title:"转账确认"`；保留兼容 `plan` 字段。
- 订阅：`{id,kind:"subscription",action:"cancel_subscription"|"stop_autopay",subscription_id,merchant,title,description,status,expires_at,conversation_id?}`。

HTTP：

- `GET /api/conversations` → `{conversations:Summary[]}`，主会话先返回。
- `POST /api/conversations`，`{title?}` → Summary。
- `PATCH /api/conversations/{id}`，`{title}` → Summary。
- `GET /api/conversations/{id}` → `{conversation:Summary,messages:UIMessage[],pending_action:Action|null}`。
- `POST /api/conversations/{id}/read` → `{status:"ok"}`。
- Chat/SSE 保持首批契约，chat result 增加 `action:Action|null`、`message_id`，所有正式 UI 调用均携带已创建的 conversation_id。
- `GET /api/notifications` → `{notifications:[{id,title,text,kind,source,created_at,conversation_id?,operation_id?,read}]}`。

资金/订阅操作完成回执及续费提醒投递主会话，按确定性事件ID去重。先连接模拟银行和订阅事件；不假装接入支付宝。刷新恢复会话历史、知识来源与待确认事项。切换会话不取消其他会话的计划。新消息只使当前会话的旧预览失效。

## 账单 M15–M17

`BillingService(database_url)` 提供：`seed_demo()`、`entries(start=None,end=None)`、`report(start=None,end=None)`、`import_csv(content,filename="账单.csv")`、`close()`。

Entry：`{id,date,merchant,raw_merchant,amount_minor,currency,account_id,category,transaction_kind,source,confidence,reason}`。

- date 为 `YYYY-MM-DD`；amount_minor 收入/退款为正，扣款为负。
- transaction_kind 为 `expense|refund|income|transfer|opening`。
- source 为 `bank|import|demo`。银行记录只读；导入和演示账单独立存储，不写 bank_transactions，不改变余额。
- 商户归一化、类别/规则依据可查询；原始备注是数据，不作为模型指令。

Report：`{start,end,coverage_start,coverage_end,entry_count,income_minor,expense_minor,refund_minor,net_expense_minor,categories:[{category,amount_minor,count}],anomalies:[{entry_id,merchant,amount_minor,reason,severity}],entries:Entry[],source_notice}`。

净消费=消费扣款绝对值−消费退款。分类 amount_minor 合计与净消费一致。期初/转账排除消费与收入统计；退款作为独立口径说明；覆盖不完整不推算全年；异常是复核提示而非欺诈判定。

Import result：`{imported,duplicates,errors:[{row,message}],message}`。CSV 支持中文/英文标准列，日期/商户/金额必需，返回逐行错误与明确导入计数；重复导入去重，冲突不覆盖。

- `GET /api/billing/report?start=&end=` → Report。
- `GET /api/billing/entries?start=&end=` → `{entries:Entry[]}`。
- `POST /api/billing/import`，JSON `{filename,content}`（CSV 文本，最多2MiB）→ Import result。
- `GET /api/billing/sample` → 可下载 CSV。

共同演示商户：星云视频（月扣2390分）、云舟音乐（月扣1500分）、轻云存储（月扣1800分）；云栖公寓房租为2200000分/月，属于周期非订阅消费。账单 agent 提供前6个月合成记录（demo 来源），含退款/转账/别名及非周期消费，不改银行当前余额。数据区间由当前上海日期向过去构建。

## 订阅 M18 与 M19 的订阅部分

`SubscriptionService(database_url)` 提供：`seed_demo()`、`detect(entries,as_of=None)`、`list_subscriptions(entries,as_of=None)`、`create_action_plan(subscription_id,action,conversation_id=None)`、`get_plan(plan_id)`、`pending_for_conversation(conversation_id)`、`invalidate_pending_plans(conversation_id)`、`decide_plan(plan_id,decision)`、`renewal_notifications(entries,as_of=None,window_days=7)`、`close()`。

Contract：`{id,merchant,amount_minor,currency,cycle,next_charge_on,status,autopay_enabled,cancel_supported,source}`，status 为 active/cancelled，source 为 demo_contract。固定3个模拟合同，id 分别为 `demo-sub-video`、`demo-sub-music`、`demo-sub-cloud`，与上述账单商户和金额一致。cloud 不支持商户取消，允许停止代扣；video/music 支持取消。

Candidate：`{id,merchant,cycle,amount_minor,confidence,next_charge_on,price_change,missing_cycles,reason,entry_ids,is_subscription_candidate}`。按账单证据分析周/月/季度/年，别名、漏月、变价、月末日期和周期非订阅消费均有案例。候选不能冒充已经存在的合同。

列表结果：`{subscriptions:Contract[],candidates:Candidate[]}`。

动作仅先生成待确认 Action，审批/拒绝由独立HTTP执行，模型不能自动批准。取消商户合同后状态 cancelled；仅停止代扣时合同仍 active，autopay_enabled=false，明确展示效果。10分钟预览过期，同次确认幂等，改口只取消对应会话的旧预览。get_plan 与 pending_for_conversation 返回上述 Action（不存在返回 None）；decide_plan 返回 `{status:"completed"|"declined",message,operation_id?,code?,conversation_id?}`。错误用 BankError 兼容 code/message 或模块同样的业务异常。

续费提醒返回 `{id,title,text,kind:"renewal",source,created_at,subscription_id,next_charge_on}`，持久化防重复，重启不重复投递。只针对模拟合同活动且代扣开启的续费；候选提醒必须清楚标识估计。本批不自动扣款。

- `GET /api/subscriptions` → 列表结果。
- `POST /api/subscriptions/{id}/preview`，`{action,conversation_id}` → Action。
- `POST /api/subscription-plans/{id}/decision`，`{decision,conversation_id?}` → decision result。
- 银行 `/api/plans/{id}/decision` 请求也增加可选 conversation_id，若提供必须与计划所属会话一致。

## 前端交互与验收

使用已审的第四轮同款三个状态，源图在 `.local/design-review-v4/`，implementation 复制需要的参考和真实字库到 web/design 与 web/public，保留许可。正文尝试文楷、标签轻量无衬线，灰白底色，少量低饱和强调色。

左栏主会话/临时事项分组、创建、切换、改名及收起。右栏有新 pending action 时自动展开；手动关闭按 action ID记忆，同一动作不反复弹；聊天重开入口；切换会话隔离；完成后关闭、保留回执。用真实 sources、会话、银行数据与订阅动作。

账户/交易保留可用；增加账单/订阅入口，提供日期筛选、CSV 导入与逐行结果、分类汇总/退款口径/异常提示、周期候选/合同区别、取消及停止代扣预览。SSE 错误不自动重发。字体/桌面/手机在浏览器核对，不以构建通过代替完整验收。

退出条件：历史恢复、跨会话计划隔离、关闭/重开、回执/提醒去重；账单重复导入、退款和统计一致；周期与非订阅区分、商户取消和代扣区别；真实 DS 问答/工具→页面结果联调。卡片、理财、实际商户/支付宝接入仍属于后续阶段。
