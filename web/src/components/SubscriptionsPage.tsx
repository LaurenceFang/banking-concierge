import { useEffect, useRef, useState } from "react";
import { getSubscriptions, money, previewSubscription } from "../api";
import type { OpenRecord } from "./RecordPanel";
import type { Action, SubscriptionAction, SubscriptionList } from "../types";
const cycles: Record<string, string> = {
  weekly: "每周",
  monthly: "每月",
  quarterly: "每季度",
  yearly: "每年",
  annual: "每年",
};
export function SubscriptionsPage({
  conversationId, ensureConversation,
  onAction,
  revision,
  blocked, onManage, managementBlocked = false,
}: {
  conversationId: string; ensureConversation:(existingId?:string)=>Promise<string>;
  onAction: (action: Action) => void;
  revision: number;
  blocked: boolean; onManage?:OpenRecord; managementBlocked?:boolean;
}) {
  const [filter,setFilter]=useState("pending");
  const [data, setData] = useState<SubscriptionList | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const alive = useRef(true);
  const previewLock = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    void getSubscriptions()
      .then((result) => {
        if (active) {
          setData(result);
          setError("");
        }
      })
      .catch((failure) => {
        if (active) setError(failure.message);
      });
    return () => {
      active = false;
    };
  }, [revision]);
  async function preview(id: string, action: SubscriptionAction["action"]) {
    if (previewLock.current || blocked) return;
    previewLock.current = true;
    setBusy(id);
    setError("");
    try {
      const result = await previewSubscription(id, action, await ensureConversation());
      if (alive.current) onAction(result);
    } catch (failure) {
      if (alive.current)
        setError(failure instanceof Error ? failure.message : "预览失败。");
    } finally {
      previewLock.current = false;
      if (alive.current) setBusy("");
    }
  }
  return (
    <section className="data-page" aria-label="订阅管理">
      <div className="page-intro">
        <h2>订阅与周期支出</h2>
        <p>先核对合同，再决定取消订阅或停止代扣。</p>
      </div>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      <h3>模拟订阅合同</h3>
      <p className="muted small">
        取消订阅会结束商户合同；停止代扣只关闭自动支付，合同仍然有效。
      </p>
      {!data && !error && <p role="status">正在读取订阅…</p>}
      {data?.subscriptions.map((sub) => (
        <article className="subscription-row" key={sub.id}>
          <div>
            <h3>{sub.merchant}</h3>
            <p>
              {money(sub.amount_minor, sub.currency)} /{" "}
              {cycles[sub.cycle] ?? sub.cycle}{" "}
              <span className="muted">· 模拟合同</span>
            </p>
            <p className="muted">
              {sub.status === "cancelled"
                ? "合同已取消"
                : `合同有效 · ${sub.autopay_enabled ? "自动代扣开启" : "自动代扣已停止"}`}
              {sub.status !== "cancelled" &&
                sub.autopay_enabled &&
                ` · 下次扣款 ${sub.next_charge_on}`}
            </p>
          </div>
          <div className="row-actions">
            {sub.status === "active" && (
              <>
                {sub.cancel_supported ? (
                  <button
                    className="button secondary"
                    disabled={!!busy || blocked}
                    onClick={() => void preview(sub.id, "cancel_subscription")}
                  >
                    预览取消订阅
                  </button>
                ) : (
                  <span className="muted small">商户不支持在线取消</span>
                )}
                {sub.autopay_enabled && (
                  <button
                    className="text-button"
                    disabled={!!busy || blocked}
                    onClick={() => void preview(sub.id, "stop_autopay")}
                  >
                    预览停止代扣
                  </button>
                )}
              </>
            )}
          </div>
        </article>
      ))}
      <div className="management-entry-heading section-gap"><h3>个人记录</h3>{onManage&&<button className="button secondary" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"personal"})}>添加订阅记录</button>}</div>
      <p className="muted small">用于记录和应用内提醒；不会取消商户订阅或停止银行代扣。</p>
      {data?.personal_records?.map(record=><article className="subscription-row" key={record.id}><div><h3>{record.name}</h3><p>{money(record.amount_minor)} / {cycles[record.cycle]} · 个人记录</p><p className="muted">下次续费 {record.next_charge_on} · {record.reminder_enabled?`提前 ${record.lead_days} 天提醒`:"提醒已关闭"}{record.next_charge_on<new Date().toLocaleDateString("sv-SE")?" · 日期已过期，待更新":""}</p>{record.note&&<p className="muted small">{record.note}</p>}</div>{onManage&&<button className="text-button" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"personal",record})}>编辑 / 移除</button>}</article>)}
      {data&&!data.personal_records?.length&&<p className="muted">暂无个人订阅记录。</p>}
      <h3 className="section-gap">从账单识别的周期记录</h3>
      <div className="record-feedback-filter" aria-label="候选筛选">{[["pending","待处理"],["confirmed","已确认"],["not_subscription","不是订阅"],["ignored","已忽略"],["all","全部"]].map(([id,label])=><button className="text-button" key={id} aria-pressed={filter===id} onClick={()=>setFilter(id)}>{label}</button>)}</div>
      <p className="muted small">
        这些是规则推测的候选，不代表已查询到商户合同；预计日期以账单证据为准。
      </p>
      {data?.candidates.filter(item=>filter==="all"||filter==="pending"?filter==="all"||!item.feedback||item.feedback==="reset":item.feedback===filter).map((item) => (
        <article className="candidate-row" key={item.id}>
          <div className="stat-row">
            <h4>{item.merchant}</h4>
            <span>
              {money(item.amount_minor)} / {cycles[item.cycle] ?? item.cycle}
            </span>
          </div>
          <p>
            <span
              className={
                item.is_subscription_candidate ? "candidate-label" : "muted"
              }
            >
              {item.feedback === "confirmed" ? "已确认订阅" : item.feedback === "not_subscription" ? "已标记不是订阅" : item.feedback === "ignored" ? "已忽略" : item.is_subscription_candidate ? "疑似订阅" : "周期非订阅消费"}
            </span>{" "}
            · 预计 {item.next_charge_on} · 可信度{" "}
            {Math.round(item.confidence * 100)}%
          </p>
          <p className="muted">识别依据：{item.reason}</p>
          {(item.price_change || item.missing_cycles > 0) && (
            <p className="small">
              {item.price_change ? "检测到金额变化。" : ""}
              {item.missing_cycles > 0
                ? `存在 ${item.missing_cycles} 个缺失周期。`
                : ""}
            </p>
          )}
          <details>
            <summary>查看账单证据 · {item.entry_ids.length} 笔</summary>
            <div className="table-scroll"><table><thead><tr><th>日期</th><th>商户</th><th>金额</th><th>来源</th></tr></thead><tbody>{(item.evidence_entries??[]).slice().sort((a,b)=>b.date.localeCompare(a.date)).map(e=><tr key={e.id}><td>{e.date}</td><td>{e.merchant}</td><td>{money(e.amount_minor)}</td><td>{{bank:"模拟银行",demo:"演示账单",import:"CSV 导入",manual:"手工记账"}[e.source]??"账单"}</td></tr>)}</tbody></table></div>
          </details>
          {onManage&&<div className="record-actions"><button className="button secondary" disabled={blocked||managementBlocked} onClick={()=>onManage({kind:"candidate",candidate:item})}>整理候选 / 识别依据</button><span className="muted small">{item.feedback==="confirmed"?"已确认订阅":item.feedback==="ignored"?"已忽略":item.feedback==="not_subscription"?"已标记不是订阅":"待核对"}{item.personal_record_id?" · 已关联个人记录":""}</span></div>}
        </article>
      ))}
      {data && !data.candidates.length && (
        <p className="muted">暂无足够证据识别周期记录。</p>
      )}
    </section>
  );
}
