import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
async function load(file) {
  const source=await readFile(new URL(file,import.meta.url),'utf8');
  const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
}
const {sections,validScope,routeHash,routeFromHash}=await load('../src/workspace.ts');
const {getConversations,streamChat}=await load('../src/api.ts');
test('four primary entries lead directly to implemented defaults; all routes round-trip',()=>{
  assert.deepEqual(Object.keys(sections),['accounts','bills','wealth','life']);
  assert.equal(sections.bills[0][0],'entries');
  for(const [domain,tabs] of Object.entries(sections))for(const [section] of tabs){
    const scope=`${domain}/${section}`;
    assert.deepEqual(routeFromHash(routeHash(scope,'conversation A',true)),{scope,conversation:'conversation A',expanded:true});
  }
  assert.equal(validScope('bills/entries/extra'),undefined);
  assert.equal(validScope('accounts/loans'),undefined);
  assert.equal(routeFromHash('#/').scope,undefined);
});
test('scoped history request encodes stable scope and home requests global list',async t=>{
  const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);const urls=[];
  globalThis.fetch=async url=>{urls.push(url);return Response.json({conversations:[]});};
  await getConversations('bills/payments');await getConversations();
  assert.deepEqual(urls,['/api/conversations?scope=bills%2Fpayments','/api/conversations']);
});
test('first send carries scope and stable retry id, consumes early conversation event',async t=>{
  const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);const bodies=[];const ids=[];
  globalThis.fetch=async (_url,init)=>{bodies.push(JSON.parse(init.body));return new Response('event: conversation\ndata: {"conversation_id":"c1"}\n\nevent: result\ndata: {"conversation_id":"c1","reply":"完成","sources":[]}\n\nevent: done\ndata: {}\n\n',{headers:{'Content-Type':'text/event-stream'}});};
  const handlers={onStatus(){},onToken(){},onConversation(id){ids.push(id);}};
  const context={scope:'bills/payments',client_request_id:'stable-first-send'};
  await streamChat('准备转账',undefined,handlers,context);
  await streamChat('准备转账',undefined,handlers,context);
  assert.equal(bodies[0].conversation_id,undefined);
  assert.deepEqual(bodies[0],bodies[1]);assert.deepEqual(ids,['c1','c1']);
});

test('explicit reply retry preserves message identity and signals failed-response retry', async t=>{
  const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);let body;
  globalThis.fetch=async (_url,init)=>{body=JSON.parse(init.body);return new Response('event: result\ndata: {"conversation_id":"c1","reply":"已恢复","sources":[]}\n\nevent: done\ndata: {}\n\n',{headers:{'Content-Type':'text/event-stream'}});};
  await streamChat('原消息','c1',{onStatus(){},onToken(){}},{scope:'bills/payments',client_request_id:'same-message',retry_failed:true});
  assert.equal(body.retry_failed,true);assert.equal(body.client_request_id,'same-message');assert.equal(body.conversation_id,'c1');
});
