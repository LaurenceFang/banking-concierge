import { useState } from "react";
import { CreditCard } from "lucide-react";
import { getCards, money, parseMinor, previewCard, timestamp } from "../api";
import type { Action, Card, CardAction } from "../types";
import { useDomainPage } from "./useDomainPage";

export const cardStatus: Record<string, string> = {active:"正常", frozen:"已冻结", lost:"已挂失", closed:"已关闭"};
export function CardsPage({ conversationId, ensureConversation, revision, blocked, onAction, onBusy }: {
  conversationId:string; ensureConversation:(existingId?:string)=>Promise<string>; revision:number; blocked:boolean; onAction:(action:Action)=>void; onBusy:(busy:boolean)=>void;
}) {
  const {data,error,busy,run,alive} = useDomainPage(getCards,revision,onBusy);
  const [edit,setEdit] = useState<{card:Card; action:"set_daily_limit"|"set_credit_limit"}|null>(null);
  const [amount,setAmount] = useState("");
  const disabled = busy || blocked || !data;
  function preview(action:CardAction["action"], card?:Card, cardKind?:"debit"|"credit") {
    if(disabled) return;
    void run(async()=>{
      const result = await previewCard({action,conversation_id:await ensureConversation(),...(card?{card_id:card.id}:{}),...(cardKind?{card_kind:cardKind}:{}),...(action.startsWith("set_")?{amount_minor:parseMinor(amount)}:{})});
      if(alive.current) {setEdit(null);onAction(result);}
    });
  }
  return <section className="data-page p3-page" aria-label="卡片管理">
    <div className="page-intro"><h2>卡片管理</h2><p>管理卡片状态与消费限额</p></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!data && !error && <p role="status">正在读取卡片…</p>}
    {data?.cards.slice().sort((a,b)=>Number(a.kind === "credit")-Number(b.kind === "credit")).map(card=><article className="card-management-row" key={card.id}>
      <div className="card-identification" aria-label={`尾号${card.last4}`}><CreditCard size={29}/><span>{card.last4}</span></div>
      <div className="card-facts"><h3>{card.name} · 尾号 {card.last4}</h3><p>状态　{cardStatus[card.status]}</p><p>单日消费限额 {money(card.daily_limit_minor)}</p>
        {card.kind==="credit" && <p className="muted small">信用额度 {money(card.credit_limit_minor ?? 0)} · 已用 {money(card.outstanding_minor)} · 可用 {money(card.available_credit_minor ?? 0)}</p>}
      </div>
      <div className="row-actions card-controls">
        {(card.status==="active" || card.status==="frozen") && <>
          <button className="button secondary" disabled={disabled} onClick={()=>{setEdit({card,action:"set_daily_limit"});setAmount((card.daily_limit_minor/100).toFixed(2));}}>调整日限额</button>
          <button className="text-button" disabled={disabled} onClick={()=>preview(card.status==="frozen"?"unfreeze":"freeze",card)}>{card.status==="frozen"?"解锁":"冻结"}</button>
          <button className="text-button" disabled={disabled} onClick={()=>preview("report_lost",card)}>挂失</button>
          {card.kind==="credit" && <button className="text-button" disabled={disabled} onClick={()=>{setEdit({card,action:"set_credit_limit"});setAmount(((card.credit_limit_minor??0)/100).toFixed(2));}}>调整信用额度</button>}
        </>}
        {card.status==="lost" && <span className="muted small">挂失卡片不能普通解锁</span>}
      </div>
      {edit?.card.id===card.id && <form className="p3-inline-form" onSubmit={e=>{e.preventDefault();preview(edit.action,card);}}>
        <label>{edit.action==="set_daily_limit"?"新单日消费限额（元）":"新信用额度（元）"}<input required inputMode="decimal" value={amount} onChange={e=>setAmount(e.target.value)}/></label>
        <p className="muted small">{edit.action==="set_daily_limit"?"模拟范围：100–20,000元。":"模拟范围：3,000–50,000元，且不低于已用额度。"}</p>
        <button className="button secondary" disabled={disabled}>预览调整</button><button type="button" className="text-button" disabled={busy} onClick={()=>setEdit(null)}>收起</button>
      </form>}
    </article>)}
    <section className="section-gap"><h3>卡片申请</h3><p className="muted">提交申请后等待审核，不会立即发卡。</p><div className="row-actions p3-spacing"><button className="button secondary" disabled={disabled} onClick={()=>preview("apply_card",undefined,"debit")}>申请借记卡</button><button className="button secondary" disabled={disabled} onClick={()=>preview("apply_card",undefined,"credit")}>申请信用卡</button></div>
      {data?.applications.map(app=><p key={app.id} className="p3-spacing">{app.card_kind==="debit"?"借记卡":"信用卡"}申请 · 已提交，待审核 <span className="muted small">{timestamp(app.submitted_at)}</span></p>)}
    </section>
    <p className="muted small section-gap">冻结或挂失仅限制卡片渠道，不影响账户转账。<br/>所有资料与规则均为模拟。</p>
  </section>;
}
