import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
async function load(file) {
  const source=await readFile(new URL(file,import.meta.url),'utf8');
  const compiled=ts.transpileModule(source,{fileName:file,compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText
    .replace(/from "([^".][^"]*)"/g,(_all,name)=>`from "${import.meta.resolve(name)}"`);
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
}
const {splitConversationGroups,ConversationGroup}=await load('../src/components/SidebarControls.tsx');
const {pinConversation}=await load('../src/api.ts');
const row=(id,pinned=false,pinned_at=null,kind='temporary')=>({id,title:id,pinned,pinned_at,kind,updated_at:'2026-10-13',unread_count:0});
test('pinned group excludes main and preserves recent order without duplication',()=>{
  const input=[row('main',true,'2026-10-14','main'),row('recent-2'),row('pin-z',true,'2026-10-12'),row('pin-b',true,'2026-10-13'),row('pin-a',true,'2026-10-13'),row('recent-1')];
  const grouped=splitConversationGroups(input);
  assert.deepEqual(grouped.pinned.map(x=>x.id),['pin-a','pin-b','pin-z']);
  assert.deepEqual(grouped.recent.map(x=>x.id),['recent-2','recent-1']);
  assert.equal(new Set([...grouped.pinned,...grouped.recent].map(x=>x.id)).size,5);
  assert.equal(input[0].id,'main');
});
test('collapsed section removes conversation buttons; empty pinned keeps its heading',()=>{
  const collapsed=renderToStaticMarkup(createElement(ConversationGroup,{name:'pinned',label:'置顶',open:false,empty:false,loading:false,onToggle(){}},createElement('button',null,'hidden conversation')));
  assert.match(collapsed,/aria-expanded="false"/);assert.doesNotMatch(collapsed,/hidden conversation/);
  const empty=renderToStaticMarkup(createElement(ConversationGroup,{name:'pinned',label:'置顶',open:true,empty:true,loading:false,onToggle(){}}));
  assert.match(empty,/置顶/);assert.match(empty,/常用对话可右键置顶/);
});
test('pin API sends strict boolean and propagates failure without conversation navigation',async t=>{
  const previous=globalThis.fetch;t.after(()=>globalThis.fetch=previous);const calls=[];
  globalThis.fetch=async(url,init)=>{calls.push([url,init]);return Response.json(row('a/b',true,'2026-10-13'));};
  assert.equal((await pinConversation('a/b',true)).pinned,true);
  assert.equal(calls[0][0],'/api/conversations/a%2Fb/pin');
  assert.equal(calls[0][1].method,'PATCH');assert.deepEqual(JSON.parse(calls[0][1].body),{pinned:true});
  globalThis.fetch=async()=>Response.json({detail:{message:'稍后重试'}},{status:503});
  await assert.rejects(()=>pinConversation('a/b',false),/稍后重试/);
});
