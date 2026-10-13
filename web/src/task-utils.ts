import type { Account, TaskList } from "./types";
export const availableMoney = (account: Account) => account.available_minor ?? account.balance_minor - (account.reserved_minor ?? 0);
export function shanghaiInput(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error("请选择完整的执行日期与时间（北京时间）。");
  const result = `${value}:00+08:00`;
  const date = new Date(result);
  if (!Number.isFinite(date.getTime()) || new Date(date.getTime() + 8 * 3600000).toISOString().slice(0,16) !== value) throw new Error("执行日期或时间无效。");
  return result;
}
export function beijingTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "时间不可用";
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(date);
}
export function mergeTaskVersions(previous: TaskList | null, incoming: TaskList): TaskList {
  function merge<T extends {id:string;state_version:number}>(oldRows: T[], newRows: T[]): T[] {
    const old = new Map(oldRows.map(row => [row.id,row]));
    return newRows.map(row => {const prior=old.get(row.id); return prior && prior.state_version > row.state_version ? prior : row;});
  }
  return {...incoming, aa:merge(previous?.aa ?? [],incoming.aa),schedules:merge(previous?.schedules ?? [],incoming.schedules)};
}
export const taskStatus: Record<string,string> = {awaiting_payment:"待收款",partial:"部分到账",completed:"已完成",cancelled:"已取消",scheduled:"已安排 · 尚未扣款",expired:"已过期 · 预留已释放",failed:"执行失败 · 预留已释放"};
export const participantStatus: Record<string,string> = {self:"自担",pending:"待付",paid:"已到账",cancelled:"已停止"};
