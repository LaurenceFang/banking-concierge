import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  ArrowRightLeft,
  ChevronDown,
  CreditCard,
  Info,
  List,
  LoaderCircle,
  Send,
  UserRound,
} from "lucide-react";
import { ApiError, streamChat, timestamp } from "../api";
import type { Account, ChatMessage, DecisionResponse } from "../types";
import { TransferCard } from "./TransferCard";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const suggestions = [
  { label: "查询余额", message: "帮我查询账户余额", icon: CreditCard },
  { label: "最近交易", message: "帮我查看最近的交易记录", icon: List },
  {
    label: "转账规则",
    message: "请介绍转账规则和手续费",
    icon: ArrowRightLeft,
  },
];
interface Props {
  accounts: Account[];
  onRefresh: () => void;
}

export function ChatWorkspace({ accounts, onRefresh }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationId] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamStatus, setStreamStatus] = useState("正在查询并整理答复…");
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const decisionLock = useRef(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  useEffect(() => {
    if (nearBottom.current)
      bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [messages, busy, error, streamText, streamStatus]);
  const submit = async (text: string) => {
    const message = text.trim();
    if (!message || lock.current || decisionLock.current) return;
    lock.current = true;
    setBusy(true);
    setStreamText("");
    setStreamStatus("正在查询并整理答复…");
    setError("");
    setDraft("");
    nearBottom.current = true;
    setMessages((previous) => [
      ...previous.map((item) =>
        item.plan?.status === "pending"
          ? { ...item, plan: { ...item.plan, status: "superseded" } }
          : item,
      ),
      {
        id: crypto.randomUUID(),
        role: "user",
        text: message,
        createdAt: new Date().toISOString(),
      },
    ]);
    try {
      const result = await streamChat(message, conversationId, {
        onStatus: setStreamStatus,
        onToken: (text) => setStreamText((previous) => previous + text),
      });
      setConversationId(result.conversation_id);
      setMessages((previous) => [
        ...previous.map((item) =>
          item.plan?.status === "pending"
            ? { ...item, plan: { ...item.plan, status: "superseded" } }
            : item,
        ),
        {
          id: crypto.randomUUID(),
          role: "assistant",
          text: result.reply,
          sources: result.sources ?? [],
          plan: result.plan,
          createdAt: new Date().toISOString(),
        },
      ]);
      onRefresh();
    } catch (failure) {
      setError(
        failure instanceof ApiError
          ? failure.message
          : "消息发送失败，请稍后重试。",
      );
      setDraft(message);
    } finally {
      setBusy(false);
      setStreamText("");
      lock.current = false;
      requestAnimationFrame(() => input.current?.focus());
    }
  };
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit(draft);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void submit(draft);
    }
  };
  const onDecision = (id: string, response: DecisionResponse) => {
    setMessages((previous) =>
      previous.map((item) =>
        item.plan?.id === id
          ? { ...item, plan: { ...item.plan, status: response.status } }
          : item,
      ),
    );
    onRefresh();
  };
  const onDecisionBusy = (value: boolean) => {
    decisionLock.current = value;
    setDecisionBusy(value);
  };
  return (
    <section className="chat-workspace" aria-label="对话助手">
      <div
        className="chat-scroll"
        ref={scroller}
        onScroll={() => {
          const element = scroller.current;
          if (element)
            nearBottom.current =
              element.scrollHeight - element.scrollTop - element.clientHeight <
              160;
        }}
      >
        <div className="welcome">
          <h1>今天，想办理什么？</h1>
          <p>查询账户、了解业务，或准备一笔转账。</p>
          <div className="quick-actions">
            {suggestions.map(({ label, message, icon: Icon }) => (
              <button
                className="button secondary"
                key={label}
                disabled={busy || decisionBusy}
                onClick={() => void submit(message)}
              >
                <Icon size={20} />
                {label}
              </button>
            ))}
          </div>
        </div>
        <div
          className="messages"
          role="log"
          aria-label="对话消息"
          aria-live="polite"
          aria-relevant="additions text"
        >
          {messages.length === 0 && (
            <div className="conversation-empty">
              <span className="assistant-avatar">银</span>
              <p>
                你好，我是银小二。
                <br />
                想了解一项业务，或办理一笔转账，都可以从这里开始。
              </p>
              <p className="muted small">
                转账会先生成确认单，核对后再由你确认。
              </p>
            </div>
          )}
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <span
                className={
                  message.role === "user" ? "user-avatar" : "assistant-avatar"
                }
                aria-hidden="true"
              >
                {message.role === "user" ? <UserRound size={22} /> : "银"}
              </span>
              <div className="message-content">
                <div
                  className={`message-bubble ${message.role === "assistant" ? "markdown" : ""}`}
                >
                  {message.role === "assistant" ? (
                    <Markdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        a: ({ children, href }) => (
                          <a
                            href={href}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {children}
                          </a>
                        ),
                      }}
                    >
                      {message.text}
                    </Markdown>
                  ) : (
                    message.text
                  )}
                </div>
                <time className="message-time" dateTime={message.createdAt}>
                  {timestamp(message.createdAt)}
                </time>
                {message.sources && message.sources.length > 0 && (
                  <details className="sources">
                    <summary>
                      查看依据 · {message.sources.length} 份资料
                      <ChevronDown size={14} />
                    </summary>
                    {message.sources.map((source, index) => (
                      <div className="source" key={`${source.doc_id}-${index}`}>
                        <h4>{source.title}</h4>
                        <span>版本 {source.version}</span>
                        <p>{source.excerpt}</p>
                      </div>
                    ))}
                  </details>
                )}
                {message.plan && (
                  <TransferCard
                    plan={message.plan}
                    accounts={accounts}
                    chatBusy={busy || decisionBusy}
                    onDecision={onDecision}
                    onBusyChange={onDecisionBusy}
                  />
                )}
              </div>
            </article>
          ))}
          {busy && (
            <div className="message assistant">
              <span className="assistant-avatar" aria-hidden="true">
                银
              </span>
              <div className="message-content">
                {streamText && (
                  <div className="message-bubble markdown streaming">
                    <Markdown remarkPlugins={[remarkGfm]}>
                      {streamText}
                    </Markdown>
                  </div>
                )}
                <div className="thinking" role="status">
                  <LoaderCircle size={17} className="spin" />
                  {streamStatus}
                </div>
              </div>
            </div>
          )}
          {error && (
            <div className="chat-error" role="alert">
              <Info size={18} />
              <div>
                {error}
                <p>内容已保留在输入框中，可以重新发送。</p>
              </div>
            </div>
          )}
          <div ref={bottom} />
        </div>
      </div>
      <div className="composer-area">
        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="chat-input" className="sr-only">
            发送消息给银小二
          </label>
          <textarea
            ref={input}
            id="chat-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="想办理什么？直接告诉我…"
            rows={2}
            maxLength={4000}
            disabled={busy || decisionBusy}
          />
          <button
            className="send-button"
            type="submit"
            disabled={busy || decisionBusy || !draft.trim()}
            aria-label="发送消息"
          >
            {busy ? (
              <LoaderCircle size={22} className="spin" />
            ) : (
              <Send size={22} />
            )}
          </button>
        </form>
        <div className="composer-note">
          <span>
            <Info size={14} />
            仅连接模拟账本。
          </span>
          <span className="keyboard-hint">Enter 发送 · Shift + Enter 换行</span>
        </div>
      </div>
    </section>
  );
}
