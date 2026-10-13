import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
async function load(file){const source=await readFile(new URL(file,import.meta.url),'utf8');const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);}
const api=await load('../src/api.ts');
const {shanghaiInput,beijingTime,availableMoney,mergeTaskVersions}=await load('../src/task-utils.ts');
test('local datetime is interpreted in Beijing regardless of host zone and rejects invalid dates',()=>{
 assert.equal(shanghaiInput('2026-10-14T09:00'),'2026-10-14T09:00:00+08:00');
 assert.equal(new Date(shanghaiInput('2026-10-14T09:00')).toISOString(),'2026-10-14T01:00:00.000Z');
 assert.match(beijingTime('2026-10-14T01:00:00Z'),/09:00/);
 assert.throws(()=>shanghaiInput('2026-02-30T09:00'),/无效/);
 assert.throws(()=>shanghaiInput('2026-10-14T24:00'),/无效/);
 assert.throws(()=>shanghaiInput('2026-10-14'),/完整/);
});
test('available balance respects holds and old task state cannot overwrite paid or executed state',()=>{
 assert.equal(availableMoney({balance_minor:10000,reserved_minor:3000}),7000);
 assert.equal(availableMoney({balance_minor:10000,reserved_minor:3000,available_minor:6500}),6500);
 const paid={id:'aa',state_version:3,status:'partial',collected_minor:100};
 const completed={id:'schedule',state_version:2,status:'completed',reserved_minor:0};
 const result=mergeTaskVersions({aa:[paid],schedules:[completed],now:'later'},{aa:[{...paid,state_version:1,status:'awaiting_payment',collected_minor:0}],schedules:[{...completed,state_version:1,status:'scheduled',reserved_minor:500}],now:'earlier'});
 assert.equal(result.aa[0],paid);assert.equal(result.schedules[0],completed);
});
test('task preview, confirmation, participant payment and clock each use explicit separate endpoints',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);const calls=[];
 globalThis.fetch=async(url,init={})=>{calls.push({url,...init});return Response.json({status:'completed'});};
 await api.previewTask({action:'create_aa',conversation_id:'topic',total_minor:api.parseMinor('1.01'),account_id:'checking',participant_ids:['self','person-a','person-b']});
 await api.decideAction({id:'plan/1',kind:'task'},'approve','topic');
 await api.simulateTaskPayment('aa/1','person-a');
 await api.getTaskClock();await api.getTasks();await api.getTaskRecipients('张伟');
 await api.advanceTaskClock(5);
 assert.deepEqual(calls.map(x=>x.url),['/api/tasks/preview','/api/task-plans/plan%2F1/decision','/api/tasks/aa%2F1/demo-payment','/api/tasks/clock','/api/tasks','/api/tasks/recipients?query=%E5%BC%A0%E4%BC%9F','/api/tasks/demo/advance']);
 assert.deepEqual(JSON.parse(calls[0].body),{action:'create_aa',conversation_id:'topic',total_minor:101,account_id:'checking',participant_ids:['self','person-a','person-b']});
 assert.deepEqual(JSON.parse(calls[1].body),{decision:'approve',conversation_id:'topic'});
 assert.deepEqual(JSON.parse(calls[2].body),{participant_id:'person-a'});
 assert.equal(calls[3].method,undefined);assert.equal(calls[4].method,undefined);
 assert.deepEqual(JSON.parse(calls[6].body),{minutes:5});
});
