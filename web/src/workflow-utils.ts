import type { WorkflowList, WorkflowTask } from "./types";
export function workflowLeaveDecision(busy:boolean,dirty:boolean):"block"|"confirm"|"allow" {
 if(busy)return "block";
 return dirty?"confirm":"allow";
}
export const workflowStatus:Record<string,string>={scheduled:"已安排 · 尚未扣款",running:"正在执行",completed:"订单已确认",cancelled:"已取消",failed:"安排未完成",partial:"部分处理完成",unknown:"结果待核对",refund_pending:"安排未完成 · 退款处理中",expired:"已过期"};
export const workflowStepStatus:Record<string,string>={pending:"待执行",processing:"正在执行",confirmed:"订单已确认",unknown:"结果待核对",failed:"失败 · 未完成",cancelled:"已取消",refunded:"已退款",refund_pending:"退款处理中",fulfilled:"已履约 · 不可取消",skipped:"已停止",running:"正在执行"};
export function canCancelWorkflow(task:WorkflowTask){return ["scheduled","running","unknown","completed"].includes(task.status);}
export function canRecoverWorkflow(task:WorkflowTask){return ["unknown","refund_pending","partial"].includes(task.status);}
export function mergeWorkflowVersions(previous:WorkflowList|null,incoming:WorkflowList):WorkflowList {
 const prior=new Map(previous?.tasks.map(task=>[task.id,task])??[]);
 return {...incoming,tasks:incoming.tasks.map(task=>{const old=prior.get(task.id);return old && old.state_version>task.state_version?old:task;})};
}
export function workflowOutcome(task:WorkflowTask):string {
 if(task.refund_pending_minor>0)return "退款尚未到账，待退款不抵减当前净支出。";
 if(task.status==="completed")return "模拟订单已确认，不代表已配送。";
 if(task.refunded_minor>0 && task.status!=="completed")return "退款按实际入账更新；退款完成不表示本次安排成功。";
 return task.message;
}
