import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
const source=await readFile(new URL('../src/api.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const api=await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
test('directory creation retries preserve request identity; edits and removals carry versions without business execution',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);const calls=[];
 globalThis.fetch=async(url,init={})=>{calls.push({url,...init});return Response.json({id:'person',version:2});};
 const body={name:'同名联系人',note:'AA',client_request_id:'stable-retry'};
 await api.getContacts('备注 & 尾号');await api.saveContact(undefined,body);await api.saveContact(undefined,body);
 await api.saveContact('person/1',{name:'新备注',version:2});await api.removeDirectoryItem('contacts','person/1',3);
 await api.getManagedServices('节日');await api.saveManagedService(undefined,{name:'贺卡',price_minor:2900,client_request_id:'service-retry'});await api.removeDirectoryItem('services','service/1',4);
 assert.equal(calls[0].url,'/api/contacts?query='+encodeURIComponent('备注 & 尾号'));
 assert.equal(calls[1].body,calls[2].body);assert.equal(calls[3].method,'PATCH');assert.equal(JSON.parse(calls[3].body).version,2);
 assert.equal(calls[4].url,'/api/contacts/person%2F1?version=3');assert.equal(calls[4].method,'DELETE');
 assert.equal(calls[7].url,'/api/workflow-services/service%2F1?version=4');
 assert.ok(calls.every(x=>!x.url.includes('decision')&&!x.url.includes('preview')));
});
test('failed directory save exposes server conflict; custom workflow retains title and service order',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);
 globalThis.fetch=async()=>Response.json({detail:{message:'资料已更新，请重新读取',code:'version_conflict'}},{status:409});
 await assert.rejects(()=>api.saveContact('person',{version:1}),e=>e.code==='version_conflict'&&e.message.includes('重新读取'));
 let sent;globalThis.fetch=async(url,init)=>{sent=JSON.parse(init.body);return Response.json({id:'preview'});};
 await api.previewWorkflow({action:'arrange',title:'节日安排',conversation_id:'original',items:[{product_id:'custom',quantity:1},{product_id:'cake',quantity:2}]});
 assert.equal(sent.title,'节日安排');assert.deepEqual(sent.items.map(x=>x.product_id),['custom','cake']);
});

test('real contact and service form bodies omit create-only identity on PATCH and edit-only version on POST',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);const calls=[];
 globalThis.fetch=async(url,init)=>{calls.push({url,...init,parsed:JSON.parse(init.body)});return Response.json({id:'saved'});};
 const contact={name:'测试联系人',note:'同事',bank_name:null,account_last4:null,version:2,client_request_id:'contact-request'};
 const service={name:'测试服务',merchant_name:'模拟店',price_minor:2900,unit:'份',description:'说明',cancel_supported:true,version:3,client_request_id:'service-request'};
 await api.saveContact(undefined,contact);await api.saveContact('person',contact);
 await api.saveManagedService(undefined,service);await api.saveManagedService('service',service);
 for(const index of [0,2]){assert.equal(calls[index].method,'POST');assert.ok(!Object.hasOwn(calls[index].parsed,'version'));assert.ok(calls[index].parsed.client_request_id);}
 for(const index of [1,3]){assert.equal(calls[index].method,'PATCH');assert.ok(!Object.hasOwn(calls[index].parsed,'client_request_id'));assert.ok(calls[index].parsed.version);}
 assert.deepEqual(calls[1].parsed,{name:'测试联系人',note:'同事',bank_name:null,account_last4:null,version:2});
 assert.deepEqual(calls[3].parsed,{name:'测试服务',merchant_name:'模拟店',price_minor:2900,unit:'份',description:'说明',cancel_supported:true,version:3});
 assert.equal(contact.client_request_id,'contact-request');assert.equal(service.version,3);
});
test('validation detail arrays expose the offending fields as parameter errors',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);
 globalThis.fetch=async()=>Response.json({detail:[{type:'extra_forbidden',loc:['body','client_request_id'],msg:'Extra inputs are not permitted'},{type:'missing',loc:['body','name'],msg:'Field required'}]},{status:422});
 await assert.rejects(()=>api.saveContact('person',{version:1}),e=>e.message==='参数有误，保存请求标识：包含不支持的字段；名称：请补充此项');
});
