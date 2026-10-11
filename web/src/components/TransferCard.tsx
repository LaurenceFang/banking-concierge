import { useEffect, useRef, useState } from "react";
import {
  ArrowRightLeft,
  Check,
  ChevronDown,
  LoaderCircle,
  X,
} from "lucide-react";
import { ApiError, decidePlan, money, timestamp } from "../api";
import type { Account, DecisionResponse, Plan } from "../types";

interface Props {
  plan: Plan;
  accounts: Account[];
  chatBusy: boolean;
  onDecision: (id: string, response: DecisionResponse) => void;
  onBusyChange: (busy: boolean) => void;
}

export function TransferCard({
  plan,
  accounts,
  chatBusy,
  onDecision,
  onBusyChange,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<DecisionResponse | null>(null);
  const [now, setNow] = useState(Date.now());
  const lock = useRef(false);
  const expired = new Date(plan.expires_at).getTime() <= now;
  const pending = plan.status === "pending" && !expired && !receipt;
  useEffect(() => {
    if (plan.status !== "pending") return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [plan.status]);
  const handleDecision = async (decision: "approve" | "decline") => {
    if (lock.current || !pending || chatBusy) return;
    lock.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    try {
      const result = await decidePlan(plan.id, decision);
      setReceipt(result);
      onDecision(plan.id, result);
    } catch (failure) {
      setError(
        failure instanceof ApiError
          ? failure.message
          : "未能取得办理结果，请重试。",
      );
    } finally {
      setBusy(false);
      lock.current = false;
      onBusyChange(false);
    }
  };
  const source =
    accounts.find((account) => account.id === plan.source_account_id)?.name ??
    plan.source_account_id;
  const finished = receipt?.status ?? plan.status;
  return (
    <section
      className={`transfer-card ${!pending ? "settled" : ""}`}
      aria-label="转账确认"
    >
      <div className="transfer-title">
        <span className="transfer-symbol">
          {finished === "completed" ? (
            <Check size={22} />
          ) : finished === "declined" ? (
            <X size={22} />
          ) : (
            <ArrowRightLeft size={22} />
          )}
        </span>
        <h3>
          {finished === "completed"
            ? "转账已完成"
            : finished === "declined"
              ? "转账已取消"
              : plan.status === "superseded"
                ? "此计划已失效"
                : expired
                  ? "确认已过期"
                  : "确认转账"}
        </h3>
      </div>
      <div className="transfer-amount">
        {money(plan.amount_minor, plan.currency)}
      </div>
      <dl className="transfer-fields">
        <div>
          <dt>收款人</dt>
          <dd>{plan.recipient_name}</dd>
        </div>
        <div>
          <dt>付款账户</dt>
          <dd>{source}</dd>
        </div>
        <div>
          <dt>手续费</dt>
          <dd>{money(plan.fee_minor, plan.currency)}</dd>
        </div>
        <div className="transfer-total">
          <dt>合计扣款</dt>
          <dd>{money(plan.total_minor, plan.currency)}</dd>
        </div>
      </dl>
      <details className="transfer-details">
        <summary>
          转账详情
          <ChevronDown size={14} />
        </summary>
        <dl>
          <div>
            <dt>收款人编号</dt>
            <dd>{plan.recipient_id}</dd>
          </div>
          <div>
            <dt>收款账户</dt>
            <dd>{plan.destination_account_id}</dd>
          </div>
          <div>
            <dt>付款账户</dt>
            <dd>{plan.source_account_id}</dd>
          </div>
          <div>
            <dt>有效期至</dt>
            <dd>{timestamp(plan.expires_at)}</dd>
          </div>
          <div>
            <dt>执行时间</dt>
            <dd>确认后即时执行</dd>
          </div>
        </dl>
      </details>
      {pending && (
        <div className="transfer-actions">
          <button
            className="button secondary"
            disabled={busy || chatBusy}
            onClick={() => void handleDecision("decline")}
          >
            取消
          </button>
          <button
            className="button primary"
            disabled={busy || chatBusy}
            onClick={() => void handleDecision("approve")}
          >
            {busy ? (
              <>
                <LoaderCircle size={17} className="spin" />
                正在办理
              </>
            ) : (
              "确认转账"
            )}
          </button>
        </div>
      )}
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      {receipt && (
        <div className="receipt" role="status">
          <p>{receipt.message}</p>
          {receipt.operation_id && (
            <span>回执编号：{receipt.operation_id}</span>
          )}
        </div>
      )}
      {expired && plan.status === "pending" && !receipt && (
        <p className="muted small">
          请在对话中重新发起转账，获取新的确认计划。
        </p>
      )}
      {plan.status === "superseded" && (
        <p className="muted small">请以最新转账预览为准。</p>
      )}
    </section>
  );
}
