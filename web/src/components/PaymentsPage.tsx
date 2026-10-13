import { useState } from "react";
import { ArrowUpRight, ArrowDownLeft } from "lucide-react";
import { money, timestamp } from "../api";
import type { Overview } from "../types";
export function PaymentsPage({overview,onDiscuss}:{overview:Overview|null;onDiscuss:(text:string)=>void}) {
  const [filter,setFilter]=useState("all"); const [recipient,setRecipient]=useState("");
  const transactions=overview?.transactions.filter(x=>filter==="all" || (filter==="out"?x.amount_minor<0:x.amount_minor>0))??[];
  return <section className="data-page payments-page" aria-label="收付款"><div className="page-intro"><h2>收付款</h2><p>转账前先核对收款人、金额与付款账户。</p></div>
    <form className="payment-compose" onSubmit={e=>{e.preventDefault();onDiscuss(recipient.trim()?`我想给${recipient.trim()}转账，请先帮我核对收款人。`:"我想转账，请帮我准备转账草稿。");}}><label className="sr-only" htmlFor="recipient">收款人姓名</label><input id="recipient" placeholder="收款人姓名（可稍后补充）" value={recipient} onChange={e=>setRecipient(e.target.value)}/><button className="button primary">讨论一笔转账</button></form>
    <div className="payment-filters" aria-label="收付款筛选">{[["all","全部"],["out","付款"],["in","收款"]].map(([id,label])=><button key={id} className={filter===id?"active":""} onClick={()=>setFilter(id)}>{label}</button>)}</div>
    <div className="table-scroll"><table><thead><tr><th>收支对象 / 摘要</th><th>金额</th><th>记录</th><th>时间</th><th></th></tr></thead><tbody>{transactions.map(item=><tr key={item.id}><td><span className="payment-description">{item.amount_minor<0?<ArrowUpRight size={17}/>:<ArrowDownLeft size={17}/>} {item.description}</span></td><td>{money(item.amount_minor,item.currency)}</td><td><span className="entry-status">已记账</span></td><td>{timestamp(item.created_at)}</td><td><button className="text-button" onClick={()=>onDiscuss(`请帮我解释这笔收支记录：${item.description}，金额${money(item.amount_minor,item.currency)}，时间${item.created_at}，记录编号${item.id}。`)}>讨论</button></td></tr>)}</tbody></table></div>
    {overview && !transactions.length && <p className="muted empty-list">暂无此类收付款记录。</p>}{!overview && <p className="muted">正在读取收付款记录…</p>}
  </section>;
}
