# P3-B：AA、定时与跨场景契约

2026-10-13，先规格后并行实现。新增前端必须先出图、用户认可后再写运行页面。继续单用户模拟银行、官方DeepSeek、Python模块化单体，不接外部支付/短信/商户。前端任务中心包含AA、定时转账、联动安排三类。金额均CNY整数分；所有操作只作用于虚构数据。

## 共用资金与时间（主集成负责）

`bank.models.FundHold`：`id,account_id,reference(unique),amount_minor,remaining_minor,status(active|consumed|released),created_at`，预留不减少账面余额。账户响应增加 `reserved_minor,available_minor`，可用=账面余额−有效预留。即时转账、理财申购、AA模拟付款及任务预留均不得侵占其他预留。

`app.bank.reservations` 在调用方SQLAlchemy同一事务内工作，不自行commit：
- `available_balance(session, account:Account)->int`；`reserved_balance(session,account_id)->int`。
- `lock_accounts(session,*ids)->dict[id,Account]`，按ID排序加行锁。
- `reserve_funds(session,account_id,amount_minor,reference)->FundHold`，固定当前用户归属，同reference同参数重试返回原有效预留，不同参数冲突。
- `release_funds(session,hold_id)->FundHold`，幂等释放尚未消费的部分。
- `consume_funds(session,hold_id,destination_id,amount_minor,operation_id,description)->str`：原子减预留、扣付款账户、加收款账户、记平衡双边流水；可分多次消费，累计不超过原预留；相同operation重试不再扣预留或钱。
- `transfer_funds(session,source_id,destination_id,amount_minor,operation_id,description)->str`：不使用预留的平衡转账，校验可用余额；调用方负责该笔模拟付款/退款的业务授权及归属。所有operation ID≤64字符，流水ID派生为operation加借/贷后缀。

每个资金写事务统一先按ID锁涉及的账户，再锁任务/订单/预留；SQLite沿用BEGIN IMMEDIATE。业务服务不得先独立扣款再存订单，不得重置应用表。失败补偿是新退款分录，不能删除已发生的账务。

`app.task_clock.TaskClock(database_url)`：`now()->datetime(UTC aware)`, `advance(minutes:int)->datetime`, `close()`；内部持久offset秒，逻辑时间=真实UTC+offset，不改变OS或理财业务日。1≤minutes≤525600。helper `task_now(session)->aware datetime` 从同库clock表读时间。无row则真实UTC，主集成先初始化Clock再seed业务。日期请求必须含时区，不接受模糊/naive时间；展示按Asia/Shanghai。

主应用运行期间后台每2秒调用两服务的 `process_due()`，并同步事件；启动补查过期/到期任务，关闭时等待本轮处理结束。后台不调用LLM；授权只覆盖确认过的对象、金额、时间和补偿范围。应用关闭时不运行，重启后在授权窗口内补执行；超过24小时窗口则过期并释放预留。演示推进时间后立即跑任务。查询接口不负责触发任务，页面按可见性定期刷新状态。

## 通用计划/事件

计划TTL为真实时间10分钟，`{id,kind,action,title,description,status,expires_at,conversation_id,...}`；状态pending/completed/declined/invalidated/expired。确认结果 `{status:completed|declined,message,operation_id?,conversation_id?,task_id?,task_status?,state_version?}`。`completed`仅表示该次确认操作完成，任务可能仍scheduled/awaiting_payment/refund_pending，文案不得混淆。

共有方法：`get_plan(id)`, `pending_for_conversation(id)`, `invalidate_pending_plans(id)`, `decide_plan(id,decision)`, `completed_events()`, `close()`；新消息/预览仅替换本会话旧待确认单。未来任务在原授权范围内执行，不因后续聊天而撤销；撤销需明确取消动作。

事件稳定ID、`kind,source,title,text,created_at,plan_id,conversation_id,operation_id,receipt`。任务事件receipt还含task_id/task_status/state_version，版本只增；同task旧事件不得把回执退回早期状态。后续事件指向原授权plan_id，取消计划亦可通过 `related_plan_ids` 更新；模型上下文与用户可见回执保持一致。错误统一BankError。

