import type {
  WorkflowList, WorkflowAction, WorkflowInput, WorkflowTask,
  TaskList, TaskRecipient, TaskAction, TaskPreviewInput,
  ChatResponse,
  DecisionResponse,
  Overview,
  Conversation,
  ConversationDetail,
  BillingReport, BillingEntry, ImportBatch, PersonalRecord,
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

function validationMessage(detail: unknown): string | undefined {
  if (!Array.isArray(detail)) return undefined;
  const labels: Record<string,string> = {name:"名称",note:"备注",bank_name:"模拟银行名称",account_last4:"账户尾号",merchant_name:"模拟商户",price_minor:"单价",unit:"单位",description:"简短说明",cancel_supported:"取消规则",version:"资料版本",client_request_id:"保存请求标识"};
  const errors = detail.flatMap(issue => {
    if (!issue || typeof issue !== "object") return [];
    const location = Array.isArray(issue.loc) ? issue.loc.filter((part:unknown)=>part!=="body"&&part!=="query") : [];
    const field = String(location.at(-1)??"参数");
    const message = issue.type==="extra_forbidden" ? "包含不支持的字段" : issue.type==="missing" ? "请补充此项" : typeof issue.msg==="string" ? issue.msg : "请核对填写内容";
    return [`${labels[field]??field}：${message}`];
  });
  return errors.length ? `参数有误，${errors.join("；")}` : "参数有误，请核对填写内容。";
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
          : validationMessage(data?.detail) ?? "服务暂时不可用，请稍后重试。",
      data?.detail?.code,
    );
  }
  if (!data)
    throw new ApiError("服务返回了空响应，请稍后重试。", "empty_response");
  return data as T;
}

export const getOverview = () => request<Overview>("/overview");
export interface ChatContext { scope?: string; client_request_id?: string; retry_failed?: boolean }
export const sendChat = (message: string, conversationId?: string, context: ChatContext = {}) =>
  request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      ...context,
      message,
      ...(conversationId ? { conversation_id: conversationId } : {}),
    }),
  });

interface StreamHandlers {
  onStatus: (message: string) => void;
  onToken: (text: string) => void;
  onConversation?: (id: string) => void;
}

