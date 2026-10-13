import { useEffect, useRef, useState } from "react";
import { ArrowUp, ChevronDown, LoaderCircle } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { streamChat, timestamp } from "../api";
import type { Action, ChatMessage, ConversationDetail } from "../types";

interface Props {
  detail: ConversationDetail;
  pending: Action | null;
  onAction: (action: Action | null) => void;
  onRefresh: () => void;
  onOpen: () => void;
  onBusy: (busy: boolean) => void;
  decisionBusy: boolean;
  scope?: string;
  onCreated?: (id: string) => void;
}
export function ChatWorkspace({
  detail,
  pending,
  onAction,
  onRefresh,
  onOpen,
  onBusy,
  decisionBusy, scope, onCreated,
}: Props) {
  const [messages, setMessages] = useState(detail.messages);
  const draftKey = `yx2.draft.${detail.conversation.id}`;
  const [draft, setDraft] = useState(() => { try { return localStorage.getItem(draftKey) ?? ""; } catch { return ""; } });
  const retryKey = `yx2.retry.${detail.conversation.id}`;
  const retry = useRef<{message:string; id:string; serverId?:string} | null>((() => {try { return JSON.parse(localStorage.getItem(retryKey) ?? "null"); } catch { return null; }})());
  const serverId = useRef(retry.current?.serverId ?? (detail.conversation.id.startsWith("compose:") ? undefined : detail.conversation.id));
  const persistRetry = () => {try { if (retry.current) localStorage.setItem(retryKey, JSON.stringify(retry.current)); else localStorage.removeItem(retryKey); } catch {}};
  useEffect(() => { try { localStorage.setItem(draftKey, draft); } catch {} }, [draft, draftKey]);
  const [busy, setBusy] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  const lock = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    setMessages(detail.messages);
  }, [detail.messages]);
  useEffect(() => {
    if (nearBottom.current) bottom.current?.scrollIntoView({ block: "end" });
  }, [messages, streamText, status]);
  async function submit(text: string) {
    const message = text.trim();
    if (!message || lock.current || decisionBusy) return;
    lock.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    setStreamText("");
    setStatus("正在查询并整理答复…");

    nearBottom.current = true;
    const isRetry = retry.current?.message === message;
    const requestId = isRetry ? retry.current!.id : crypto.randomUUID();
    retry.current = {message, id: requestId, serverId: serverId.current};
    persistRetry();
    setMessages((previous) => previous.some(item => item.id === requestId) || (isRetry && previous.filter(item=>item.role === "user").at(-1)?.text === message) ? previous : [
      ...previous,
      {
        id: requestId,
        role: "user",
        text: message,
        created_at: new Date().toISOString(),
      },
    ]);
    try {
      const result = await streamChat(message, serverId.current, {
        onConversation: (id) => { serverId.current = id; if(retry.current)retry.current.serverId=id; persistRetry(); },
        onStatus: (value) => {
          if (alive.current) setStatus(value);
        },
        onToken: (value) => {
          if (alive.current) setStreamText((previous) => previous + value);
        },
      }, {scope, client_request_id: requestId, ...(isRetry ? {retry_failed: true} : {})});
      retry.current = null;
      persistRetry();
      try { localStorage.removeItem(draftKey); } catch {}
      if (!alive.current) return;
      setDraft("");
      serverId.current = result.conversation_id;
      if (detail.conversation.id.startsWith("compose:")) { onCreated?.(result.conversation_id); return; }
      const action =
        result.action ??
        (result.plan
          ? { ...result.plan, kind: "transfer" as const, title: "转账确认" }
          : null);
      setMessages((previous) => [
        ...previous,
        {
          id: result.message_id ?? crypto.randomUUID(),
          role: "assistant",
          text: result.reply,
          sources: result.sources,
          action,
          created_at: new Date().toISOString(),
        },
      ]);
      if (action) onAction(action);
      onRefresh();
    } catch (failure) {
      if (alive.current) {
        setError(failure instanceof Error ? failure.message : "消息发送失败。");
        setDraft(message);
      }
    } finally {
      lock.current = false;
      if (alive.current) {
        setBusy(false);
        onBusy(false);
        setStreamText("");
      }
    }
  }
  function renderMessage(message: ChatMessage) {
    const actionId = message.action?.id ?? message.plan?.id;
    return (
      <article className={`message ${message.role}`} key={message.id} tabIndex={0}
        aria-label={message.role === "user" ? "你的消息" : message.role === "notification" ? "业务通知" : "银小二的回复"}>
        <div className="message-content">
          <time className="message-time" dateTime={message.created_at}>
            {timestamp(message.created_at)}
          </time>
          <div
            className={`message-bubble ${message.role !== "user" ? "markdown" : ""}`}
          >
            {message.role === "user" ? (
              message.text
            ) : (
              <Markdown remarkPlugins={[remarkGfm]}>{message.text}</Markdown>
            )}
          </div>
          {!!message.sources?.length && (
            <details className="sources">
              <summary>
                查看依据 · {message.sources.length} 份资料{" "}
                <ChevronDown size={14} />
              </summary>
              {message.sources.map((source, index) => (
                <div className="source" key={`${source.doc_id}-${index}`}>
                  <h4>{source.title}</h4>
                  <small>版本 {source.version}</small>
                  <p>{source.excerpt}</p>
                </div>
              ))}
            </details>
          )}
          {actionId && pending?.id === actionId && (
            <button className="text-button action-link" onClick={onOpen}>
              查看确认单 <span aria-hidden="true">›</span>
            </button>
          )}
          {(message.action?.kind === "transfer" || message.action?.kind === "task" || message.receipt?.task_id || message.plan) && <a className="text-button action-link" href="#/bills/payments">查看收付款 ›</a>}
          {message.receipt && (
            <div className="receipt">
              {message.role !== "notification" && <p>{message.receipt.message}</p>}
              {message.receipt.operation_id && (
                <details className="receipt-details">
                  <summary>回执详情</summary>
                  <small>回执编号：{message.receipt.operation_id}</small>
                </details>
              )}
            </div>
          )}
        </div>
      </article>
    );
  }
  return (
    <section className="chat-workspace" aria-label="对话助手">
      <div
        className="chat-scroll"
        onScroll={(event) => {
          const el = event.currentTarget;
          nearBottom.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 150;
        }}
      >
        {!messages.length && (
          <div className="empty-conversation">
            <span className="avatar">
              <span className="assistant-dot" />
            </span>
            <div>
              <p>
                今天想聊点什么？
                <br />
                {detail.conversation.kind === "main"
                  ? "Anything you can ask."
                  : "这件事，我们从这里开始聊。"}
              </p>
              <div className="quick-actions">
                {["查询账户余额", "查看最近交易", "转账规则和手续费"].map(
                  (text) => (
                    <button
                      className="text-button"
                      key={text}
                      disabled={busy || decisionBusy}
                      onClick={() => void submit(text)}
                    >
                      {text} ›
                    </button>
                  ),
                )}
              </div>
            </div>
          </div>
        )}
        <div role="log" aria-label="对话消息" aria-live="polite">
          {messages.map(renderMessage)}
          {busy && (
            <article className="message assistant">
              <div className="message-content">
                <div className="markdown">
                  <Markdown remarkPlugins={[remarkGfm]}>{streamText}</Markdown>
                </div>
                <p className="thinking" role="status">
                  <LoaderCircle size={15} className="spin" />
                  {status}
                </p>
              </div>
            </article>
          )}
        </div>
        <div ref={bottom} />
      </div>
      <div className="composer-area">
        {error && (
          <p className="inline-error" role="alert">
            {error} 内容已保留，未自动重发。
            <button className="text-button" disabled={busy || decisionBusy} onClick={() => retry.current && void submit(retry.current.message)}>重试回复</button>
          </p>
        )}
        {pending && (
          <button className="text-button pending-link" onClick={onOpen}>
            {pending.title} · 查看确认单 ›
          </button>
        )}
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(draft);
          }}
        >
          <label className="sr-only" htmlFor="chat-input">
            发送消息给银小二
          </label>
          <textarea
            id="chat-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void submit(draft);
              }
            }}
            placeholder={
              detail.conversation.kind === "main"
                ? "有什么想问，或想让我帮你办的事？"
                : "继续聊，或补充这件事…"
            }
            rows={1}
            maxLength={4000}
            disabled={busy || decisionBusy}
          />
          <button
            className="send-button"
            aria-label="发送消息"
            disabled={busy || decisionBusy || !draft.trim()}
          >
            {busy ? (
              <LoaderCircle size={21} className="spin" />
            ) : (
              <ArrowUp size={24} />
            )}
          </button>
        </form>
        <div className="composer-note">
          <span>所有操作使用模拟账户与产品</span>
          <span>Enter 发送 · Shift + Enter 换行</span>
        </div>
      </div>
    </section>
  );
}
