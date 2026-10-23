import { useCallback, useEffect, useRef, useState } from "react";
import { advanceTaskClock, getContacts, getTasks, money, parseMinor, previewTask, simulateTaskPayment } from "../api";
import type { Account, Action, TaskList, TaskPreviewInput, Contact } from "../types";
import { availableMoney, beijingTime, mergeTaskVersions, participantStatus, shanghaiInput, taskStatus } from "../task-utils";
import "../tasks.css";
import { contactLabel, type OpenManagement } from "./ManagementPanel";
interface Props {
  onManage:OpenManagement;managementBlocked:boolean;
  view: "aa" | "schedules"; visible: boolean; accounts: Account[]; revision: number; blocked: boolean;
  ensureConversation: (existingId?:string) => Promise<string>; onAction: (action: Action) => void;
  onBusy: (busy: boolean) => void; onRefresh: () => void; onContinue: (id: string) => void;
}
interface FormState {title:string;total:string;aaAccount:string;participants:string[];account:string;recipient:string;amount:string;executeAt:string}
const initialForm: FormState={title:"",total:"",aaAccount:"",participants:[],account:"",recipient:"",amount:"",executeAt:""};
function readForm():FormState {try{return {...initialForm,...JSON.parse(localStorage.getItem("yx2.payment.tasks.form.v1")??"{}")};}catch{return initialForm;}}
export function PaymentTasks(props:Props) {
  const {view,visible,accounts,revision,blocked,ensureConversation,onAction,onBusy,onRefresh,onContinue,onManage,managementBlocked}=props;
  const [form,setForm]=useState(readForm);
  const [data,setData]=useState<TaskList|null>(null);
  const [recipients,setRecipients]=useState<Contact[]>([]);
  const [error,setError]=useState(""); const [notice,setNotice]=useState(""); const [busy,setBusy]=useState(false);
  const [selected,setSelected]=useState(""); const [minutes,setMinutes]=useState("5");
  const alive=useRef(true); const lock=useRef(false); const requestVersion=useRef(0);
  const dataRef=useRef<TaskList|null>(null); const propsRef=useRef(props);propsRef.current=props;
  const previewConversation=useRef("");
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;++requestVersion.current;};},[]);
  useEffect(()=>{try{localStorage.setItem("yx2.payment.tasks.form.v1",JSON.stringify(form));}catch{}},[form]);
  const refresh=useCallback(async()=>{
    const version=++requestVersion.current;
    try {
      const next=await getTasks();
      if(!alive.current || version!==requestVersion.current)return;
      const merged=mergeTaskVersions(dataRef.current,next);
      const fingerprint=(value:TaskList)=>[...value.aa,...value.schedules].map(x=>`${x.id}:${x.state_version}`).sort().join("|");
      const changed=dataRef.current && fingerprint(dataRef.current)!==fingerprint(merged);
      dataRef.current=merged;setData(merged);
      if(previewConversation.current){const task=[...merged.aa,...merged.schedules].find(x=>x.conversation_id===previewConversation.current);if(task){setSelected(task.id);previewConversation.current="";}}
      if(changed)propsRef.current.onRefresh();
    } catch(failure){if(alive.current && version===requestVersion.current)setError(failure instanceof Error?failure.message:"无法读取收付款任务。");}
  },[]);
  useEffect(()=>{
    if(!visible)return;
    void refresh();
    let active=true;
    void getContacts().then(result=>{if(active)setRecipients(result.contacts);}).catch(failure=>{if(active)setError(failure.message);});
    const tick=()=>{if(!document.hidden && !lock.current)void refresh();};
    const timer=window.setInterval(tick,10000);document.addEventListener("visibilitychange",tick);
    return()=>{active=false;clearInterval(timer);document.removeEventListener("visibilitychange",tick);++requestVersion.current;};
  },[visible,revision,refresh]);
  const checking=accounts.filter(x=>x.kind==="checking");
  const aaAccount=form.aaAccount || checking[0]?.id || "";
  const sourceAccount=form.account || checking[0]?.id || "";
  const aa=data?.aa.find(x=>x.id===selected);
  const scheduled=data?.schedules.find(x=>x.id===selected);
  const disabled=busy || blocked;
  function update<K extends keyof FormState>(key:K,value:FormState[K]){setForm(previous=>({...previous,[key]:value}));}
  async function run(task:()=>Promise<void>) {
    if(lock.current || blocked)return;
    lock.current=true;setBusy(true);onBusy(true);setError("");setNotice("");
    try{await task();}catch(failure){if(alive.current)setError(failure instanceof Error?failure.message:"操作未完成，请重试。");}
    finally{lock.current=false;if(alive.current){setBusy(false);onBusy(false);}}
  }
  async function prepare(input:Omit<TaskPreviewInput,"conversation_id">, originalConversation?:string){
    const startingView=view;
    await run(async()=>{
      const conversation=await ensureConversation(originalConversation);
      const action=await previewTask({...input,conversation_id:conversation});
      if(alive.current && propsRef.current.visible && propsRef.current.view===startingView){
        if(input.action==="create_aa" || input.action==="schedule_transfer")previewConversation.current=conversation;
        onAction(action);
      }
    });
  }
  function createAA(){try{const total=parseMinor(form.total);if(form.participants.some(id=>!recipients.some(x=>x.id===id)))throw new Error("所选参与者已移出目录，请取消勾选后重新核对。");if(!aaAccount || !form.participants.length)throw new Error("请选择收款账户及至少一位其他参与者。");void prepare({action:"create_aa",title:form.title.trim()||"AA收款",account_id:aaAccount,total_minor:total,participant_ids:["self",...form.participants]});}catch(e){setError(e instanceof Error?e.message:"请核对AA信息。");}}
  function createSchedule(){try{const amount=parseMinor(form.amount);const execute=shanghaiInput(form.executeAt);if(!recipients.find(x=>x.id===form.recipient)?.can_transfer)throw new Error("请先补充该联系人的模拟收款信息。");if(!form.recipient || !sourceAccount)throw new Error("请选择付款账户与明确的收款人。");if(!data)throw new Error("请先读取服务时钟。");if(Date.parse(execute)<=Date.parse(data.now))throw new Error("执行时间须晚于当前服务时间。");void prepare({action:"schedule_transfer",account_id:sourceAccount,recipient_id:form.recipient,amount_minor:amount,execute_at:execute});}catch(e){setError(e instanceof Error?e.message:"请核对定时信息。");}}
  const recipientLabel=(id:string,name:string)=>{const person=recipients.find(x=>x.id===id);return person?contactLabel(person):name;};
  let shares:number[]=[];
  try{const total=parseMinor(form.total);const count=form.participants.length+1;shares=Array.from({length:count},(_,i)=>Math.floor(total/count)+(i<total%count?1:0));}catch{}
  function chooseContact(item:Contact){setRecipients(old=>[...old.filter(x=>x.id!==item.id),item]);setForm(old=>view==="aa"?{...old,participants:[...new Set([...old.participants,item.id])]}:{...old,recipient:item.id});}
  const selectedContact=recipients.find(x=>x.id===form.recipient);
  const accountOptions=(items:Account[])=>items.map(x=><option key={x.id} value={x.id}>{x.name} · 可用 {money(availableMoney(x))}</option>);
  return <div className="payment-tasks" hidden={!visible}>
    {error && <p className="inline-error" role="alert">{error} <button className="text-button" onClick={()=>{setError("");void refresh();}}>重新读取</button></p>}
    {notice && <p className="task-notice" role="status">{notice}</p>}
    <div className="task-page-heading"><h2>{view==="aa"?"AA收款":"定时指令"}</h2><button className="text-button" disabled={disabled} onClick={()=>{setSelected("");setError("");}}>{view==="aa"?"新建AA":"新建定时"}</button></div>
    {view==="aa" && (aa ? <section aria-label="AA详情">
      <div className="task-title"><h3>{aa.title}</h3><span className="entry-status">{taskStatus[aa.status]}</span></div>
      <p className="muted small">共 {aa.participants.length} 人参与 · 已收 {aa.participants.filter(x=>x.status==="paid").length} 人 · 待付 {aa.participants.filter(x=>x.status==="pending").length} 人</p>
      <div className="task-metrics"><span>总额 <strong>{money(aa.total_minor)}</strong></span><span>自担 <strong>{money(aa.total_minor-aa.collectable_minor)}</strong></span><span>应收 <strong>{money(aa.collectable_minor)}</strong></span><span>已收 <strong>{money(aa.collected_minor)}</strong></span></div>
      <div className="task-participants">{aa.participants.map(person=><div className="task-participant" key={person.id}><span>{person.id==="self"?"我":person.name}</span><span>{money(person.share_minor)}</span><span className="entry-status">{participantStatus[person.status]}</span>{person.status==="pending" && <button className="text-button" aria-label={`模拟${person.name}付款 · 仅模拟`} disabled={disabled} onClick={()=>void run(async()=>{const result=await simulateTaskPayment(aa.id,person.id);if(alive.current){setNotice(result.message);await refresh();onRefresh();}})}>模拟付款 · 仅模拟</button>}</div>)}</div>
      <div className="task-actions">{(aa.status==="awaiting_payment" || aa.status==="partial") && <button className="button secondary" disabled={disabled} onClick={()=>void prepare({action:"cancel_aa",task_id:aa.id},aa.conversation_id)}>取消未付请求</button>}<button className="text-button" onClick={()=>onContinue(aa.conversation_id)}>继续对话</button></div>
      <p className="muted small">模拟付款只使用虚构账户。取消仅停止未付请求，保留已收款。</p>
    </section> : <form className="task-form" aria-label="创建AA收款" onSubmit={e=>{e.preventDefault();createAA();}}>
      <label>事项名称<input value={form.title} maxLength={120} placeholder="例如：聚餐AA" onChange={e=>update("title",e.target.value)}/></label>
      <label>总额（元）<input required inputMode="decimal" value={form.total} onChange={e=>update("total",e.target.value)}/></label>
      <label>收款账户<select required value={aaAccount} onChange={e=>update("aaAccount",e.target.value)}><option value="" disabled>请选择收款账户</option>{accountOptions(checking)}</select></label>
      <fieldset><legend>参与者</legend><button type="button" className="text-button" disabled={disabled||managementBlocked} onClick={()=>onManage({kind:"contacts",add:true,onSelect:chooseContact})}>添加参与者</button><label className="participant-choice"><input type="checkbox" checked disabled/>我 · 自担</label>{recipients.map(person=><label className="participant-choice" key={person.id}><input type="checkbox" checked={form.participants.includes(person.id)} onChange={e=>update("participants",e.target.checked?[...form.participants,person.id]:form.participants.filter(id=>id!==person.id))}/>{recipientLabel(person.id,person.name)}</label>)}{form.participants.filter(id=>!recipients.some(x=>x.id===id)).map(id=><label className="participant-choice" key={id}><input type="checkbox" checked onChange={()=>update("participants",form.participants.filter(x=>x!==id))}/>已移除的联系人，请取消勾选</label>)}{!recipients.length && <p className="muted small">暂无可选联系人；请检查服务连接。</p>}</fieldset>
      {shares.length>1 && <div className="task-share-preview"><h3>均摊金额</h3>{["self",...form.participants].map((id,index)=><div className="stat-row" key={id}><span>{id==="self"?"我 · 自担":recipientLabel(id,"所选联系人")}</span><output>{money(shares[index])}</output></div>)}<p className="muted small">分摊金额只读；余下的分按参与顺序分配，最终以确认单为准。</p></div>}
      <button className="button primary" disabled={disabled || !form.participants.length || !aaAccount}>预览AA请求</button><p className="muted small">创建请求不代表到账，也不会向真实联系人发送消息。</p>
    </form>)}
    {view==="schedules" && (scheduled ? <section aria-label="定时指令详情">
      <div className="task-title"><h3>给{scheduled.recipient_name}的定时转账</h3><span className="entry-status">{taskStatus[scheduled.status]}</span></div>
      <dl className="task-facts"><div><dt>金额</dt><dd>{money(scheduled.amount_minor)}</dd></div><div><dt>收款人</dt><dd>{scheduled.recipient_name}</dd></div><div><dt>执行时间</dt><dd>{beijingTime(scheduled.execute_at)}（北京时间）</dd></div><div><dt>窗口截止</dt><dd>{beijingTime(scheduled.deadline_at)}（北京时间）</dd></div></dl>
      {accounts.filter(x=>x.id===scheduled.account_id).map(account=><div className="task-metrics" key={account.id}><span>账面余额 <strong>{money(account.balance_minor)}</strong></span><span>账户预留 <strong>{money(account.reserved_minor??0)}</strong></span><span>可用 <strong>{money(availableMoney(account))}</strong></span></div>)}
      <p className="muted small">本指令当前预留 {money(scheduled.reserved_minor)}。{scheduled.message}</p>
      <div className="task-actions">{scheduled.status==="scheduled" && <button className="button secondary" disabled={disabled} onClick={()=>void prepare({action:"cancel_schedule",task_id:scheduled.id},scheduled.conversation_id)}>撤销安排</button>}<button className="text-button" onClick={()=>onContinue(scheduled.conversation_id)}>继续对话</button></div>
      <p className="muted small">应用关闭时暂停处理；重启后在授权窗口内补执行，超过窗口则释放预留。</p>
    </section> : <form className="task-form" aria-label="创建定时转账" onSubmit={e=>{e.preventDefault();createSchedule();}}>
      <label>付款账户<select required value={sourceAccount} onChange={e=>update("account",e.target.value)}><option value="" disabled>请选择付款账户</option>{accountOptions(checking)}</select></label>
      <label>收款人<select required value={form.recipient} onChange={e=>update("recipient",e.target.value)}><option value="" disabled>请选择姓名、银行与账户</option>{recipients.map(person=><option key={person.id} value={person.id}>{recipientLabel(person.id,person.name)}{!person.can_transfer?" · 补充收款信息":""}</option>)}</select></label>
      <div className="management-entry-actions">{selectedContact&&!selectedContact.can_transfer&&<button type="button" className="text-button" disabled={disabled||managementBlocked} onClick={()=>onManage({kind:"contacts",edit:selectedContact,requireTransfer:true,onSelect:chooseContact})}>补充收款信息</button>}<button type="button" className="text-button" disabled={disabled||managementBlocked} onClick={()=>onManage({kind:"contacts",add:true,requireTransfer:true,onSelect:chooseContact})}>添加联系人</button></div>
      <label>金额（元）<input required inputMode="decimal" value={form.amount} onChange={e=>update("amount",e.target.value)}/></label>
      <label>执行时间（北京时间）<input required type="datetime-local" value={form.executeAt} onChange={e=>update("executeAt",e.target.value)}/></label>
      <p className="muted small">当前服务时间：{beijingTime(data?.now)}（北京时间）。确认后预留资金，执行前尚未扣款。</p>
      <button className="button primary" disabled={disabled || !selectedContact?.can_transfer || !sourceAccount || !data}>预览定时安排</button>
    </form>)}
    <section className="task-history"><h3>{view==="aa"?"AA请求记录":"定时指令记录"}</h3>{!data && !error && <p className="muted" role="status">正在读取…</p>}
      {view==="aa" && data?.aa.map(task=><button className={`task-list-row ${selected===task.id?"selected":""}`} key={task.id} onClick={()=>setSelected(task.id)}><span>{task.title}</span><span>{money(task.collected_minor)} / {money(task.collectable_minor)}</span><span>{taskStatus[task.status]}</span></button>)}
      {view==="schedules" && data?.schedules.map(task=><button className={`task-list-row ${selected===task.id?"selected":""}`} key={task.id} onClick={()=>setSelected(task.id)}><span>{task.recipient_name} · {beijingTime(task.execute_at)}</span><span>{money(task.amount_minor)}</span><span>{taskStatus[task.status]}</span></button>)}
      {data && !(view==="aa"?data.aa:data.schedules).length && <p className="muted small">暂无记录。</p>}
    </section>
    {view==="schedules" && <details className="task-demo"><summary>模拟演示时钟</summary><p className="muted small">仅演示：推进模拟银行服务时间，会执行已确认且到期的定时指令。不会改变电脑时间。</p><form onSubmit={e=>{e.preventDefault();void run(async()=>{const amount=Number(minutes);if(!Number.isInteger(amount)||amount<1||amount>525600)throw new Error("推进分钟数须为1至525600的整数。");const result=await advanceTaskClock(amount);if(alive.current){setNotice(`服务时间已推进至${beijingTime(result.now)}（北京时间）。`);await refresh();onRefresh();}});}}><label>推进分钟<input type="number" min={1} max={525600} step={1} value={minutes} onChange={e=>setMinutes(e.target.value)}/></label><button className="button secondary" disabled={disabled}>推进模拟时间</button></form></details>}
  </div>;
}
