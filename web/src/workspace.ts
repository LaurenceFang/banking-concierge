export const sections = {
  accounts: [["overview", "资金总览"], ["accounts", "我的账户"], ["cards", "银行卡"]],
  bills: [["entries", "收支明细"], ["payments", "收付款"], ["analysis", "分析与预算"], ["import", "导入整理"]],
  wealth: [["positions", "我的持仓"], ["products", "产品比较"], ["orders", "理财订单"], ["risk", "风险档案"]],
  life: [["subscriptions", "订阅与周期支出"], ["arrangements", "我的安排"]],
} as const;
export type Domain = keyof typeof sections;
export function validScope(value: string): string | undefined {
  const parts = value.split("/");
  if (parts.length !== 2) return undefined;
  const [domain, section] = parts;
  return domain in sections && sections[domain as Domain].some(([key]) => key === section) ? value : undefined;
}
export function routeFromHash(hash: string) {
  const [path, query] = hash.replace(/^#\/?/, "").split("?");
  return {scope: validScope(path), conversation: new URLSearchParams(query).get("chat") ?? "", expanded: new URLSearchParams(query).get("expanded") === "1"};
}
export function routeHash(scope?: string, conversation = "", expanded = false) {
  const query = new URLSearchParams();
  if (conversation) query.set("chat", conversation);
  if (expanded) query.set("expanded", "1");
  return `#/${scope ?? ""}${query.size ? `?${query}` : ""}`;
}
