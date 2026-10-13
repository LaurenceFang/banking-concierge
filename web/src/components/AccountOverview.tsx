import {
  ArrowDownLeft,
  ArrowUpRight,
  CreditCard,
  PiggyBank,
  RefreshCw,
} from "lucide-react";
import { availableMoney } from "../task-utils";
import { money, timestamp } from "../api";
import type { Overview } from "../types";

interface Props {
  overview: Overview | null;
  loading: boolean;
  error: string;
  refresh: () => void;
  expanded?: boolean;
  transactionsOnly?: boolean;
  accountsOnly?: boolean;
}

export function AccountOverview({
  overview,
  loading,
  error,
  refresh,
  expanded = false,
  transactionsOnly = false, accountsOnly = false,
}: Props) {
  return (
    <div className={`account-content ${expanded ? "expanded" : ""}`}>
      {!transactionsOnly && (
        <section aria-labelledby="accounts-heading">
          <div className="section-heading">
            <h2 id="accounts-heading">账户概览</h2>
            <button
              className="icon-button"
              onClick={refresh}
              disabled={loading}
              aria-label="刷新账户"
            >
              <RefreshCw size={16} className={loading ? "spin" : ""} />
            </button>
          </div>
          {error && (
            <div className="inline-error" role="alert">
              {error}
              <button onClick={refresh} disabled={loading}>
                重试
              </button>
            </div>
          )}
          {!overview && loading ? (
            <div className="skeleton-accounts" aria-label="正在读取账户">
              <div />
              <div />
            </div>
          ) : null}
          {overview?.accounts.map((account, index) => (
            <div className="account-row" key={account.id}>
              <span className="account-icon">
                {index === 0 ? (
                  <CreditCard size={23} />
                ) : (
                  <PiggyBank size={23} />
                )}
              </span>
              <div className="account-name">
                <h3>{account.name}</h3>
                <span>账面 {money(account.balance_minor, account.currency)} · 预留 {money(account.reserved_minor ?? 0, account.currency)}</span>
              </div>
              <strong className="account-balance">
                <small>可用 </small>{money(availableMoney(account), account.currency)}
              </strong>
            </div>
          ))}
          {overview && overview.accounts.length === 0 && (
            <p className="muted empty-list">暂无可用账户。</p>
          )}
        </section>
      )}
      {!accountsOnly && <section
        className="transactions-section"
        aria-labelledby="transactions-heading"
      >
        <div className="section-heading">
          <h2 id="transactions-heading">
            {transactionsOnly ? "交易记录" : "最近交易"}
          </h2>
          <span className="muted small">
            {overview ? `${overview.transactions.length} 笔` : ""}
          </span>
        </div>
        {transactionsOnly && error && (
          <div className="inline-error" role="alert">
            {error}
            <button onClick={refresh}>重试</button>
          </div>
        )}
        {overview && overview.transactions.length === 0 && (
          <p className="muted empty-list">
            还没有交易记录。完成转账后，会在这里显示。
          </p>
        )}
        <div className="transaction-list">
          {overview?.transactions
            .slice(0, expanded ? undefined : 5)
            .map((transaction) => (
              <div className="transaction-row" key={transaction.id}>
                <span className="transaction-icon">
                  {transaction.amount_minor >= 0 ? (
                    <ArrowDownLeft size={22} />
                  ) : (
                    <ArrowUpRight size={22} />
                  )}
                </span>
                <div className="transaction-description">
                  <h3>{transaction.description}</h3>
                  <time dateTime={transaction.created_at}>
                    {timestamp(transaction.created_at)}
                  </time>
                </div>
                <strong
                  className={transaction.amount_minor >= 0 ? "positive" : ""}
                >
                  {transaction.amount_minor > 0 ? "+" : ""}
                  {money(transaction.amount_minor, transaction.currency)}
                </strong>
              </div>
            ))}
        </div>
        {!overview && !loading && !error && (
          <p className="muted empty-list">暂无交易记录。</p>
        )}
      </section>}
    </div>
  );
}
