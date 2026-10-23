import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
async function load(file){const source=await readFile(new URL(file,import.meta.url),'utf8');const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);}
const api=await load('../src/api.ts');
const {mergeWorkflowVersions,workflowOutcome,canRecoverWorkflow,workflowLeaveDecision}=await load('../src/workflow-utils.ts');
test('workflow leave guard blocks in-flight saves and asks before discarding dirty edits',()=>{
 assert.equal(workflowLeaveDecision(true,true),'block');
 assert.equal(workflowLeaveDecision(true,false),'block');
 assert.equal(workflowLeaveDecision(false,true),'confirm');
 assert.equal(workflowLeaveDecision(false,false),'allow');
});
test('workflow APIs separate preview, explicit confirmation, cancellation and original-order recovery',async t=>{
 const prior=globalThis.fetch;t.after(()=>globalThis.fetch=prior);const calls=[];
 globalThis.fetch=async(url,init={})=>{calls.push({url,...init});return Response.json({id:'task1'});};
 await api.getWorkflows();
 await api.previewWorkflow({action:'arrange',conversation_id:'original',account_id:'checking',recipient_label:'家人',budget_minor:50000,items:[{product_id:'flowers',quantity:1},{product_id:'cake',quantity:1}],execute_at:'2026-10-14T09:00:00+08:00'});
 await api.decideAction({id:'plan/1',kind:'workflow'},'approve','original');
 await api.previewWorkflow({action:'cancel',conversation_id:'original',task_id:'task1'});
 await api.recoverWorkflow('task/1');
 assert.deepEqual(calls.map(x=>x.url),['/api/workflows','/api/workflows/preview','/api/workflow-plans/plan%2F1/decision','/api/workflows/preview','/api/workflows/task%2F1/recover']);
 assert.equal(calls[0].method,undefined);
 assert.deepEqual(JSON.parse(calls[2].body),{decision:'approve',conversation_id:'original'});
 assert.deepEqual(JSON.parse(calls[3].body),{action:'cancel',conversation_id:'original',task_id:'task1'});
 assert.equal(calls[4].method,'POST');assert.equal(calls[4].body,undefined);
 assert.equal(JSON.parse(calls[1].body).budget_minor,50000);
});
test('refund progress retains actual net spent and older snapshots cannot reverse refunded state',()=>{
 const pending={id:'task',state_version:3,status:'refund_pending',paid_minor:19900,refunded_minor:0,refund_pending_minor:19900,net_spent_minor:19900,message:'等待退款'};
 assert.match(workflowOutcome(pending),/尚未到账/);assert.equal(canRecoverWorkflow(pending),true);
 const refunded={...pending,state_version:4,status:'failed',refunded_minor:19900,refund_pending_minor:0,net_spent_minor:0};
 const result=mergeWorkflowVersions({catalog:[],tasks:[refunded],now:'later'},{catalog:[],tasks:[pending],now:'earlier'});
 assert.equal(result.tasks[0],refunded);assert.equal(result.tasks[0].net_spent_minor,0);
 assert.match(workflowOutcome(refunded),/不表示本次安排成功/);
 assert.match(workflowOutcome({...refunded,status:'completed'}),/不代表已配送/);
});

test('revision preserves omitted fields and distinguishes immediate execution from unchanged schedule',async t=>{
 const prior=globalThis.fetch;t.after(()=>globalThis.fetch=prior);const calls=[];
 globalThis.fetch=async(url,init)=>{calls.push({url,...init});return Response.json({id:'replacement'});};
 await api.reviseWorkflow('plan/old',{conversation_id:'original',budget_minor:45000});
 await api.reviseWorkflow('plan/old',{conversation_id:'original',execute_at:null,items:[{product_id:'flowers',quantity:2}]});
 assert.equal(calls[0].url,'/api/workflow-plans/plan%2Fold/revise');
 assert.equal(calls[0].method,'POST');
 assert.deepEqual(JSON.parse(calls[0].body),{conversation_id:'original',budget_minor:45000});
 assert.deepEqual(JSON.parse(calls[1].body),{conversation_id:'original',execute_at:null,items:[{product_id:'flowers',quantity:2}]});
});