export async function streamChat(
  message: string,
  conversationId: string | undefined,
  handlers: StreamHandlers,
  context: ChatContext = {},
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
        ...context,
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
    return sendChat(message, conversationId, context);
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
    else if (event === "conversation" && typeof data.conversation_id === "string") handlers.onConversation?.(data.conversation_id);
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

export const getConversations = (scope?: string) =>
  request<{ conversations: Conversation[] }>(`/conversations${scope ? `?${new URLSearchParams({scope})}` : ""}`);
export const createConversation = (scope?: string, client_request_id?: string) =>
  request<Conversation>("/conversations", { method: "POST", body: JSON.stringify({scope, client_request_id}) });
export const pinConversation = (id: string, pinned: boolean) =>
  request<Conversation>(`/conversations/${encodeURIComponent(id)}/pin`, {
    method: "PATCH",
    body: JSON.stringify({ pinned }),
  });
export const renameConversation = (id: string, title: string) =>
  request<Conversation>(`/conversations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
export const getConversation = (id: string) =>
  request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`);
export const markRead = (id: string) =>
  request(`/conversations/${encodeURIComponent(id)}/read`, { method: "POST" });
export const getBillingReport = (start: string, end: string, filters: {query?:string;category?:string;source?:string} = {}) =>
  request<BillingReport>(
    `/billing/report?${new URLSearchParams({ ...(start ? { start } : {}), ...(end ? { end } : {}), ...Object.fromEntries(Object.entries(filters).filter(([,v])=>v)) })}`,
  );
export const importBilling = (filename: string, content: string, client_request_id?:string) =>
  request<ImportResult>("/billing/import", {
    method: "POST",
    body: JSON.stringify({ filename, content, client_request_id }),
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
        `/${action.kind === "workflow" ? "workflow" : action.kind === "task" ? "task" : action.kind === "card" ? "card" : action.kind === "wealth" ? "wealth" : "subscription"}-plans/${encodeURIComponent(action.id)}/decision`,
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

export const getTasks = () => request<TaskList>("/tasks");
export const getTaskRecipients = (query = "") => request<{recipients: TaskRecipient[]}>(`/tasks/recipients?${new URLSearchParams({query})}`);
export const previewTask = (input: TaskPreviewInput) => request<TaskAction>("/tasks/preview", {method:"POST",body:JSON.stringify(input)});
export const simulateTaskPayment = (id: string, participantId: string) => request<DecisionResponse>(`/tasks/${encodeURIComponent(id)}/demo-payment`, {method:"POST",body:JSON.stringify({participant_id:participantId})});
export const getTaskClock = () => request<{now:string;timezone:string}>("/tasks/clock");
export const advanceTaskClock = (minutes: number) => request<{now:string;timezone:string}>("/tasks/demo/advance", {method:"POST",body:JSON.stringify({minutes})});

export const getWorkflows = () => request<WorkflowList>("/workflows");
export const previewWorkflow = (input:WorkflowInput) => request<WorkflowAction>("/workflows/preview",{method:"POST",body:JSON.stringify(input)});
export const recoverWorkflow = (id:string) => request<WorkflowTask>(`/workflows/${encodeURIComponent(id)}/recover`,{method:"POST"});

export const getContacts=(query="")=>request<{contacts:import("./types").Contact[]}>(`/contacts?query=${encodeURIComponent(query)}`);
export const getManagedServices=(query="")=>request<{services:import("./types").ManagedService[]}>(`/workflow-services?query=${encodeURIComponent(query)}`);
function directoryBody(id:string|undefined,body:Record<string,unknown>){
 const payload={...body};
 if(id)delete payload.client_request_id;else delete payload.version;
 return JSON.stringify(payload);
}
export const saveContact=(id:string|undefined,body:Record<string,unknown>)=>request<import("./types").Contact>(id?`/contacts/${encodeURIComponent(id)}`:"/contacts",{method:id?"PATCH":"POST",body:directoryBody(id,body)});
export const saveManagedService=(id:string|undefined,body:Record<string,unknown>)=>request<import("./types").ManagedService>(id?`/workflow-services/${encodeURIComponent(id)}`:"/workflow-services",{method:id?"PATCH":"POST",body:directoryBody(id,body)});
export const removeDirectoryItem=(kind:"contacts"|"services",id:string,version:number)=>request<{removed:boolean;pending_tasks:number}>(`/${kind==="contacts"?"contacts":"workflow-services"}/${encodeURIComponent(id)}?version=${version}`,{method:"DELETE"});

export const saveBillingEntry=(id:string|undefined, payload:Record<string,unknown>)=>request<BillingEntry>(id?`/billing/entries/${encodeURIComponent(id)}`:"/billing/manual",{method:id?"PATCH":"POST",body:JSON.stringify(payload)});
export const removeBillingEntry=(id:string,version:number)=>request(`/billing/entries/${encodeURIComponent(id)}?version=${version}`,{method:"DELETE"});
export const getImportBatches=()=>request<{batches:ImportBatch[]}>("/billing/import-batches");
export const undoImportBatch=(id:string)=>request(`/billing/import-batches/${encodeURIComponent(id)}/undo`,{method:"POST"});
export const savePersonalRecord=(id:string|undefined,payload:Record<string,unknown>)=>request<PersonalRecord>(id?`/subscription-records/${encodeURIComponent(id)}`:"/subscription-records",{method:id?"PATCH":"POST",body:JSON.stringify(payload)});
export const removePersonalRecord=(id:string,version:number)=>request(`/subscription-records/${encodeURIComponent(id)}?version=${version}`,{method:"DELETE"});
export const setCandidateFeedback=(id:string,feedback:string,version:number)=>request(`/subscription-candidates/${encodeURIComponent(id)}/feedback`,{method:"PUT",body:JSON.stringify({feedback,version})});

export type WorkflowRevisionInput = {conversation_id:string; title?:string;recipient_label?:string;budget_minor?:number;account_id?:string;execute_at?:string|null;items?:{product_id:string;quantity:number}[]};
export const reviseWorkflow = (id:string,input:WorkflowRevisionInput) => request<WorkflowAction>(`/workflow-plans/${encodeURIComponent(id)}/revise`,{method:"POST",body:JSON.stringify(input)});
