import type {
  ChatResponse,
  DecisionResponse,
  Overview,
  Conversation,
  ConversationDetail,
  BillingReport,
  ImportResult,
  SubscriptionAction,
  SubscriptionList,
  Action,
  CardAction, CardList, WealthAction, WealthOverview, WealthOrder, RiskAnswers, RiskProfile,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(
      "暂时连接不上服务，请检查本地服务是否已启动后重试。",
      "connection_failed",
    );
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      typeof data?.detail?.message === "string"
        ? data.detail.message
        : typeof data?.detail === "string"
          ? data.detail
          : "服务暂时不可用，请稍后重试。",
      data?.detail?.code,
    );
  }
  if (!data)
    throw new ApiError("服务返回了空响应，请稍后重试。", "empty_response");
  return data as T;
}

export const getOverview = () => request<Overview>("/overview");
export const sendChat = (message: string, conversationId?: string) =>
  request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  });

interface StreamHandlers {
  onStatus: (message: string) => void;
  onToken: (text: string) => void;
}

export async function streamChat(
  message: string,
  conversationId: string | undefined,
  handlers: StreamHandlers,
): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        message,
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    });
  } catch {
    throw new ApiError(
      "暂时连接不上服务。消息未自动重发，请检查连接后重试。",
      "connection_failed",
    );
  }
  // Only route-level rejection proves the stream request was not processed.
  // Once accepted, never replay a request that may have created a transfer plan.
  if (response.status === 404 || response.status === 405)
    return sendChat(message, conversationId);
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(
      data?.detail?.message ?? "服务暂时不可用，请稍后重试。",
      data?.detail?.code,
    );
  }
  if (
    !response.body ||
    !response.headers.get("content-type")?.includes("text/event-stream")
  ) {
    throw new ApiError(
      "未收到完整答复，消息未自动重发。请稍后重试。",
      "invalid_stream",
    );
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: ChatResponse | undefined;
  let finished = false;
  const consume = (block: string) => {
    let event = "message";
    const lines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (!lines.length) return;
    const data = JSON.parse(lines.join("\n"));
    if (event === "status" && typeof data.message === "string")
      handlers.onStatus(data.message);
    else if (event === "token" && typeof data.text === "string")
      handlers.onToken(data.text);
    else if (event === "result") result = data as ChatResponse;
    else if (event === "error")
      throw new ApiError(data.message ?? "答复中断，请稍后重试。", data.code);
    else if (event === "done") finished = true;
  };
  try {
    while (!finished) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      // Split on either LF or CRLF framing, including boundaries across chunks.
      let boundary = /\r?\n\r?\n/.exec(buffer);
      while (boundary) {
        consume(buffer.slice(0, boundary.index).replace(/\r\n/g, "\n"));
        buffer = buffer.slice(boundary.index + boundary[0].length);
        boundary = /\r?\n\r?\n/.exec(buffer);
      }
      if (done) {
        if (buffer.trim()) consume(buffer.replace(/\r\n/g, "\n"));
        break;
      }
    }
  } catch (failure) {
    if (failure instanceof ApiError) throw failure;
    throw new ApiError(
      "连接中断，未取得完整答复。消息未自动重发，请稍后重试。",
      "stream_interrupted",
    );
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  if (
    !result ||
    typeof result.reply !== "string" ||
    typeof result.conversation_id !== "string"
  ) {
    throw new ApiError(
      "答复尚未完成，消息未自动重发。请稍后重试。",
      "incomplete_stream",
    );
  }
  return result;
}
export const decidePlan = (
  planId: string,
  decision: "approve" | "decline",
  conversationId?: string,
) =>
  request<DecisionResponse>(`/plans/${encodeURIComponent(planId)}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, conversation_id: conversationId }),
  });

export const getConversations = () =>
  request<{ conversations: Conversation[] }>("/conversations");
export const createConversation = () =>
  request<Conversation>("/conversations", { method: "POST", body: "{}" });
export const renameConversation = (id: string, title: string) =>
  request<Conversation>(`/conversations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
export const getConversation = (id: string) =>
  request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`);
export const markRead = (id: string) =>
  request(`/conversations/${encodeURIComponent(id)}/read`, { method: "POST" });
export const getBillingReport = (start: string, end: string) =>
  request<BillingReport>(
    `/billing/report?${new URLSearchParams({ ...(start ? { start } : {}), ...(end ? { end } : {}) })}`,
  );
export const importBilling = (filename: string, content: string) =>
  request<ImportResult>("/billing/import", {
    method: "POST",
    body: JSON.stringify({ filename, content }),
  });
export const getSubscriptions = () =>
  request<SubscriptionList>("/subscriptions");
export const previewSubscription = (
  id: string,
  action: SubscriptionAction["action"],
  conversationId: string,
) =>
  request<SubscriptionAction>(
    `/subscriptions/${encodeURIComponent(id)}/preview`,
    {
      method: "POST",
      body: JSON.stringify({ action, conversation_id: conversationId }),
    },
  );
export const decideAction = (
  action: Action,
  decision: "approve" | "decline",
  conversationId: string,
) =>
  action.kind === "transfer"
    ? decidePlan(action.id, decision, conversationId)
    : request<DecisionResponse>(
        `/${action.kind === "card" ? "card" : action.kind === "wealth" ? "wealth" : "subscription"}-plans/${encodeURIComponent(action.id)}/decision`,
        {
          method: "POST",
          body: JSON.stringify({ decision, conversation_id: conversationId }),
        },
      );

export const money = (minor: number, currency = "CNY") =>
  new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(minor / 100);
// Decimal text is converted through integer arithmetic; never round excess decimals.
export function parseMinor(text: string): number {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text.trim());
  if (!match) throw new Error("请输入有效金额，最多两位小数。");
  const value = BigInt(match[1]) * 100n + BigInt((match[2] ?? "").padEnd(2, "0"));
  if (value <= 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("金额须大于零且在支持范围内。");
  return Number(value);
}
export const getCards = () => request<CardList>("/cards");
export const previewCard = (input: {action: CardAction["action"]; conversation_id: string; card_id?: string; amount_minor?: number; card_kind?: "debit" | "credit"}) => request<CardAction>("/cards/preview", {method:"POST",body:JSON.stringify(input)});
export const getWealth = () => request<WealthOverview>("/wealth");
export const saveRiskProfile = (answers: RiskAnswers) => request<RiskProfile>("/wealth/risk-profile", {method:"POST",body:JSON.stringify({answers})});
export const previewWealth = (input: {action: WealthAction["action"]; conversation_id: string; product_id?: string; account_id?: string; position_id?: string; amount_minor: number}) => request<WealthAction>("/wealth/preview", {method:"POST",body:JSON.stringify(input)});
export const advanceWealthDay = () => request<{as_of:string; settled_orders: WealthOrder[]}>("/wealth/demo/advance", {method:"POST",body:JSON.stringify({business_days:1})});
export const timestamp = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
};
