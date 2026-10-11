import { useCallback, useEffect, useRef, useState } from "react";
import { CreditCard, List, MessageCircle, UserRound } from "lucide-react";
import { ApiError, getOverview } from "./api";
import type { Overview } from "./types";
import { AccountOverview } from "./components/AccountOverview";
import { ChatWorkspace } from "./components/ChatWorkspace";

type View = "chat" | "accounts" | "transactions";
const navigation = [
  { id: "chat" as const, label: "对话", icon: MessageCircle },
  { id: "accounts" as const, label: "账户", icon: CreditCard },
  { id: "transactions" as const, label: "交易", icon: List },
];

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const overviewVersion = useRef(0);
  const refresh = useCallback(async () => {
    const version = ++overviewVersion.current;
    setLoading(true);
    try {
      const result = await getOverview();
      if (version === overviewVersion.current) {
        setOverview(result);
        setError("");
      }
    } catch (failure) {
      if (version === overviewVersion.current)
        setError(
          failure instanceof ApiError
            ? failure.message
            : "暂时无法读取账户，请重试。",
        );
    } finally {
      if (version === overviewVersion.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  const overviewProps = {
    overview,
    loading,
    error,
    refresh: () => void refresh(),
  };
  return (
    <div className="app-shell">
      <nav className="nav-rail" aria-label="主导航">
        <a
          className="brand-mark"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            setView("chat");
          }}
          aria-label="银小二首页"
        >
          银
        </a>
        <div className="nav-items">
          {navigation.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${view === id ? "active" : ""}`}
              aria-current={view === id ? "page" : undefined}
              onClick={() => setView(id)}
            >
              <Icon size={24} strokeWidth={1.8} />
              <span>{label}</span>
            </button>
          ))}
        </div>
        <div className="profile">
          <UserRound size={23} />
          <span>{overview?.user.name ?? "演示用户"}</span>
        </div>
      </nav>
      <main className="main-shell">
        <header className="app-header">
          <div className="brand-title">
            银小二<span>你的银行助手</span>
          </div>
          <span className="demo-label">
            <span />
            模拟银行
          </span>
        </header>
        <div
          className={`workspace-layout ${view !== "chat" ? "single-view" : ""}`}
        >
          <div className={`chat-holder ${view !== "chat" ? "hidden" : ""}`}>
            <ChatWorkspace
              accounts={overview?.accounts ?? []}
              onRefresh={() => void refresh()}
            />
          </div>
          {view === "chat" ? (
            <aside className="account-sidebar" aria-label="账户与交易">
              <AccountOverview {...overviewProps} />
            </aside>
          ) : (
            <section
              className="full-overview"
              aria-label={view === "accounts" ? "账户总览" : "全部交易"}
            >
              <AccountOverview
                {...overviewProps}
                expanded
                transactionsOnly={view === "transactions"}
              />
            </section>
          )}
        </div>
      </main>
    </div>
  );
}
