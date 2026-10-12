import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronsLeft,
  ChevronsRight,
  CreditCard,
  ArrowRightLeft,
  MessageCircle,
  Plus,
  Pencil,
  ReceiptText,
  Repeat2,
  UserRound,
  RefreshCw,
  ChartNoAxesCombined,
} from "lucide-react";
import {
  createConversation,
  getConversation,
  getConversations,
  getOverview,
  markRead,
  renameConversation,
} from "./api";
import type {
  Action,
  Conversation,
  ConversationDetail,
  Overview,
} from "./types";
import { AccountOverview } from "./components/AccountOverview";
import { ChatWorkspace } from "./components/ChatWorkspace";
import { ActionDrawer } from "./components/ActionDrawer";
import { BillingPage } from "./components/BillingPage";
import { SubscriptionsPage } from "./components/SubscriptionsPage";
import { CardsPage } from "./components/CardsPage";
import { WealthPage } from "./components/WealthPage";
type View = "chat" | "accounts" | "transactions" | "billing" | "subscriptions" | "cards" | "wealth";
const nav = [
  { id: "accounts" as const, label: "账户", icon: CreditCard },
  { id: "transactions" as const, label: "交易", icon: ArrowRightLeft },
  { id: "billing" as const, label: "账单", icon: ReceiptText },
  { id: "subscriptions" as const, label: "订阅", icon: Repeat2 },
  { id: "cards" as const, label: "卡片", icon: CreditCard },
  { id: "wealth" as const, label: "理财", icon: ChartNoAxesCombined },
];
function stored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function save(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* Storage may be unavailable in a private browser. */
  }
}
function readClosed(): string[] {
  try {
    const value = JSON.parse(stored("yx2.closed.v1") ?? "[]");
    return Array.isArray(value)
      ? value.filter((x) => typeof x === "string")
      : [];
  } catch {
    return [];
  }
}
export default function App() {
  const [view, setView] = useState<View>("chat");
  const [collapsed, setCollapsed] = useState(
    () => stored("yx2.rail.v1") === "closed" || window.innerWidth < 760,
  );
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState("");
  const activeRef = useRef("");
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [pending, setPending] = useState<Action | null>(null);
  const [drawer, setDrawer] = useState(false);
  const closed = useRef(new Set(readClosed()));
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [overviewError, setOverviewError] = useState("");
  const [error, setError] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revision, setRevision] = useState(0);
  const [renaming, setRenaming] = useState("");
  const [title, setTitle] = useState("");
  const loadVersion = useRef(0);
  const refreshOverview = useCallback(async () => {
    setLoading(true);
    try {
      setOverview(await getOverview());
      setOverviewError("");
    } catch (failure) {
      setOverviewError(
        failure instanceof Error ? failure.message : "无法读取账户。",
      );
    } finally {
      setLoading(false);
    }
  }, []);
  const refreshList = useCallback(async () => {
    const result = await getConversations();
    setConversations(result.conversations);
    return result.conversations;
  }, []);
  const acceptAction = useCallback((action: Action | null) => {
    const live = action?.status === "pending" ? action : null;
    setPending(live);
    setDrawer(!!live && !closed.current.has(`${activeRef.current}:${live.id}`));
  }, []);
  const loadConversation = useCallback(
    async (id: string) => {
      const v = ++loadVersion.current;
      try {
        const result = await getConversation(id);
        if (v !== loadVersion.current || activeRef.current !== id) return;
        setDetail(result);
        acceptAction(result.pending_action);
        setError("");
        await markRead(id);
        void refreshList().catch(() => {});
      } catch (failure) {
        if (v === loadVersion.current)
          setError(
            failure instanceof Error ? failure.message : "无法读取会话。",
          );
      }
    },
    [acceptAction, refreshList],
  );
  const select = useCallback(
    (id: string) => {
      activeRef.current = id;
      setActiveId(id);
      save("yx2.active.v1", id);
      setDetail(null);
      setPending(null);
      setDrawer(false);
      setChatBusy(false);
      setDecisionBusy(false);
      setActionBusy(false);
      setError("");
      setView("chat");
      if (window.innerWidth < 760) setCollapsed(true);
      void loadConversation(id);
    },
    [loadConversation],
  );
  useEffect(() => {
    void refreshOverview();
    let alive = true;
    void refreshList()
      .then((list) => {
        if (!alive) return;
        const selected =
          list.find((item) => item.id === stored("yx2.active.v1")) ?? list[0];
        if (selected) select(selected.id);
      })
      .catch((failure) => {
        if (alive) setError(failure.message);
      });
    return () => {
      alive = false;
    };
  }, [refreshList, refreshOverview, select]);
  async function create() {
    if (creating) return;
    setCreating(true);
    try {
      const conversation = await createConversation();
      await refreshList();
      select(conversation.id);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "新建失败。");
    } finally {
      setCreating(false);
    }
  }
  function refreshCurrent() {
    void refreshOverview();
    void loadConversation(activeRef.current);
    setRevision((value) => value + 1);
  }
  async function rename() {
    if (!title.trim()) return;
    try {
      await renameConversation(renaming, title.trim());
      setRenaming("");
      await refreshList();
      if (renaming === activeRef.current) void loadConversation(renaming);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "改名失败。");
    }
  }
  function closeDrawer() {
    if (pending) {
      closed.current.add(`${activeId}:${pending.id}`);
      save("yx2.closed.v1", JSON.stringify([...closed.current].slice(-200)));
    }
    setDrawer(false);
  }
  const current = conversations.find((item) => item.id === activeId);
  const heading =
    view === "chat"
      ? (current?.title ?? "日常助手")
      : nav.find((item) => item.id === view)?.label;
  function conversationRow(item: Conversation) {
    return (
      <div
        className={`conversation-row ${activeId === item.id && view === "chat" ? "selected" : ""}`}
        key={item.id}
      >
        {renaming === item.id ? (
          <form
            className="rename-form"
            onSubmit={(event) => {
              event.preventDefault();
              void rename();
            }}
          >
            <input
              aria-label="事项名称"
              autoFocus
              value={title}
              maxLength={80}
              onChange={(event) => setTitle(event.target.value)}
            />
            <button type="submit">保存</button>
            <button type="button" onClick={() => setRenaming("")}>
              取消
            </button>
          </form>
        ) : (
          <>
            <button
              className="conversation-select"
              title={item.title}
              onClick={() => select(item.id)}
            >
              <MessageCircle size={19} />
              <span>{item.title}</span>
              {item.unread_count > 0 && (
                <small className="unread">{item.unread_count}</small>
              )}
            </button>
            {item.kind === "temporary" && !collapsed && (
              <button
                className="icon-button rename-button"
                aria-label={`重命名${item.title}`}
                onClick={() => {
                  setRenaming(item.id);
                  setTitle(item.title);
                }}
              >
                <Pencil size={13} />
              </button>
            )}
          </>
        )}
      </div>
    );
  }
  return (
    <div className={`app-shell ${collapsed ? "rail-collapsed" : ""}`}>
      <nav className="nav-rail" aria-label="主导航">
        <div className="rail-brand">
          {!collapsed && <span>银小二</span>}
          <button
            className="icon-button"
            aria-label={collapsed ? "展开会话栏" : "收起会话栏"}
            onClick={() => {
              save("yx2.rail.v1", collapsed ? "open" : "closed");
              setCollapsed(!collapsed);
            }}
          >
            {collapsed ? (
              <ChevronsRight size={22} />
            ) : (
              <ChevronsLeft size={22} />
            )}
          </button>
        </div>
        <button
          className="new-conversation"
          aria-label="新事项"
          onClick={() => void create()}
          disabled={creating}
        >
          <Plus size={23} />
          {!collapsed && <span>新事项</span>}
        </button>
        <div className="conversation-list">
          {!collapsed && <p className="rail-caption">主会话</p>}
          {conversations
            .filter((item) => item.kind === "main")
            .map(conversationRow)}
          {!collapsed && <p className="rail-caption divided">临时事项</p>}
          {conversations
            .filter((item) => item.kind === "temporary")
            .map(conversationRow)}
        </div>
        <div className="utility-nav">
          {nav.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={view === id ? "selected" : ""}
              aria-current={view === id ? "page" : undefined}
              title={label}
              onClick={() => {
                setView(id);
                setActionBusy(false);
                if (window.innerWidth < 760) setCollapsed(true);
              }}
            >
              <Icon size={20} />
              {!collapsed && <span>{label}</span>}
            </button>
          ))}
        </div>
        <div className="profile">
          <span className="avatar">
            <UserRound size={20} />
          </span>
          {!collapsed && <span>{overview?.user.name ?? "演示用户"}</span>}
        </div>
      </nav>
      {!collapsed && (
        <button
          className="rail-backdrop"
          aria-label="关闭会话导航"
          onClick={() => setCollapsed(true)}
        />
      )}
      <main className="main-shell">
        <header className="app-header">
          <div>
            <h1>{heading}</h1>
            {view === "chat" && (
              <span className="muted small">
                {current?.kind === "temporary" ? "临时事项" : "主会话"}
              </span>
            )}
          </div>
          <div className="header-tools">
            {pending && !drawer && view !== "chat" && (
              <button className="text-button" onClick={() => setDrawer(true)}>
                查看确认单
              </button>
            )}
            <button
              className="icon-button"
              aria-label="刷新当前内容"
              disabled={chatBusy || decisionBusy || actionBusy}
              onClick={refreshCurrent}
            >
              <RefreshCw size={17} />
            </button>
            <span>
              模拟银行
              <br />
              {new Intl.DateTimeFormat("zh-CN", {
                timeZone: "Asia/Shanghai",
              }).format(new Date())}
            </span>
          </div>
        </header>
        {error && (
          <div className="shell-error" role="alert">
            {error}
            <button
              className="text-button"
              onClick={() =>
                activeId
                  ? void loadConversation(activeId)
                  : window.location.reload()
              }
            >
              重试
            </button>
          </div>
        )}
        <div className="workspace-layout">
          <div className="workspace-content">
            {detail && (
              <div className={`chat-holder ${view !== "chat" ? "hidden" : ""}`}>
                <ChatWorkspace
                  key={activeId}
                  detail={detail}
                  pending={pending}
                  onAction={acceptAction}
                  onOpen={() => setDrawer(true)}
                  onRefresh={refreshCurrent}
                  onBusy={(value) => {
                    if (value) loadVersion.current++;
                    setChatBusy(value);
                  }}
                  decisionBusy={decisionBusy || actionBusy}
                />
              </div>
            )}
            {view === "chat" ? (
              !detail && (
                <p className="loading-copy">
                  {error ? "暂时无法读取会话。" : "正在恢复会话…"}
                </p>
              )
            ) : view === "cards" || view === "wealth" ? (
              view === "cards" ? <CardsPage key={`cards:${activeId}`} conversationId={activeId} revision={revision} blocked={chatBusy || decisionBusy} onBusy={setActionBusy} onAction={(action)=>{if(action.conversation_id!==activeRef.current)return;acceptAction(action);void loadConversation(activeId);}} />
              : <WealthPage key={`wealth:${activeId}`} conversationId={activeId} revision={revision} accounts={overview?.accounts??[]} blocked={chatBusy || decisionBusy} onBusy={setActionBusy} onRefresh={refreshCurrent} onAction={(action)=>{if(action.conversation_id!==activeRef.current)return;acceptAction(action);void loadConversation(activeId);}} />
            ) : view === "billing" ? (
              <BillingPage revision={revision} />
            ) : view === "subscriptions" ? (
              <SubscriptionsPage
                key={activeId}
                conversationId={activeId}
                revision={revision}
                blocked={chatBusy || decisionBusy}
                onAction={(action) => {
                  acceptAction(action);
                  void loadConversation(activeId);
                }}
              />
            ) : (
              <div className="data-page">
                <AccountOverview
                  overview={overview}
                  loading={loading}
                  error={overviewError}
                  refresh={() => void refreshOverview()}
                  expanded
                  transactionsOnly={view === "transactions"}
                />
              </div>
            )}
          </div>
          {pending && drawer && (
            <ActionDrawer
              key={`${activeId}:${pending.id}`}
              action={pending}
              accounts={overview?.accounts ?? []}
              conversationId={activeId}
              chatBusy={chatBusy || actionBusy}
              onClose={closeDrawer}
              onBusy={setDecisionBusy}
              onDone={() => {
                setPending(null);
                setDrawer(false);
                setDecisionBusy(false);
                refreshCurrent();
              }}
            />
          )}
        </div>
      </main>
    </div>
  );
}
