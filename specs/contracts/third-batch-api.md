# 第三批 A：卡片与理财接口

2026-10-12 冻结；关联 M19 卡片部分、M20。用户先授权后端与样图，三张新增界面稿随后全部获用户认可并授权实现。主运行应用显式启用 P3 模型工具与共享抽屉；`create_app(..., enable_p3_actions=False)` 的工厂默认兼容旧界面测试，隔离测试可显式启用。此批为卡片与理财，不能称六场景完成。

## 共用

金额固定 CNY 整数分，模型只能查询或创建预览，无批准工具。新动作都包含 `{id,kind,action,title,description,status,expires_at,conversation_id}`；`status=pending/completed/declined/invalidated/expired`。卡片 `kind=card`，理财 `kind=wealth`。业务单据状态与计划状态分别返回。确认结果 `{status,message,operation_id?,code?,conversation_id?}`，已受理但待结算的理财操作另含 `order_id,order_status`，不得描述为已到账。每个会话最多一张待确认计划；新消息/新预览只失效本会话的旧计划，其他会话独立。副作用与结果持久化、重复确认幂等。

两服务共有：`get_plan(id)`, `pending_for_conversation(id)`, `invalidate_pending_plans(id)`, `decide_plan(id,decision)`, `completed_events()`, `close()`。事件使用稳定 `card:{operation_id}` 或 `wealth:{operation_id}`，包含 `plan_id,conversation_id,operation_id,title,text,kind,source,created_at,receipt`。结算事件另以 `wealth-settlement:{order_id}` 唯一，`plan_id` 可空。

错误统一 `BankError(code,message)`。输入拒绝不能写业务状态或改变余额，内部业务错误与 API 422/409 使用现有封装。新表各自命名空间，不能重建或清空应用银行表。

## 卡片：CardService(database_url)

- `seed_demo()`：两张虚构卡。`demo-card-debit-001` 日常借记卡，账户 `demo-checking-001`，尾号 6028；`demo-card-credit-001` 轻享信用卡，尾号 7391，信用额度 2,000,000 分，已用额度 85,000 分。初始均 active；默认单日消费限额借记卡 500,000 分、信用卡 300,000 分。
- `list_cards()` -> Card[]。Card `{id,name,kind:debit|credit,last4,account_id,status:active|frozen|lost|closed,daily_limit_minor,credit_limit_minor:null|int,outstanding_minor,available_credit_minor:null|int,currency,version}`。不返回完整卡号。
- `list_applications()` -> Application[]。Application `{id,card_kind,status:submitted,submitted_at,operation_id}`。提交申请仅产生申请记录，不自动发卡或虚称审批通过。
- `create_action_plan(action, conversation_id, card_id=None, amount_minor=None, card_kind=None)` -> CardAction。
- action 枚举 `freeze,unfreeze,report_lost,set_daily_limit,set_credit_limit,apply_card`。普通解锁只允许 frozen→active，lost/closed 不能解锁；挂失允许 active/frozen→lost。已冻结/已挂失或未变金额不能制造重复业务效果。申请 `card_kind=debit|credit`。
- 规则基线：单日限额允许 10,000–2,000,000 分；信用额度允许 300,000–5,000,000 分，不得低于已用额度，不适用于借记卡。都是模拟规则，不是银行授信政策。额度变化预览绑定当前卡片版本，确认时再查；同一版本的竞争动作不能覆盖状态改变。
- CardAction 额外 `{card_id:null|str,card_name:null|str,card_kind:null|str,amount_minor:null|int,before:object,after:object}`；before/after 只包含相关可读业务事实。
- `authorize_charge(card_id,amount_minor)` 只读校验渠道状态和额度，返回 `{allowed:bool,code?,message}`，用于证明冻结/挂失会阻止卡片渠道，不写资金。冻结卡片不冻结整个存款账户，不能误阻断已有账户转账；独立模拟刷卡扣款不进入本批范围。
- HTTP：GET `/api/cards` -> `{cards,applications}`；POST `/api/cards/preview` 请求 `{action,conversation_id,card_id?,amount_minor?,card_kind?}`；POST `/api/card-plans/{id}/decision` 请求 `{decision,conversation_id?}`。

## 理财：WealthService(database_url)

