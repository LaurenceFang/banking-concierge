# 四入口与共享对话增量契约

日期：2026-10-13。依据：已审四状态v2样稿和[交互SPEC](../14-共享对话与工作区交互SPEC.md)。状态：本轮核心接口已实现，见[验收记录](../15-四入口工作区实施状态.md)。保留现有业务API，导航重组不新增四套账本或聊天存储。

## 范围键

稳定字符串`domain/section`用于列表筛选与首条消息上下文：

| 一级 | 范围键 | 本轮页面 |
|---|---|---|
| 账户 | accounts/overview、accounts/accounts、accounts/cards | 资金总览、资金账户、银行卡 |
| 账单 | bills/entries、bills/payments、bills/analysis、bills/import | 收支明细、收付款、分析、导入整理 |
| 财富 | wealth/positions、wealth/products、wealth/orders、wealth/risk | 持仓、产品比较、订单、风险档案 |
| 生活 | life/subscriptions | 订阅与代扣 |

主页面不传scope，展示全部普通对话。未实现安排、借贷等页面不显示；完整产品地图中的预算、日常缴费等能力不据此声称可用。

## 列表与首次发送

- `GET /api/conversations`：保留全局历史；返回固定主对话与普通对话。
- `GET /api/conversations?scope=bills/payments`：返回固定主对话与该范围普通对话。主对话用于固定入口，不混入普通列表。
- 摘要保留`id/title/kind/updated_at/unread_count`，新增`scopes: string[]`。`kind=temporary`仅作为兼容旧数据的内部枚举，用户界面统一称“对话”。
- `POST /api/chat`与`POST /api/chat/stream`增量接受`scope?: string`、`client_request_id?: string`；原`message`及`conversation_id?`不变。
- 打开“新建对话”只建前端编辑态，不发创建请求。首条发送不传conversation_id，由服务端为同一client_request_id确定唯一对话并持久化用户消息及范围。安全重试不能新增同一条用户消息或重复执行模型已完成的回合。
- SSE可在最终result之前发送`conversation`事件，数据为`{conversation_id}`；前端尽早认领持久ID，最终响应仍返回conversation_id。断线不自动重复POST。
- `retry_failed?: boolean`：用户明确点击“重试回复”时为true，复用同一client_request_id、message、scope。仅已失败回合可恢复处理；原用户消息不再追加，失败前已创建的确认单复用。普通同key重试回放缓存失败或结果；请求标识冲突不会把原请求标记为失败。
- `POST /api/conversations`保留用于真正发起卡片／理财／订阅预览的旧接口，接受可选scope、client_request_id；仅浏览业务页不调用。
- `GET /api/conversations/{id}`与重命名、已读接口沿用。主页与范围左栏点击的是同一id，不复制消息。

## 关联与恢复

显式范围和已知业务工具结果可建立关联。转账预览归bills/payments，卡片归accounts/cards，订阅归life/subscriptions，理财按对象归相应页。已有历史不得删除；能根据历史真实动作识别归属时做幂等补关联，不能用标题关键词推断真实资金行为。

原对话的网络请求、模型回复和确认操作始终绑定原id。切页只改变视图，不能把迟到回复附到新对话。左栏列表按scope标识请求版本，旧范围响应不得覆盖当前列表。

只读询问保留待确认单；创建新的办理参数会替换原预览并令旧单失效。现有业务使用plan id承担确认快照身份，本轮不假装已经实现任意参数表单编辑与完整通用draft引擎。

## 本轮兼容与后续

修复暂停P3-B造成的启动参数不匹配；未批准的AA、定时与联动不出现在可操作页面。开发期间不得因启动工作区而自动执行暂停任务。设置、归档、用户改分类、通用对象深链接和完整多草稿编辑若没有实际实现与验收，继续标为后续，不以规划条目充当完成项。
