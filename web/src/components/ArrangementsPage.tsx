import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { decideAction, getWorkflows, money, parseMinor, previewWorkflow, recoverWorkflow } from "../api";
import { availableMoney, beijingTime, shanghaiInput } from "../task-utils";
import { canCancelWorkflow, canRecoverWorkflow, mergeWorkflowVersions, workflowOutcome, workflowStatus, workflowStepStatus } from "../workflow-utils";
import type { Account, Action, WorkflowAction, WorkflowList, WorkflowTask, ManagedService } from "../types";
import "../arrangements.css";
import { type OpenManagement } from "./ManagementPanel";
interface Props {
 onManage:OpenManagement;managementBlocked:boolean;
 accounts:Account[];revision:number;blocked:boolean;pending:Action|null;
 ensureConversation:(existingId?:string)=>Promise<string>;onAction:(action:Action)=>void;
 onClearAction:(id:string)=>void;onBusy:(busy:boolean)=>void;onRefresh:()=>void;onContinue:(id:string)=>void;
}
interface FormState {title:string;order:string[];recipient:string;budget:string;account:string;timing:string;executeAt:string;quantities:Record<string,number>}
const blank:FormState={title:"",order:[],recipient:"",budget:"",account:"",timing:"now",executeAt:"",quantities:{}};
function readForm():FormState{try{return {...blank,...JSON.parse(localStorage.getItem("yx2.arrangements.form.v1")??"{}")};}catch{return blank;}}
export function ArrangementsPage(props:Props){
 const {accounts,revision,blocked,pending,ensureConversation,onAction,onClearAction,onBusy,onRefresh,onContinue,onManage,managementBlocked}=props;
 const [data,setData]=useState<WorkflowList|null>(null);const [form,setForm]=useState(readForm);
 const [selected,setSelected]=useState("");const [creating,setCreating]=useState(false);const [filter,setFilter]=useState("all");
 const [error,setError]=useState("");const [notice,setNotice]=useState("");const [busy,setBusy]=useState(false);
 const alive=useRef(true);const lock=useRef(false);const version=useRef(0);const dataRef=useRef<WorkflowList|null>(null);
 const propsRef=useRef(props);propsRef.current=props;const previewConversation=useRef("");const formConversation=useRef("");const formPreview=useRef<WorkflowAction|null>(null);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;++version.current;};},[]);
 useEffect(()=>{try{localStorage.setItem("yx2.arrangements.form.v1",JSON.stringify(form));}catch{}},[form]);
 const refresh=useCallback(async()=>{
  const request=++version.current;
  try{const incoming=await getWorkflows();if(!alive.current||request!==version.current)return;
   const merged=mergeWorkflowVersions(dataRef.current,incoming);
   const signature=(value:WorkflowList)=>value.tasks.map(task=>`${task.id}:${task.state_version}`).join("|");
   const changed=dataRef.current && signature(dataRef.current)!==signature(merged);
   dataRef.current=merged;setData(merged);
   if(previewConversation.current){const task=merged.tasks.find(item=>item.conversation_id===previewConversation.current);if(task){setSelected(task.id);setCreating(false);previewConversation.current="";}}
   if(changed)propsRef.current.onRefresh();
  }catch(failure){if(alive.current&&request===version.current)setError(failure instanceof Error?failure.message:"无法读取安排。");}
 },[]);
 useEffect(()=>{void refresh();const tick=()=>{if(!document.hidden&&!lock.current)void refresh();};const timer=window.setInterval(tick,10000);document.addEventListener("visibilitychange",tick);return()=>{clearInterval(timer);document.removeEventListener("visibilitychange",tick);++version.current;};},[refresh,revision]);
 const task=data?.tasks.find(item=>item.id===selected);
 const accountId=form.account || accounts.find(item=>item.kind==="checking")?.id || "";
 const account=accounts.find(item=>item.id===accountId);
 const orderedIds=[...new Set([...form.order,...(data?.catalog.map(x=>x.id)??[])])];
 const products=orderedIds.flatMap(id=>data?.catalog.find(x=>x.id===id)??[]);
 const chosen=products.filter(product=>(form.quantities[product.id]??0)>0);
 const subtotal=chosen.reduce((sum,product)=>sum+product.price_minor*form.quantities[product.id],0);
 const draft=pending?.kind==="workflow" && pending.action==="arrange" && pending.id===formPreview.current?.id?pending:null;
 const disabled=busy||blocked;const editingDisabled=disabled||!!draft;
 function update<K extends keyof FormState>(key:K,value:FormState[K]){setForm(previous=>({...previous,[key]:value}));}
 async function run(operation:()=>Promise<void>){if(lock.current||blocked)return;lock.current=true;setBusy(true);onBusy(true);setError("");setNotice("");try{await operation();}catch(failure){if(alive.current)setError(failure instanceof Error?failure.message:"操作未完成，请重试。");}finally{lock.current=false;if(alive.current){setBusy(false);onBusy(false);}}}
 function chooseService(item:ManagedService){setData(old=>old?{...old,catalog:[...old.catalog.filter(x=>x.id!==item.id),item]}:old);setForm(old=>({...old,quantities:{...old.quantities,[item.id]:1},order:[...old.order.filter(id=>id!==item.id),item.id]}));}
 function selectService(id:string,checked:boolean){if(checked&&chosen.length>=10){setError("每个方案最多选择10种服务。");return;}setForm(old=>({...old,quantities:{...old.quantities,[id]:checked?1:0},order:checked?[...old.order.filter(x=>x!==id),id]:old.order.filter(x=>x!==id)}));}
 function moveService(id:string,direction:number){const ids=chosen.map(x=>x.id);const from=ids.indexOf(id);const to=from+direction;if(to<0||to>=ids.length)return;[ids[from],ids[to]]=[ids[to],ids[from]];update("order",ids);}
 function preview(){
  try{
   const budget=parseMinor(form.budget);if(!form.recipient.trim())throw new Error("请填写对象称呼。");if(!account)throw new Error("请选择付款账户。");if(!chosen.length)throw new Error("请选择至少一项模拟服务。");
   if(chosen.length>10)throw new Error("每个方案最多选择10种服务。");
   if(chosen.some(product=>!Number.isInteger(form.quantities[product.id])||form.quantities[product.id]<1||form.quantities[product.id]>5))throw new Error("每项数量须为1至5份。");
   if(subtotal>budget)throw new Error("所选服务报价超过预算，请调整数量或预算。");if(subtotal>availableMoney(account))throw new Error("可用余额不足以预留当前报价。");
   const execute=form.timing==="later"?shanghaiInput(form.executeAt):undefined;
   if(execute && data && Date.parse(execute)<=Date.parse(data.now))throw new Error("指定时间须晚于当前服务时间。");
   void run(async()=>{const conversation=await ensureConversation(formConversation.current||undefined);const action=await previewWorkflow({action:"arrange",conversation_id:conversation,account_id:accountId,recipient_label:form.recipient.trim(),title:form.title.trim()||undefined,budget_minor:budget,items:chosen.map(product=>({product_id:product.id,quantity:form.quantities[product.id]})),...(execute?{execute_at:execute}:{})});if(alive.current){previewConversation.current=conversation;formConversation.current=conversation;formPreview.current=action;onAction(action);}});
  }catch(failure){setError(failure instanceof Error?failure.message:"请核对方案。");}
 }
 function cancel(task:WorkflowTask){void run(async()=>{const conversation=await ensureConversation(task.conversation_id);const action=await previewWorkflow({action:"cancel",conversation_id:conversation,task_id:task.id});if(alive.current)onAction(action);});}
 function recover(task:WorkflowTask){void run(async()=>{await ensureConversation(task.conversation_id);const updated=await recoverWorkflow(task.id);if(alive.current){if(dataRef.current){const next=mergeWorkflowVersions(dataRef.current,{...dataRef.current,tasks:dataRef.current.tasks.map(item=>item.id===updated.id?updated:item)});dataRef.current=next;setData(next);}setNotice(workflowOutcome(updated));await refresh();onRefresh();}});}
 function edit(){if(!draft)return;void run(async()=>{await decideAction(draft,"decline",draft.conversation_id);if(alive.current){onClearAction(draft.id);previewConversation.current="";setNotice("旧确认已撤销，可修改方案后重新核对。");}});}
 const back=()=>{setCreating(false);setSelected("");setError("");};
 return <section className="data-page arrangements-page" aria-label="我的安排">
  <div className="arrangements-heading"><h2>我的安排</h2>{!creating && !task && <button className="button secondary" disabled={disabled} onClick={()=>{setCreating(true);setError("");formConversation.current="";formPreview.current=null;}}>新建安排</button>}{(creating||task)&&<button className="text-button" onClick={back}><ArrowLeft size={15}/> 返回安排列表</button>}</div>
  {error && <p className="inline-error" role="alert">{error} <button className="text-button" onClick={()=>{setError("");void refresh();}}>重新读取</button></p>}
  {notice && <p className="arrangement-notice" role="status">{notice}</p>}
  {creating ? <form className="arrangement-form" onSubmit={event=>{event.preventDefault();preview();}}>
   <fieldset disabled={editingDisabled}>
    <label>事项名称<input maxLength={120} placeholder="例如：节日安排（可留空）" value={form.title} onChange={e=>update("title",e.target.value)}/></label>
    <label>对象称呼<input required maxLength={120} placeholder="例如：家人" value={form.recipient} onChange={e=>update("recipient",e.target.value)}/></label>
    <label>预算（元）<input required inputMode="decimal" value={form.budget} onChange={e=>update("budget",e.target.value)}/></label>
    <label>付款账户<select required value={accountId} onChange={e=>update("account",e.target.value)}><option value="" disabled>请选择账户</option>{accounts.map(item=><option key={item.id} value={item.id}>{item.name} · 可用 {money(availableMoney(item))}</option>)}</select></label>
    <label>执行方式<select value={form.timing} onChange={e=>update("timing",e.target.value)}><option value="now">确认后开始</option><option value="later">指定时间</option></select></label>
    {form.timing==="later" && <label>执行时间（北京时间）<input required type="datetime-local" value={form.executeAt} onChange={e=>update("executeAt",e.target.value)}/></label>}
    <p className="muted small">当前服务时间 {beijingTime(data?.now)}（北京时间）。对象称呼仅用于模拟，不采集配送信息。</p>
    <div className="management-entry-heading"><h3>选择模拟服务</h3><div className="management-entry-actions"><button type="button" className="text-button" disabled={editingDisabled||managementBlocked||chosen.length>=10} onClick={()=>onManage({kind:"services",add:true,onSelect:chooseService})}>添加服务</button><button type="button" className="text-button" disabled={editingDisabled||managementBlocked} onClick={()=>onManage({kind:"services"})}>管理服务</button></div></div><p className="muted small">每个方案最多10种服务，每种1–5份。按所选顺序执行。</p>
    {products.map(product=><div key={product.id}><div className="arrangement-product"><label className="arrangement-product-choice"><input type="checkbox" checked={(form.quantities[product.id]??0)>0} onChange={e=>selectService(product.id,e.target.checked)}/><span>{product.name}<small>{product.merchant_name} · {product.cancel_supported?"按订单状态核对取消":"不支持取消"}</small></span></label><span>{money(product.price_minor,product.currency)} / {product.unit||"份"}</span><label className="arrangement-quantity">数量<input type="number" min={1} max={5} step={1} aria-label={`${product.name}数量`} disabled={!(form.quantities[product.id]>0)} value={form.quantities[product.id]||1} onChange={e=>update("quantities",{...form.quantities,[product.id]:Number(e.target.value)})}/></label><output>{money(product.price_minor*(form.quantities[product.id]??0))}</output></div>{form.quantities[product.id]>0&&<div className="arrangement-order"><button type="button" className="text-button" disabled={chosen[0]?.id===product.id} onClick={()=>moveService(product.id,-1)}>上移</button><button type="button" className="text-button" disabled={chosen.at(-1)?.id===product.id} onClick={()=>moveService(product.id,1)}>下移</button><button type="button" className="text-button" onClick={()=>selectService(product.id,false)}>从本方案移除</button></div>}</div>)}
   </fieldset>
   <div className="arrangement-quote"><div><span>试算合计</span><strong>{money(subtotal)}</strong></div><p className="muted small">确认单使用服务端实时报价。仅预留实际报价，不预留全部预算。</p></div>
   {draft?<button type="button" className="button secondary" disabled={disabled} onClick={edit}>修改方案（撤销旧确认）</button>:<button className="button primary" disabled={disabled||!data||!chosen.length}>核对方案</button>}
  </form> : task ? <div className="arrangement-detail">
   <div className="arrangement-title"><h3>{task.title}</h3><span className={`arrangement-status state-${task.status}`}>{workflowStatus[task.status]??task.status}</span></div>
   <p className="muted small">对象 {task.recipient_label} · 执行 {beijingTime(task.execute_at)}（北京时间）</p>
   <div className="arrangement-metrics">{[["预算",task.budget_minor],["报价",task.total_minor],["当前预留",task.reserved_minor],["已支付",task.paid_minor],["已退款",task.refunded_minor],["待退款",task.refund_pending_minor],["已释放预留",Math.max(task.total_minor-task.paid_minor-task.reserved_minor,0)],["实际净支出",task.net_spent_minor]].map(([label,value])=><div key={label}><span>{label}</span><strong>{money(Number(value))}</strong></div>)}</div>
   <div className="arrangement-steps">{task.steps.map((step,index)=><article key={step.step_id} className="arrangement-step"><div className="arrangement-step-title"><span>{index+1}. {step.name}<small>{step.merchant_name} · {step.quantity}{step.unit||"份"}</small></span><span>{money(step.total_minor)}</span><span className={`arrangement-status state-${step.status}`}>{workflowStepStatus[step.status]??step.status}</span></div><p className="muted small">{step.message}</p><details><summary>订单与资金详情</summary><dl><div><dt>订单编号</dt><dd>{step.order_id??"尚未创建"}</dd></div><div><dt>订单状态</dt><dd>{step.order_status??"尚未下单"}</dd></div><div><dt>已退款</dt><dd>{money(step.refunded_minor)}</dd></div><div><dt>待退款</dt><dd>{money(step.refund_pending_minor)}</dd></div>{step.operation_id&&<div><dt>付款回执</dt><dd>{step.operation_id}</dd></div>}{step.refund_operation_id&&<div><dt>退款回执</dt><dd>{step.refund_operation_id}</dd></div>}</dl></details></article>)}</div>
   <p className="arrangement-outcome">{workflowOutcome(task)}</p><p className="muted small">执行窗口截止 {beijingTime(task.deadline_at)}（北京时间）。净支出 = 已支付 − 已退款，待退款尚未入账。</p>
   <div className="arrangement-actions"><button className="button secondary" onClick={()=>onContinue(task.conversation_id)}>继续对话</button>{canCancelWorkflow(task)&&<button className="text-button" disabled={disabled} onClick={()=>cancel(task)}>取消安排</button>}{canRecoverWorkflow(task)&&<button className="button primary" disabled={disabled} onClick={()=>recover(task)}>核对处理进度</button>}</div>
  </div> : <>
   <div className="management-entry-heading"><p className="muted">在预算内组合模拟服务，确认后按方案执行。</p><button className="text-button" disabled={disabled||managementBlocked} onClick={()=>onManage({kind:"services"})}>管理服务</button></div>
   <div className="arrangement-filters"><label>状态<select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部</option>{Object.entries(workflowStatus).map(([id,label])=><option value={id} key={id}>{label}</option>)}</select></label></div>
   {!data&&!error&&<p role="status">正在读取安排…</p>}
   {data?.tasks.filter(item=>filter==="all"||item.status===filter).map(item=><button className="arrangement-list-row" key={item.id} onClick={()=>{setSelected(item.id);setNotice("");}}><span>{item.title}<small>{item.recipient_label} · {beijingTime(item.execute_at)}（北京时间）</small></span><span>{money(item.total_minor)}</span><span className={`arrangement-status state-${item.status}`}>{workflowStatus[item.status]??item.status}</span></button>)}
   {data&&!data.tasks.some(item=>filter==="all"||item.status===filter)&&<p className="muted empty-list">暂无此类安排。</p>}
  </>}
 </section>;
}
