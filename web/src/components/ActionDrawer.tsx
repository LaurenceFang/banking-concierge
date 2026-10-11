import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { decideAction, money, timestamp } from "../api";
import type { Account, Action, DecisionResponse } from "../types";
interface Props {
  action: Action;
  accounts: Account[];
  conversationId: string;
  chatBusy: boolean;
  onClose: () => void;
  onBusy: (busy: boolean) => void;
  onDone: (result: DecisionResponse) => void;
}
export function ActionDrawer({
  action,
  accounts,
  conversationId,
  chatBusy,
  onClose,
  onBusy,
  onDone,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const lock = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, []);
  const expired = Date.parse(action.expires_at) <= now;
  async function decide(decision: "approve" | "decline") {
    if (lock.current || chatBusy || expired) return;
    lock.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    try {
      const result = await decideAction(action, decision, conversationId);
      if (alive.current) onDone(result);
    } catch (failure) {
      if (alive.current)
        setError(
          failure instanceof Error
            ? failure.message
            : "未能取得办理结果，请刷新会话核对。",
        );
    } finally {
      lock.current = false;
      if (alive.current) {
        setBusy(false);
        onBusy(false);
      }
    }
  }
  const source =
    action.kind === "transfer"
      ? accounts.find((account) => account.id === action.source_account_id)
      : null;
  return (
    <aside className="action-drawer" aria-label={action.title}>
      <div className="drawer-heading">
        <div>
          <h2>{action.title}</h2>
          <p className="muted">{expired ? "确认已过期" : "等待你确认"}</p>
        </div>
        <button
          className="icon-button"
          onClick={onClose}
          disabled={busy}
          aria-label="关闭确认面板"
        >
          <X size={23} />
        </button>
      </div>
      {action.kind === "transfer" ? (
        <>
          <div className="amount-block">
            <span className="muted">转账金额</span>
            <p>{money(action.amount_minor, action.currency)}</p>
          </div>
          <dl className="transfer-fields">
            <div>
              <dt>收款人</dt>
              <dd>{action.recipient_name}</dd>
            </div>
            <div>
              <dt>付款账户</dt>
              <dd>{source?.name ?? action.source_account_id}</dd>
            </div>
            {source && (
              <div>
                <dt>可用余额</dt>
                <dd>{money(source.balance_minor, source.currency)}</dd>
              </div>
            )}
            <div>
              <dt>手续费</dt>
              <dd>{money(action.fee_minor, action.currency)}</dd>
            </div>
            <div className="transfer-total">
              <dt>合计扣款</dt>
              <dd>{money(action.total_minor, action.currency)}</dd>
            </div>
          </dl>
          <details className="transfer-details">
            <summary>转账详情</summary>
            <p>收款账户：{action.destination_account_id}</p>
            <p>收款人编号：{action.recipient_id}</p>
            <p>有效期至：{timestamp(action.expires_at)}</p>
          </details>
        </>
      ) : (
        <>
          <div className="subscription-effect">
            <h3>{action.merchant}</h3>
            <p>{action.description}</p>
            <p>
              {action.action === "cancel_subscription"
                ? "确认后取消模拟商户订阅合同。"
                : "确认后仅停止自动代扣，商户订阅合同仍然有效。"}
            </p>
          </div>
          <p className="muted small">有效期至 {timestamp(action.expires_at)}</p>
        </>
      )}
      {expired ? (
        <p className="inline-error">请重新发起预览，获取新的确认单。</p>
      ) : (
        <div className="transfer-actions">
          <button
            className="button secondary"
            disabled={busy || chatBusy}
            onClick={() => void decide("decline")}
          >
            取消办理
          </button>
          <button
            className="button primary"
            disabled={busy || chatBusy}
            onClick={() => void decide("approve")}
          >
            {busy
              ? "正在办理…"
              : action.kind === "transfer"
                ? "确认转账"
                : action.action === "cancel_subscription"
                  ? "确认取消订阅"
                  : "确认停止代扣"}
          </button>
        </div>
      )}
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}
