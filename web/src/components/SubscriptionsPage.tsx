import { useEffect, useRef, useState } from "react";
import { getSubscriptions, money, previewSubscription } from "../api";
import type { Action, SubscriptionAction, SubscriptionList } from "../types";
const cycles: Record<string, string> = {
  weekly: "每周",
  monthly: "每月",
  quarterly: "每季度",
  yearly: "每年",
  annual: "每年",
};
export function SubscriptionsPage({
  conversationId,
  onAction,
  revision,
  blocked,
}: {
  conversationId: string;
  onAction: (action: Action) => void;
  revision: number;
  blocked: boolean;
}) {
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
    if (previewLock.current || blocked || !conversationId) return;
    previewLock.current = true;
    setBusy(id);
    setError("");
    try {
      const result = await previewSubscription(id, action, conversationId);
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
      <h3 className="section-gap">从账单识别的周期记录</h3>
      <p className="muted small">
        这些是规则推测的候选，不代表已查询到商户合同；预计日期以账单证据为准。
      </p>
      {data?.candidates.map((item) => (
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
              {item.is_subscription_candidate ? "疑似订阅" : "周期非订阅消费"}
            </span>{" "}
            · 预计 {item.next_charge_on} · 可信度{" "}
            {Math.round(item.confidence * 100)}%
          </p>
          <p className="muted">{item.reason}</p>
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
            <p className="small evidence-ids">{item.entry_ids.join("、")}</p>
          </details>
        </article>
      ))}
      {data && !data.candidates.length && (
        <p className="muted">暂无足够证据识别周期记录。</p>
      )}
    </section>
  );
}
