export interface Account {
  id: string;
  name: string;
  kind: string;
  balance_minor: number;
  reserved_minor?: number;
  available_minor?: number;
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
  steps?: unknown[];
  task_id?: string;
  task_status?: string;
  state_version?: number;
  conversation_id?: string;
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
  pinned: boolean;
  pinned_at: string | null;
  scopes?: string[];
  id: string;
  title: string;
  kind: "main" | "temporary";
  updated_at: string;
  unread_count: number;
}
export type Action =
  | (Plan & { kind: "transfer"; title: string; conversation_id?: string })
  | SubscriptionAction | CardAction | WealthAction | TaskAction | WorkflowAction;
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
  version: number; tags: string[]; note: string; editable: boolean;
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
export interface ImportBatch { id:string; filename:string; imported:number; duplicates:number; errors:{row:number;message:string}[]; undone:boolean; created_at:string; remaining_entries:number; removed_individually:number; }
export interface PersonalRecord { id:string; name:string; amount_minor:number; currency:string; cycle:string; next_charge_on:string; category:string; note:string; reminder_enabled:boolean; lead_days:number; active:boolean; version:number; }
export interface ImportResult {
  batch_id?: string;
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
  feedback: "confirmed"|"not_subscription"|"ignored"|"reset"|null; feedback_version:number; personal_record_id:string|null; evidence_entries:Pick<BillingEntry,"id"|"date"|"merchant"|"amount_minor"|"source"|"category">[];
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
  personal_records: PersonalRecord[];
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

export interface TaskRecipient { id: string; name: string; bank_name: string; account_hint: string }
export interface TaskParticipant { id: string; name: string; share_minor: number; status: "self" | "pending" | "paid" | "cancelled"; operation_id?: string }
export interface TaskAction extends DomainAction {
  kind: "task";
  action: "create_aa" | "cancel_aa" | "schedule_transfer" | "cancel_schedule";
  task_id?: string; account_id?: string; total_minor?: number; collectable_minor?: number;
  participants?: TaskParticipant[]; recipient_id?: string; recipient_name?: string;
  amount_minor?: number; execute_at?: string; deadline_at?: string; reserved_minor?: number;
}
export interface AATask {
  id: string; title: string; account_id: string; total_minor: number; collectable_minor: number;
  collected_minor: number; status: "awaiting_payment" | "partial" | "completed" | "cancelled";
  participants: TaskParticipant[]; created_at: string; conversation_id: string; state_version: number;
}
export interface ScheduledTask {
  id: string; account_id: string; recipient_id: string; recipient_name: string; amount_minor: number;
  execute_at: string; deadline_at: string; status: "scheduled" | "completed" | "cancelled" | "expired" | "failed";
  hold_id: string; reserved_minor: number; operation_id?: string; message: string;
  conversation_id: string; state_version: number;
}
export interface TaskList { aa: AATask[]; schedules: ScheduledTask[]; now: string }
export interface TaskPreviewInput {
  action: TaskAction["action"]; conversation_id: string; title?: string; account_id?: string;
  total_minor?: number; participant_ids?: string[]; recipient_id?: string; amount_minor?: number;
  execute_at?: string; task_id?: string;
}

export interface WorkflowProduct {unit?:string;description?:string;id:string;merchant_id:string;merchant_name:string;name:string;price_minor:number;currency:string;version:number;cancel_supported:boolean}
export interface WorkflowItem {unit?:string;product_id:string;merchant_name:string;name:string;quantity:number;unit_price_minor:number;total_minor:number}
export interface WorkflowAction extends DomainAction {
  kind:"workflow";action:"arrange"|"cancel";task_id:string|null;account_id:string;recipient_label:string;
  budget_minor:number;total_minor:number;reserved_minor:number;execute_at:string|null;deadline_at:string|null;
  execute_immediately?:boolean;
  revision?:{before_total_minor?:number;after_total_minor?:number;source_plan_id:string;changes:{field:string;label:string;before:string;after:string;summary:string}[]};
  items:WorkflowItem[];cancellation_notice:string;
}
export interface WorkflowStep extends WorkflowItem {
  step_id:string;merchant_id:string;status:string;order_id:string|null;order_status:string|null;
  operation_id:string|null;refund_operation_id:string|null;refunded_minor:number;refund_pending_minor:number;message:string;
}
export interface WorkflowTask {
  id:string;title:string;recipient_label:string;account_id:string;budget_minor:number;total_minor:number;
  reserved_minor:number;paid_minor:number;refunded_minor:number;refund_pending_minor:number;net_spent_minor:number;
  status:string;execute_at:string;deadline_at:string;hold_id:string;steps:WorkflowStep[];conversation_id:string;
  plan_id:string;related_plan_ids:string[];state_version:number;message:string;failure_code:string|null;
}
export interface WorkflowList {catalog:WorkflowProduct[];tasks:WorkflowTask[];now:string}
export interface WorkflowInput {title?:string;action:"arrange"|"cancel";conversation_id:string;account_id?:string;recipient_label?:string;budget_minor?:number;items?:{product_id:string;quantity:number}[];execute_at?:string;task_id?:string}

export interface Contact {id:string;name:string;note:string;bank_name:string;account_hint:string;account_last4:string;can_transfer:boolean;version:number;active:boolean}
export interface ManagedService extends WorkflowProduct {unit:string;description:string;active:boolean}
