import { useEffect, useState } from "react";
import { ArrowUpRight, ArrowDownLeft } from "lucide-react";
import { getContacts, money, timestamp } from "../api";
import type { Action, Overview, Contact } from "../types";
import { contactLabel, type OpenManagement } from "./ManagementPanel";
import { PaymentTasks } from "./PaymentTasks";
export function PaymentsPage({overview,onDiscuss,revision,blocked,ensureConversation,onAction,onBusy,onRefresh,onContinue,onManage,managementBlocked}:{
  onManage:OpenManagement;managementBlocked:boolean;overview:Overview|null;onDiscuss:(text:string)=>void;revision:number;blocked:boolean;
  ensureConversation:(existingId?:string)=>Promise<string>;onAction:(action:Action)=>void;onBusy:(busy:boolean)=>void;onRefresh:()=>void;onContinue:(id:string)=>void;
}) {
  const [tab,setTab]=useState("transfer");
  const [filter,setFilter]=useState("all"); const [recipient,setRecipient]=useState("");
  const [contacts,setContacts]=useState<Contact[]>([]);const [selected,setSelected]=useState("");const [contactsError,setContactsError]=useState("");
  useEffect(()=>{let active=true;void getContacts().then(r=>{if(active){setContacts(r.contacts);setContactsError("");}}).catch(e=>{if(active)setContactsError(e.message);});return()=>{active=false;};},[revision]);
  const person=contacts.find(x=>x.id===selected);
  function choose(item:Contact){setContacts(old=>[...old.filter(x=>x.id!==item.id),item]);setSelected(item.id);setRecipient(item.name);}
  const transactions=overview?.transactions.filter(x=>filter==="all" || (filter==="out"?x.amount_minor<0:x.amount_minor>0))??[];
  return <section className="data-page payments-page" aria-label="收付款"><div className="page-intro"><div className="management-entry-heading"><h2>收付款</h2><button className="text-button" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"contacts"})}>管理联系人</button></div><p>转账前先核对收款人、金额与付款账户。</p></div>
    <div className="payment-task-tabs" role="tablist" aria-label="收付款类型">{[["transfer","转账"],["aa","AA收款"],["schedules","定时指令"]].map(([id,label])=><button key={id} role="tab" aria-selected={tab===id} className={tab===id?"active":""} onClick={()=>setTab(id)}>{label}</button>)}</div>
    <div hidden={tab!=="transfer"}>
    {contactsError&&<p className="inline-error" role="alert">{contactsError}</p>}
    <div className="contact-picker"><label>选择联系人<select value={selected} onChange={e=>{setSelected(e.target.value);const contact=contacts.find(x=>x.id===e.target.value);setRecipient(contact?.name??"");}}><option value="">也可输入姓名讨论</option>{contacts.map(x=><option key={x.id} value={x.id}>{contactLabel(x)}{!x.can_transfer?" · 补充收款信息":""}</option>)}</select></label><div className="management-entry-actions">{person&&!person.can_transfer&&<button className="text-button" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"contacts",edit:person,requireTransfer:true,onSelect:choose})}>补充收款信息</button>}<button className="text-button" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"contacts",add:true,requireTransfer:true,onSelect:choose})}>添加联系人</button></div></div>
    <form className="payment-compose" onSubmit={e=>{e.preventDefault();if(person&&!person.can_transfer)return;onDiscuss(person?`我想给${contactLabel(person)}转账，请按此联系人核对收款人。`:recipient.trim()?`我想给${recipient.trim()}转账，请先帮我核对收款人。`:"我想转账，请帮我准备转账草稿。");}}><label className="sr-only" htmlFor="recipient">收款人姓名</label><input id="recipient" placeholder="收款人姓名（可稍后补充）" value={recipient} onChange={e=>{setRecipient(e.target.value);setSelected("");}}/><button className="button primary" disabled={blocked||managementBlocked||!!person&&!person.can_transfer}>讨论一笔转账</button></form>
    <div className="payment-filters" aria-label="收付款筛选">{[["all","全部"],["out","付款"],["in","收款"]].map(([id,label])=><button key={id} className={filter===id?"active":""} onClick={()=>setFilter(id)}>{label}</button>)}</div>
    <div className="table-scroll"><table><thead><tr><th>收支对象 / 摘要</th><th>金额</th><th>记录</th><th>时间</th><th></th></tr></thead><tbody>{transactions.map(item=><tr key={item.id}><td><span className="payment-description">{item.amount_minor<0?<ArrowUpRight size={17}/>:<ArrowDownLeft size={17}/>} {item.description}</span></td><td>{money(item.amount_minor,item.currency)}</td><td><span className="entry-status">已记账</span></td><td>{timestamp(item.created_at)}</td><td><button className="text-button" onClick={()=>onDiscuss(`请帮我解释这笔收支记录：${item.description}，金额${money(item.amount_minor,item.currency)}，时间${item.created_at}，记录编号${item.id}。`)}>讨论</button></td></tr>)}</tbody></table></div>
    {overview && !transactions.length && <p className="muted empty-list">暂无此类收付款记录。</p>}{!overview && <p className="muted">正在读取收付款记录…</p>}
    </div>
    <PaymentTasks onManage={onManage} managementBlocked={managementBlocked} view={tab==="aa"?"aa":"schedules"} visible={tab!=="transfer"} accounts={overview?.accounts??[]} revision={revision} blocked={blocked} ensureConversation={ensureConversation} onAction={onAction} onBusy={onBusy} onRefresh={onRefresh} onContinue={onContinue}/>
  </section>;
}
