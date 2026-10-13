import { availableMoney } from "../task-utils";
import { useState } from "react";
import { advanceWealthDay, getWealth, money, parseMinor, previewWealth, saveRiskProfile } from "../api";
import type { Account, Action, RiskAnswers, WealthPosition, WealthProduct } from "../types";
import { useDomainPage } from "./useDomainPage";

const questions: {key:keyof RiskAnswers; label:string; options:[string,string][]}[] = [
  {key:"horizon",label:"计划投资期限",options:[["short","短期"],["medium","中期"],["long","长期"]]},
  {key:"loss_tolerance",label:"可承受亏损程度",options:[["none","不能承受亏损"],["low","较低"],["moderate","中等"],["high","较高"]]},
  {key:"experience",label:"投资经验",options:[["none","没有经验"],["basic","基础经验"],["experienced","经验丰富"]]},
  {key:"liquidity_need",label:"资金流动性需求",options:[["high","较高"],["medium","中等"],["low","较低"]]},
  {key:"objective",label:"投资目标",options:[["preserve","资产保全"],["balanced","均衡配置"],["growth","追求增长"]]},
];
const fee = (bps:number) => bps===0?"0":`${(bps/100).toFixed(2)}%`;
export function WealthPage({conversationId, ensureConversation,revision,blocked,onAction,onBusy,accounts,onRefresh,section="positions"}: {
  section?:string; conversationId:string; ensureConversation:(existingId?:string)=>Promise<string>;revision:number;blocked:boolean;onAction:(action:Action)=>void;onBusy:(busy:boolean)=>void;accounts:Account[];onRefresh:()=>void;
}) {
  const {data,error,busy,run,alive} = useDomainPage(getWealth,revision,onBusy);
  const [showRisk,setShowRisk] = useState<boolean | null>(null);
  const [answers,setAnswers] = useState<Partial<RiskAnswers>>({});
  const [selection,setSelection] = useState<{product:WealthProduct;position?:WealthPosition}|null>(null);
  const [amount,setAmount] = useState("");
  const [accountId,setAccountId] = useState("");
  const disabled = busy || blocked;
  const complete = questions.every(q=>answers[q.key]);
  const riskOpen = section === "risk" || (showRisk ?? false);
  function submitRisk() {
    if(disabled || !complete) return;
    void run(async()=>{await saveRiskProfile(answers as RiskAnswers);if(alive.current){setShowRisk(false);setAnswers({});setSelection(null);onRefresh();}},true);
  }
  function select(product:WealthProduct,position?:WealthPosition) {
    setSelection({product,position});setAmount("");setAccountId("");
  }
  function preview() {
    if(disabled || !selection || !accountId) return;
    void run(async()=>{
      const minor=parseMinor(amount);
      if(!selection.position && !selection.product.eligible) throw new Error("请先完成适配的风险测评。");
      if(!selection.position && minor<selection.product.min_purchase_minor) throw new Error(`起购金额为${money(selection.product.min_purchase_minor)}。`);
      if(selection.position && (selection.position.unlock_on>data!.as_of || minor>selection.position.available_minor)) throw new Error("尚未解锁或赎回金额超过可用份额本金。");
      const result=await previewWealth({action:selection.position?"redeem":"purchase",conversation_id:await ensureConversation(),product_id:selection.product.id,account_id:accountId,...(selection.position?{position_id:selection.position.id}:{}),amount_minor:minor});
      if(alive.current){onAction(result);setSelection(null);}
    });
  }
  return <section className="data-page p3-page" aria-label="理财管理">
    <div className="page-intro"><h2>{section==="positions"?"我的持仓":section==="orders"?"理财订单":section==="risk"?"风险档案":"产品比较"}</h2><p>先看风险、费用与期限，再决定是否申购</p></div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!data && !error && <p role="status">正在读取理财…</p>}
    {data && <>
      <div className="risk-summary"><span>{data.risk_profile?`风险档案已完成 · R${data.risk_profile.risk_level}`:"尚未完成风险测评"}</span><button className="text-button" disabled={disabled} onClick={()=>setShowRisk(!riskOpen)}>{riskOpen?"收起测评":data.risk_profile?"查看风险档案":"填写风险测评"}</button></div>
      {riskOpen && <form className="risk-form" onSubmit={e=>{e.preventDefault();submitRisk();}}>
        {data.risk_profile && <><p className="muted">当前模拟档案为 R{data.risk_profile.risk_level}，可选择不高于此等级的产品。测评时间 {data.risk_profile.assessed_at.slice(0,10)}</p><dl className="risk-answers">{questions.map(q=><div key={q.key}><dt>{q.label}</dt><dd>{q.options.find(([value])=>value===data.risk_profile!.answers[q.key])?.[1]??"未填写"}</dd></div>)}</dl></>}
        <p>{data.risk_profile?"如需更新，请重新回答全部五项。":"请按实际情况回答全部五项。"}风险档案用于产品适配，不预测收益。</p>
        <div className="risk-questions">{questions.map(q=><label key={q.key}>{q.label}<select required value={answers[q.key]??""} onChange={e=>setAnswers({...answers,[q.key]:e.target.value})}><option value="" disabled>请选择</option>{q.options.map(([value,label])=><option value={value} key={value}>{label}</option>)}</select></label>)}</div>
        <button className="button secondary" disabled={disabled || !complete}>保存风险测评</button>
      </form>}
      {section === "products" && <div className="table-scroll"><table className="wealth-table"><thead><tr><th>模拟产品</th><th>风险</th><th>起购金额</th><th>申购费 · 赎回费</th><th>锁定期</th><th>结算</th><th>操作</th></tr></thead><tbody>{data.products.slice().sort((a,b)=>a.risk_level-b.risk_level).map(product=><tr key={product.id}><td>{product.name}</td><td>R{product.risk_level}</td><td>{money(product.min_purchase_minor)}</td><td>{fee(product.purchase_fee_bps)} · {fee(product.redemption_fee_bps)}</td><td>{product.lock_days}天</td><td>T+{product.settlement_business_days}</td><td>{product.eligible?<button className="button secondary" disabled={disabled} onClick={()=>select(product)}>预览申购</button>:<span className="muted small">{data.risk_profile?"风险等级不匹配":"请先完成风险测评"}</span>}</td></tr>)}</tbody></table></div>}
      {selection && <form className="p3-inline-form wealth-amount-form" onSubmit={e=>{e.preventDefault();preview();}}><h3>{selection.position?"赎回":"申购"}{selection.product.name}</h3>
        <label>{selection.position?"赎回份额本金（元）":"申购金额（元）"}<input autoFocus required inputMode="decimal" value={amount} onChange={e=>setAmount(e.target.value)}/></label>
        <label>{selection.position?"到账账户":"付款账户"}<select required value={accountId} onChange={e=>setAccountId(e.target.value)}><option value="" disabled>请选择账户</option>{accounts.map(account=><option key={account.id} value={account.id}>{account.name} · 可用 {money(availableMoney(account))}</option>)}</select></label>
        <p className="muted small">{selection.position?`可用份额本金 ${money(selection.position.available_minor)}。`:`起购 ${money(selection.product.min_purchase_minor)}，申购费率 ${fee(selection.product.purchase_fee_bps)}。`}准确费用及结算日将在确认单中展示。</p>
        <button className="button secondary" disabled={disabled || !accountId || !amount}>准备确认单</button><button className="text-button" type="button" disabled={busy} onClick={()=>setSelection(null)}>收起</button>
      </form>}
      {section === "products" && <p className="muted small p3-spacing">以上为模拟产品，不承诺收益。<br/>按模拟固定净值1元计算，业务日T+1结算。<br/>申购受理后待结算，结算完成才计入持仓。</p>}
      {section === "positions" && <section className="section-gap"><h3>我的持仓</h3>{data.positions.length===0?<p className="muted p3-spacing">暂无已结算持仓。待结算申购请在订单中查看。</p>:data.positions.map(position=>{
        const product=data.products.find(p=>p.id===position.product_id);
        const locked=position.unlock_on>data.as_of;
        return <article className="subscription-row" key={position.id}><div><h3>{position.product_name}</h3><p>持仓本金 {money(position.principal_minor)} · 可用 {money(position.available_minor)}</p><p className="muted small">赎回待结算 {money(position.redemption_pending_minor)} · {locked?`锁定至 ${position.unlock_on}`:"已解锁"}</p></div><button className="button secondary" disabled={disabled || locked || position.available_minor<=0 || !product} onClick={()=>product && select(product,position)}>预览赎回</button></article>;
      })}</section>}
      {section === "orders" && <section className="section-gap"><h3>理财订单</h3>{data.orders.length===0?<p className="muted p3-spacing">暂无理财订单。</p>:data.orders.map(order=><article className="subscription-row" key={order.id}><div><h3>{data.products.find(p=>p.id===order.product_id)?.name ?? "理财产品"} · {order.action==="purchase"?"申购":"赎回"}</h3><p>本金 {money(order.amount_minor)} · 费用 {money(order.fee_minor)}</p><p className="muted small">{order.status==="pending_settlement"?`已受理，待结算 · 预计 ${order.settles_on}`:`已结算 · ${order.action==="purchase"?"已计入持仓":"已转入到账账户"}`} {order.action==="redeem" && `· ${order.status==="settled"?"到账":"预计到账"} ${money(order.payout_minor)}`}</p></div><span className="small">{order.requested_on}</span></article>)}</section>}
      {section === "orders" && <div className="demo-settlement section-gap"><p className="muted small">模拟业务日 {data.as_of} · 与电脑实际日期独立</p><button className="text-button" disabled={disabled} onClick={()=>{if(!disabled) void run(async()=>{await advanceWealthDay();if(alive.current) onRefresh();},true);}}>体验下一业务日结算</button><p className="muted small">仅推进模拟业务日，处理到期订单。</p></div>}
    </>}
  </section>;
}