## TasksService(database_url)：AA与定时（agent一）

目录 `backend/app/tasks/**`、测试 `backend/tests/test_tasks*.py`。`seed_demo()` 不自动造已完成任务。

`create_action_plan(action,conversation_id,*,title=None,account_id=None,total_minor=None,participant_ids=None,recipient_id=None,amount_minor=None,execute_at=None,task_id=None)` -> TaskAction，kind=`task`，action=`create_aa|cancel_aa|schedule_transfer|cancel_schedule`。响应额外 `{task_id?,account_id?,total_minor?,collectable_minor?,participants?,recipient_id?,recipient_name?,amount_minor?,execute_at?,deadline_at?,reserved_minor?}`。

AA：`participant_ids` 为有序、不重复列表，至少2人、最多20人，必须含 `self`；其他ID来自现有当前用户Recipient。不能仅凭姓名猜同名对象。总额按人数均分，余数从列表头依次分配1分；我方份额标self，不记收款。提交请求不扣任何人资金、不记已到账，也不向外部联系人发送消息。

`list_aa()->AA[]`；AA `{id,title,account_id,total_minor,collectable_minor,collected_minor,status:awaiting_payment|partial|completed|cancelled,participants,created_at,conversation_id,state_version}`；participants `{id,name,share_minor,status:self|pending|paid|cancelled,operation_id?}`。

`simulate_payment(task_id,participant_id)->receipt`：明确模拟对方付款，模型不提供此工具。严格从该Recipient的既有虚构账户转入确认绑定的我方账户，金额等于未付份额，不能任意指定金额或扣真实账户；参与者只付一次，重复调用返回原结果。source可用不足无副作用。已取消请求不接受新付款。取消AA只停止未付部分，保留已收款记录，不自动退款。

定时：确认绑定source account、已消歧recipient、正金额、execute_at，执行时间必须大于task clock当前时间且≤365天。审批时预留全额，job `scheduled`；deadline_at=execute_at+24h。`list_schedules()->Schedule[]`；Schedule `{id,account_id,recipient_id,recipient_name,amount_minor,execute_at,deadline_at,status:scheduled|completed|cancelled|expired|failed,hold_id,reserved_minor,operation_id?,message,conversation_id,state_version}`。到期再次检查身份/账户/收款账户/预留；成功消费预留并写双方流水；不确定结果按原operation查询，禁止第二次扣款。失败/过期释放未消费预留；取消只允许尚未执行，重复取消无二次效果。`process_due(as_of=None)->list` 支持固定时钟测试，多个worker不能重复执行。`get_task(id)`返回对应AA或schedule/None。

HTTP：GET `/api/tasks` -> `{aa,schedules,now}`；POST `/api/tasks/preview` 参数同create_action_plan；POST `/api/task-plans/{id}/decision`；POST `/api/tasks/{id}/demo-payment` `{participant_id}`；GET `/api/tasks/clock`；POST `/api/tasks/demo/advance` `{minutes}`（主集成调用两服务）。

## WorkflowService(database_url, *, fault_mode=None)：模拟生日商户（agent二）

目录 `backend/app/workflows/**`、测试 `backend/tests/test_workflows*.py`。读取共用hold/clock；自有表不要跨metadata引用bank FK。`seed_demo()`只创建catalog及独立虚构商户User/Account（不计入我方账户列表）。

`catalog()->Product[]`：`flowers-basic` 花间花坊·生日花束19900分；`cake-basic` 云朵烘焙·生日蛋糕12900分；CNY，版本1，手续费0，一份/项，最多5份。字段 `{id,merchant_id,merchant_name,name,price_minor,currency,version,cancel_supported:true}`。确认前报价TTL10分钟；确认时锁定价格/数量/预算/对象/时间，未来执行须核验目录版本，变价须重新确认，不能超预算自动补款。

