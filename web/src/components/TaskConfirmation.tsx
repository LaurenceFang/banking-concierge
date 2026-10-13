import { useEffect, useState } from "react";
import { getTaskRecipients, money } from "../api";
import { availableMoney, beijingTime, participantStatus } from "../task-utils";
import type { Account, TaskAction, TaskRecipient } from "../types";
export function TaskConfirmation({action,accounts}:{action:TaskAction;accounts:Account[]}) {
  const [recipients,setRecipients]=useState<TaskRecipient[]>([]);
  useEffect(()=>{let active=true;void getTaskRecipients().then(result=>{if(active)setRecipients(result.recipients);}).catch(()=>{});return()=>{active=false;};},[]);
  const account=accounts.find(x=>x.id===action.account_id);
  const person=recipients.find(x=>x.id===action.recipient_id);
  const aa=action.action==="create_aa" || action.action==="cancel_aa";
  const cancel=action.action==="cancel_aa" || action.action==="cancel_schedule";
  return <div className="task-confirmation">
    <div className="amount-block"><span className="muted">{aa?"AA总额":"转账金额"}</span><p>{money((aa?action.total_minor:action.amount_minor)??0)}</p></div>
    <dl className="transfer-fields">
      <div><dt>{aa?"收款账户":"付款账户"}</dt><dd>{account?.name??action.account_id??"所选账户"}</dd></div>
      {aa ? <><div><dt>应收</dt><dd>{money(action.collectable_minor??0)}</dd></div><div><dt>自担</dt><dd>{money((action.total_minor??0)-(action.collectable_minor??0))}</dd></div></> : <>
        <div><dt>收款人</dt><dd>{person?`${person.name} · ${person.bank_name} · ${person.account_hint}`:action.recipient_name}</dd></div>
        {account && <><div><dt>账面余额</dt><dd>{money(account.balance_minor)}</dd></div><div><dt>账户预留</dt><dd>{money(account.reserved_minor??0)}</dd></div><div><dt>当前可用</dt><dd>{money(availableMoney(account))}</dd></div></>}
        <div><dt>执行时间</dt><dd>{beijingTime(action.execute_at)}<small>北京时间</small></dd></div><div><dt>窗口截止</dt><dd>{beijingTime(action.deadline_at)}<small>北京时间</small></dd></div>
        <div><dt>{cancel?"将释放预留":"确认后预留"}</dt><dd>{money(action.reserved_minor??action.amount_minor??0)}</dd></div>
      </>}
    </dl>
    {aa && <div className="task-confirm-participants">{action.participants?.map(participant=>{const contact=recipients.find(x=>x.id===participant.id);return <div className="stat-row" key={participant.id}><span>{participant.id==="self"?"我":contact?`${contact.name} · ${contact.account_hint}`:participant.name}<small>{participantStatus[participant.status]}</small></span><span>{money(participant.share_minor)}</span></div>;})}</div>}
    <p className="muted small">{action.action==="create_aa"?"创建请求不会扣款，不代表实际到账；本人份额不计入应收。":action.action==="cancel_aa"?"仅停止未付请求，保留已收款记录，不自动退款。":action.action==="schedule_transfer"?"确认后预留资金，不立即扣款；执行前可以撤销。重启后在授权窗口内补执行，超时释放预留。":"撤销尚未执行的安排，释放未使用的预留资金。"}</p>
  </div>;
}
