import { useState, useRef, useEffect } from "react";
import type { Account, WorkflowAction } from "../types";
import { money, parseMinor, reviseWorkflow } from "../api";
import { beijingTime, shanghaiInput } from "../task-utils";
import "./workflow-revision.css";
export function WorkflowConfirmation({action,accounts,conversationId,disabled,onEditing,onDirty,onBusy,onRevised}:{action:WorkflowAction;accounts:Account[];conversationId:string;disabled:boolean;onEditing:(v:boolean)=>void;onDirty:(v:boolean)=>void;onBusy:(v:boolean)=>void;onRevised:(v:WorkflowAction)=>void}){
 const [editing,setEditing]=useState(false),[saving,setSaving]=useState(false),[error,setError]=useState("");
 const [recipient,setRecipient]=useState(action.recipient_label),[budget,setBudget]=useState(String(action.budget_minor/100)),[account,setAccount]=useState(action.account_id);
 const [immediate,setImmediate]=useState(action.execute_immediately===true);
 const [time,setTime]=useState(action.execute_at?new Date(new Date(action.execute_at).getTime()+8*3600000).toISOString().slice(0,16):"");
 const [items,setItems]=useState(action.items);const lock=useRef(false);
 const originalTime=action.execute_at?new Date(new Date(action.execute_at).getTime()+8*3600000).toISOString().slice(0,16):"";
 const itemChanges=items.length!==action.items.length||items.some((item,i)=>item.product_id!==action.items[i]?.product_id||item.quantity!==action.items[i]?.quantity);
 const dirty=editing&&(recipient.trim()!==action.recipient_label||budget!==String(action.budget_minor/100)||account!==action.account_id||immediate!==(action.execute_immediately===true)||(!immediate&&time!==originalTime)||itemChanges);
 const errorRef=useRef<HTMLParagraphElement>(null);
 useEffect(()=>{onDirty(dirty);},[dirty,onDirty]);
 useEffect(()=>{if(error){errorRef.current?.focus();errorRef.current?.scrollIntoView({block:"nearest"});}},[error]);
 useEffect(()=>{if(!dirty&&!saving)return;const unload=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue="";};window.addEventListener("beforeunload",unload);return()=>window.removeEventListener("beforeunload",unload);},[dirty,saving]);
 function edit(v:boolean){setEditing(v);onEditing(v);setError("");}
 async function save(){if(lock.current||disabled)return;lock.current=true;setSaving(true);onBusy(true);setError("");try{
  if(!recipient.trim())throw new Error("请填写安排对象。");
  if(!items.length||items.some(i=>!Number.isSafeInteger(i.quantity)||i.quantity<1||i.quantity>5))throw new Error("至少保留一项服务，每项数量须为1至5的整数。");
  const changes:import("../api").WorkflowRevisionInput={conversation_id:conversationId};
  if(recipient.trim()!==action.recipient_label)changes.recipient_label=recipient.trim();
  const amount=parseMinor(budget);if(amount!==action.budget_minor)changes.budget_minor=amount;
  if(account!==action.account_id)changes.account_id=account;
  if(immediate!==(action.execute_immediately===true)||(!immediate&&time!==originalTime))changes.execute_at=immediate?null:shanghaiInput(time);
  if(itemChanges)changes.items=items.map(({product_id,quantity})=>({product_id,quantity}));
  if(Object.keys(changes).length===1){edit(false);return;}
  const result=await reviseWorkflow(action.id,changes);
  if(result.id===action.id)throw new Error("确认单未更新，请刷新会话核对。");
  onRevised(result);
 }catch(e){setError(e instanceof Error?e.message:"修改未保存，请重试。");}finally{lock.current=false;setSaving(false);onBusy(false);}}
 return <div className="workflow-confirmation workflow-revision">
 {action.revision&&<section className="revision-summary"><h3>变更汇总</h3>{action.revision.changes.map(change=><p key={change.field}>{change.summary}</p>)}{action.revision.before_total_minor!==undefined&&action.revision.after_total_minor!==undefined&&<p>原报价 {money(action.revision.before_total_minor)} → 现 {money(action.revision.after_total_minor)}</p>}</section>}
 <div className="amount-block"><span className="muted">{action.action==="cancel"?"原方案报价":"当前报价"}</span><p>{money(action.total_minor)}</p></div>
 {editing?<form onSubmit={e=>{e.preventDefault();void save();}}><fieldset disabled={saving||disabled}><legend>修改安排</legend>
 <label>对象<input value={recipient} onChange={e=>setRecipient(e.target.value)}/></label>
 <label>预算（元）<input inputMode="decimal" value={budget} onChange={e=>setBudget(e.target.value)}/></label>
 <label>付款账户<select value={account} onChange={e=>setAccount(e.target.value)}>{accounts.map(a=><option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
 <label>执行方式<select value={immediate?"now":"scheduled"} onChange={e=>setImmediate(e.target.value==="now")}><option value="now">确认后立即开始</option><option value="scheduled">指定时间</option></select></label>
 {!immediate&&<label>执行时间（北京时间）<input type="datetime-local" value={time} onInput={e=>setTime(e.currentTarget.value)} onChange={e=>setTime(e.target.value)}/></label>}
 {items.map(item=><div className="revision-item" key={item.product_id}><label>{item.name}<input aria-label={`${item.name}数量`} type="number" min="1" max="5" step="1" value={item.quantity} onChange={e=>setItems(rows=>rows.map(r=>r.product_id===item.product_id?{...r,quantity:Number(e.target.value)}:r))}/></label><button type="button" className="text-button" onClick={()=>setItems(rows=>rows.filter(r=>r.product_id!==item.product_id))}>移除</button></div>)}
 <p className="muted small">保存后重新获取报价，确认前不会预留资金或下单。</p>
 <button className="button primary" type="submit">{saving?"正在更新报价…":"保存并更新报价"}</button><button className="button secondary" type="button" onClick={()=>{setRecipient(action.recipient_label);setBudget(String(action.budget_minor/100));setAccount(action.account_id);setItems(action.items);setImmediate(action.execute_immediately===true);setTime(action.execute_at?new Date(new Date(action.execute_at).getTime()+8*3600000).toISOString().slice(0,16):"");edit(false);}}>取消修改</button>
 </fieldset></form>:<><h3>安排详情</h3><dl className="transfer-fields"><div><dt>对象</dt><dd>{action.recipient_label}</dd></div><div><dt>预算</dt><dd>{money(action.budget_minor)}</dd></div><div><dt>付款账户</dt><dd>{accounts.find(a=>a.id===action.account_id)?.name??action.account_id}</dd></div><div><dt>执行</dt><dd>{action.execute_immediately?"确认后立即开始":beijingTime(action.execute_at??undefined)}</dd></div></dl>
 <div className="workflow-confirm-items">{action.items.map(item=><div key={item.product_id}><span>{item.name}<small>{item.merchant_name} · {money(item.unit_price_minor)} × {item.quantity}{item.unit||"份"}</small></span><span>{money(item.total_minor)}</span></div>)}</div>
 {action.action==="arrange"&&<button className="button secondary" disabled={disabled} onClick={()=>edit(true)}>继续修改</button>}</>}
 {error&&<p ref={errorRef} tabIndex={-1} role="alert" className="inline-error">{error}</p>}
 <details className="revision-notes"><summary>资金预留与取消说明</summary><p>{action.cancellation_notice}</p><p>确认后仅预留报价金额。某项失败时停止后续下单，释放未消费预留，并在授权范围内取消、退款可取消订单。退款以实际到账为准。</p><p>报价有效至 {beijingTime(action.expires_at)}（北京时间）。窗口截止 {beijingTime(action.deadline_at??undefined)}。</p></details>
 </div>;
}
