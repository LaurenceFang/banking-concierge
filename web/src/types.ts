export interface Account {
  id: string;
  name: string;
  kind: string;
  balance_minor: number;
  currency: string;
}
export interface Transaction {
  id: string;
  account_id: string;
  description: string;
  amount_minor: number;
  currency: string;
  created_at: string;
}
export interface Overview {
  user: { id: string; name: string };
  accounts: Account[];
  transactions: Transaction[];
}
export interface Source {
  doc_id: string;
  title: string;
  excerpt: string;
  version: string;
}
export interface Plan {
  id: string;
  source_account_id: string;
  recipient_id: string;
  recipient_name: string;
  destination_account_id: string;
  amount_minor: number;
  fee_minor: number;
  total_minor: number;
  currency: string;
  status: string;
  expires_at: string;
}
export interface ChatResponse {
  conversation_id: string;
  reply: string;
  sources: Source[];
  plan?: Plan | null;
  action?: Action | null;
  message_id?: string;
}
export interface DecisionResponse {
  status: "completed" | "declined";
  message: string;
  operation_id?: string;
}
export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "notification";
  text: string;
  sources?: Source[];
  plan?: Plan | null;
  created_at: string;
  action?: Action | null;
  receipt?: DecisionResponse | null;
}
export interface Conversation {
  id: string;
  title: string;
  kind: "main" | "temporary";
  updated_at: string;
  unread_count: number;
}
export type Action =
  | (Plan & { kind: "transfer"; title: string; conversation_id?: string })
  | SubscriptionAction;
export interface SubscriptionAction {
  id: string;
  kind: "subscription";
  action: "cancel_subscription" | "stop_autopay";
  subscription_id: string;
  merchant: string;
  title: string;
  description: string;
  status: string;
  expires_at: string;
  conversation_id?: string;
}
export interface ConversationDetail {
  conversation: Conversation;
  messages: ChatMessage[];
  pending_action: Action | null;
}
export interface BillingEntry {
  id: string;
  account_id: string | null;
  date: string;
  merchant: string;
  raw_merchant: string;
  amount_minor: number;
  currency: string;
  category: string;
  transaction_kind: string;
  source: string;
  confidence: number;
  reason: string;
}
export interface BillingReport {
  start: string | null;
  end: string | null;
  coverage_start: string | null;
  coverage_end: string | null;
  entry_count: number;
  income_minor: number;
  expense_minor: number;
  refund_minor: number;
  net_expense_minor: number;
  categories: { category: string; amount_minor: number; count: number }[];
  anomalies: {
    entry_id: string;
    merchant: string;
    amount_minor: number;
    reason: string;
    severity: string;
  }[];
  entries: BillingEntry[];
  source_notice: string;
}
export interface ImportResult {
  imported: number;
  duplicates: number;
  errors: { row: number; message: string }[];
  message: string;
}
export interface Subscription {
  id: string;
  merchant: string;
  amount_minor: number;
  currency: string;
  cycle: string;
  next_charge_on: string;
  status: string;
  autopay_enabled: boolean;
  cancel_supported: boolean;
  source: string;
}
export interface Candidate {
  id: string;
  merchant: string;
  cycle: string;
  amount_minor: number;
  confidence: number;
  next_charge_on: string;
  price_change: boolean;
  missing_cycles: number;
  reason: string;
  entry_ids: string[];
  is_subscription_candidate: boolean;
}
export interface SubscriptionList {
  subscriptions: Subscription[];
  candidates: Candidate[];
}