`create_action_plan(action,conversation_id,*,account_id=None,recipient_label=None,budget_minor=None,items=None,execute_at=None,task_id=None)` -> WorkflowAction；kind=`workflow` action=`arrange|cancel`。items为 `{product_id,quantity}` 列表，最多两种且不可重复。默认execute_at=当前task时间（即时），未来≤365天。收件人标签仅模拟，不收集真实配送地址/电话。

响应额外 `{task_id?,account_id,recipient_label,budget_minor,total_minor,reserved_minor,execute_at,deadline_at,items:[{product_id,merchant_name,name,quantity,unit_price_minor,total_minor}],cancellation_notice}`。确认只预留实际报价total，不预留整个budget，也不声称下单已成功。同参数待确认后修改必须新预览。

`list_tasks()->Workflow[]`, `get_task(id)->Workflow|None`；Workflow `{id,title,recipient_label,account_id,budget_minor,total_minor,reserved_minor,paid_minor,refunded_minor,refund_pending_minor,net_spent_minor,status:scheduled|running|completed|cancelled|failed|partial|unknown|refund_pending|expired,execute_at,deadline_at,hold_id,steps,conversation_id,state_version,message}`。steps展示商户/项目/金额、状态、订单ID及说明。成功表示订单已确认，不表示已配送；明确每一项实际结果。

`process_due(as_of=None)->list`：按依赖逐步处理。每个商户订单有稳定业务键(task_id,itemIndex)、固定付款operation。支持丢回执：已存订单/付款但任务状态unknown，下轮查询原订单恢复，禁止重下单/再扣款。下单及付款为同一事务；每个步骤单独持久化，以便观察/恢复部分完成，不能把整个多商户过程伪装为一个全回滚事务。

任一订单失败即停止新下单、释放未消费预留，并按已确认范围取消/退款已成功的可取消订单。退款失败持久化refund_pending及资金归属，不能报告全部退回。`recover(task_id)->Workflow`只核对已有订单/执行已授权补偿，不新增消费；可重复调用。用户取消未执行安排释放全部；取消已下单安排按订单状态补偿，已履约拒绝退款则准确partial。

模拟fixture fault_mode：`None`正常；`cake_out_of_stock`第二商户失败且首笔可退款；`response_lost`首单付款成功但一次回执丢失，下轮按原订单恢复；`refund_failure`第二商户失败且首笔退款暂失败，recover第一次明确重试后可恢复。若测试已履约，测试工具可将自有订单标fulfilled，取消应保留不可退资金，不虚构成功。fault_mode不由模型工具提供、不默认加在用户主流程；测试在隔离库注入。

商户消费描述固定 `模拟商户消费·商户名`，退款 `模拟商户退款·商户名`；主集成将其映射到账单消费/退款，不能按有operation_id一律归转账。`completed_events()`输出受理、进度、完成/失败/退款事件，持久稳定ID和递增版本。

HTTP：GET `/api/workflows` -> `{catalog,tasks,now}`；POST `/api/workflows/preview`；POST `/api/workflow-plans/{id}/decision`；POST `/api/workflows/{id}/recover`（只在已授权补偿范围内）。

## 开发与验收

主集成先提供reservation与clock接口，补现有转账/理财可用余额检查，再接任务HTTP、后台tick、DS工具、知识、统一动作与通知。AA/定时agent与商户联动agent不改bank/main/agent/web；前端设计agent使用Astra medium，只出 `.local/p3b-design/` 图稿，批准后才改web。

验收包括AA三人100分的舍入和部分收款/重复付款/取消后不收；预留与即时转账/理财争用余额；定时不提前/到期一次/重启补执行/窗口过期/撤销；商户预算、部分失败退款、回执丢失核对、退款失败恢复、重复worker和资金守恒；真实DS与页面完整路径。所有PG检查只准127.0.0.1:55432/concierge_test，独占协调，不重置应用库。图稿日期锚2026-10-13。