- `seed_demo()`：固定三个自写模拟产品；不得承诺真实收益。`demo-wealth-flex` 灵活资金计划 R1，起购100,000分，申购/赎回费0，锁定0天；`demo-wealth-balance` 平衡180计划 R3，起购500,000分，申购费30bps、赎回费10bps，锁定180自然日；`demo-wealth-growth` 成长365计划 R5，起购1,000,000分，申购费50bps、赎回费20bps，锁定365自然日。均按模拟固定净值1元、业务日T+1结算；费用按正整数分四舍五入。
- `list_products()` -> Product[]。Product `{id,name,risk_level:1..5,min_purchase_minor,purchase_fee_bps,redemption_fee_bps,lock_days,settlement_business_days:1,nav_minor:100,currency,rule_version,description,eligible:bool}`；无风险档案时 eligible=false，不能擅自按最低风险放行。
- `get_risk_profile()` -> RiskProfile|null；`set_risk_profile(answers:dict)` -> RiskProfile。
- answers 必填：`horizon:short|medium|long`, `loss_tolerance:none|low|moderate|high`, `experience:none|basic|experienced`, `liquidity_need:high|medium|low`, `objective:preserve|balanced|growth`。缺项或未知值拒绝，所有问题由用户回答。RiskProfile `{answers,risk_level:1..5,risk_label,rule_version,assessed_at,explanation}`。规则分数：horizon 0/1/2，loss 0/1/3/5，experience 0/1/2，liquidity 0/1/2，objective 0/1/2；总分≤2:R1、≤5:R2、≤8:R3、≤11:R4、其余R5。loss=none 强制R1；experience=none 上限R2；horizon=short 且 liquidity=high 上限R1。用画像限制产品风险，而非推荐或预测收益。
- `list_positions()` -> Position[]；Position `{id,product_id,product_name,principal_minor,available_minor,redemption_pending_minor,currency,unlock_on,risk_level}`。pending申购不计已持仓；赎回受理后锁定对应份额，未到账现金不可用。
- `list_orders()` -> Order[]；Order `{id,action:purchase|redeem,product_id,position_id:null|str,amount_minor,fee_minor,total_minor,payout_minor,status:pending_settlement|settled,requested_on,settles_on,settled_at:null|str,operation_id,conversation_id}`。
- `create_action_plan(action, conversation_id, product_id=None, account_id=None, position_id=None, amount_minor=None)` -> WealthAction；action=`purchase|redeem`。申购必须有合格风险档案；赎回校验锁定日/可用份额。WealthAction 额外 `{product_id,product_name,account_id,position_id:null|str,amount_minor,fee_minor,total_minor,payout_minor,settles_on,risk_level,rule_version}`。
- 申购确认：原子从当前用户付款账户扣 `amount+fee`、清算账户入账并创建待结算订单。无现金扣款时不得生成成功订单。T+1后才创建已持仓；金额调整/风险画像变化/余额竞争在提交时重查。
- 赎回确认：原子预占持仓对应本金份额并创建待结算订单；T+1前不向银行账户加钱。结算时原子释放份额、清算账户转入用户实际 `amount-fee`、记录两侧流水及最终订单状态。重复结算不产生第二笔资金变化。
- 清算账户使用独立虚构所有者（不能出现在用户的两个账户列表中）；可引用现有 bank.models，在同一 SQLAlchemy 事务中更新账户/流水/订单，不能调用分离事务造成半扣款。查询接口不扣钱。
- `process_due(as_of=None)` -> newly settled Order[]；可恢复到期未结算记录。服务端持久化独立模拟业务日期，不改 OS 时钟。`advance_demo_clock(business_days:int)` -> `{as_of,settled_orders}`，范围1..365，跳过周末，结算在持久化日期后运行且重启可恢复；`current_date()` -> ISO date。仅模拟测试能力，正式UI呈现须另经样图认可。
- HTTP：GET `/api/wealth` -> `{products,risk_profile,positions,orders,as_of}`；POST `/api/wealth/risk-profile` `{answers}`；POST `/api/wealth/preview` 参数同create_action_plan；POST `/api/wealth-plans/{id}/decision` `{decision,conversation_id?}`；POST `/api/wealth/demo/advance` `{business_days}`。

## 主集成与验收

卡片 agent 只改 `backend/app/cards/**`、`backend/tests/test_cards*.py`；理财 agent 只改 `backend/app/wealth/**`、`backend/tests/test_wealth*.py`。主集成负责 app/main.py、agent.py、共享会话/事件、知识库和本批API测试。前端 agent 在样图认可前只出图；用户认可后拥有 web/** 实现与浏览器验收，保留已审设计及 P2 能力。

除服务离线测试，还检查 API错误/会话绑定/动作替换、真实DS查询和预览（隔离库启用P3）、PostgreSQL账户与持仓并发/事务回滚、风险不足、挂失不可普通解锁、申请≠获批、申购受理≠持仓、赎回受理≠到账、重启与重复结算。旧P2的82后端/9前端检查保留；新增前端待用户看样图认可。
