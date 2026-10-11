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
}
export interface DecisionResponse {
  status: "completed" | "declined";
  message: string;
  operation_id?: string;
}
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  sources?: Source[];
  plan?: Plan | null;
  createdAt: string;
}
