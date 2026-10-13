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
  order_id?: string;
  order_status?: "pending_settlement" | "settled";
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
  scopes?: string[];
  id: string;
  title: string;
  kind: "main" | "temporary";
  updated_at: string;
  unread_count: number;
}
export type Action =
  | (Plan & { kind: "transfer"; title: string; conversation_id?: string })
  | SubscriptionAction | CardAction | WealthAction;
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
export interface Card {
  id: string; name: string; kind: "debit" | "credit"; last4: string;
  account_id: string | null; status: "active" | "frozen" | "lost" | "closed";
  daily_limit_minor: number; credit_limit_minor: number | null;
  outstanding_minor: number; available_credit_minor: number | null; currency: string;
}
export interface CardList {
  cards: Card[];
  applications: { id: string; card_kind: "debit" | "credit"; status: "submitted"; submitted_at: string }[];
}
interface DomainAction {
  id: string; title: string; description: string; status: string; expires_at: string; conversation_id: string;
}
export interface CardAction extends DomainAction {
  kind: "card"; action: "freeze" | "unfreeze" | "report_lost" | "set_daily_limit" | "set_credit_limit" | "apply_card";
  card_id: string | null; card_name: string | null; card_kind: string | null; amount_minor: number | null;
  before: Record<string, unknown>; after: Record<string, unknown>;
}
export interface RiskAnswers {
  horizon: "short" | "medium" | "long";
  loss_tolerance: "none" | "low" | "moderate" | "high";
  experience: "none" | "basic" | "experienced";
  liquidity_need: "high" | "medium" | "low";
  objective: "preserve" | "balanced" | "growth";
}
export interface RiskProfile { answers: RiskAnswers; risk_level: number; risk_label: string; assessed_at: string; explanation: string }
export interface WealthProduct {
  id: string; name: string; risk_level: number; min_purchase_minor: number;
  purchase_fee_bps: number; redemption_fee_bps: number; lock_days: number;
  settlement_business_days: number; eligible: boolean; description: string;
}
export interface WealthPosition {
  id: string; product_id: string; product_name: string; principal_minor: number;
  available_minor: number; redemption_pending_minor: number; unlock_on: string; risk_level: number;
}
export interface WealthOrder {
  id: string; action: "purchase" | "redeem"; product_id: string; amount_minor: number;
  fee_minor: number; total_minor: number; payout_minor: number;
  status: "pending_settlement" | "settled"; requested_on: string; settles_on: string;
}
export interface WealthOverview {
  products: WealthProduct[]; risk_profile: RiskProfile | null;
  positions: WealthPosition[]; orders: WealthOrder[]; as_of: string;
}
export interface WealthAction extends DomainAction {
  kind: "wealth"; action: "purchase" | "redeem"; product_id: string; product_name: string;
  account_id: string; position_id: string | null; amount_minor: number; fee_minor: number;
  total_minor: number; payout_minor: number; settles_on: string; risk_level: number;
}
