import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {webcrypto} from "node:crypto";
import vm from "node:vm";
import ts from "typescript";
const source=await readFile(new URL("../src/components/RecordPanel.tsx",import.meta.url),"utf8");
const compiled=ts.transpileModule(source+"\nexport { RecordForm };",{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function form(request){
 const slots=[];let cursor=0;let work;const calls=[];
 const react={useState(initial){const i=cursor++;slots[i]??={value:initial};return [slots[i].value,v=>{slots[i].value=v;}];},useRef(initial){const i=cursor++;slots[i]??={current:initial};return slots[i];},useEffect(){}};
 const jsx=(type,props)=>({type,props});const exports={};
 vm.runInNewContext(compiled,{exports,crypto:webcrypto,require:name=>{
  if(name==="react")return react;if(name==="react/jsx-runtime")return {jsx,jsxs:jsx};if(name==="lucide-react"||name.endsWith(".css"))return {};if(name==="../task-utils")return {beijingTime:String};
  if(name==="../api")return {money:String,parseMinor:v=>{if(!/^\d+(\.\d{1,2})?$/.test(v)||Number(v)<=0)throw Error("金额无效");return Math.round(Number(v)*100);},saveBillingEntry:async(...args)=>calls.push(args),savePersonalRecord:async(...args)=>calls.push(args)};throw Error(name);
 }});
 function render(){cursor=0;const tree=exports.RecordForm({request,busy:false,onDirty(){},onBack(){},onSave:fn=>{work=fn;}});const nodes=[];function walk(n){if(Array.isArray(n))return n.forEach(walk);if(!n?.props)return;nodes.push(n);walk(n.props.children);}walk(tree);return {nodes,tree};}
 return {render,calls,async submit(){const {tree}=render();tree.props.onSubmit({preventDefault(){}});if(work){const fn=work;work=undefined;await fn();}},set(label,value){const {nodes}=render();const n=nodes.find(n=>n.type==="label"&&n.props.children[0]===label);const control=n.props.children.find?.(n=>n?.type==="input"||n?.type==="select"||n?.type==="textarea");assert.ok(control,label);control.props.onChange({target:{value,checked:value}});}};
}
const entry={id:"bank-1",version:4,merchant:"银行商户",raw_merchant:"原始描述",amount_minor:-1250,date:"2026-10-22",transaction_kind:"expense",source:"bank",category:"餐饮",tags:[],note:""};
test("bank annotation form sends only editable metadata and optimistic version",async()=>{const ui=form({kind:"entry",entry});ui.set("分类","礼物");ui.set("标签（逗号分隔，最多 10 个）","节日，家人");await ui.submit();assert.deepEqual(JSON.parse(JSON.stringify(ui.calls[0])),["bank-1",{category:"礼物",tags:["节日","家人"],note:"",version:4}]);});
test("manual create is positive; retry preserves identity and changed payload gets a new identity",async()=>{const ui=form({kind:"entry"});ui.set("商户","测试咖啡");ui.set("金额（元，填写正数）","12.50");await ui.submit();await ui.submit();assert.equal(ui.calls[0][1].amount_minor,1250);assert.equal(ui.calls[0][1].transaction_kind,"expense");assert.equal(ui.calls[0][1].client_request_id,ui.calls[1][1].client_request_id);ui.set("金额（元，填写正数）","13.50");await ui.submit();assert.notEqual(ui.calls[1][1].client_request_id,ui.calls[2][1].client_request_id);assert.equal("version" in ui.calls[0][1],false);});
test("existing expense edit sends signed cents and no create-only identity",async()=>{const ui=form({kind:"entry",entry:{...entry,source:"manual"}});ui.set("金额（元，填写正数）","18.25");await ui.submit();assert.equal(ui.calls[0][1].amount_minor,-1825);assert.equal(ui.calls[0][1].version,4);assert.equal("client_request_id" in ui.calls[0][1],false);});
test("candidate confirmation creates a personal reminder record; edit never sends candidate identity or execution action",async()=>{const candidate={id:"candidate-1",merchant:"测试会员",amount_minor:1500,cycle:"monthly",next_charge_on:"2026-11-22"};const ui=form({kind:"personal",candidate});await ui.submit();assert.equal(ui.calls[0][1].candidate_id,"candidate-1");assert.equal(ui.calls[0][1].reminder_enabled,true);const edit=form({kind:"personal",record:{...ui.calls[0][1],id:"record-1",version:2}});await edit.submit();assert.equal(edit.calls[0][1].version,2);for(const key of ["client_request_id","candidate_id","action","active"])assert.equal(key in edit.calls[0][1],false);});


test("native date input events update the saved manual date and personal renewal date before blur",async()=>{
 const manual=form({kind:"entry"});manual.set("商户","日期测试");manual.set("金额（元，填写正数）","12.50");
 const input=manual.render().nodes.find(n=>n.type==="input"&&n.props.type==="date");input.props.onInput({currentTarget:{value:"2026-10-24"}});await manual.submit();assert.equal(manual.calls[0][1].date,"2026-10-24");
 const personal=form({kind:"personal",record:{id:"personal-date",version:3,name:"日期会员",amount_minor:1500,cycle:"monthly",next_charge_on:"2026-10-22",category:"",note:"",reminder_enabled:true,lead_days:3}});
 personal.render().nodes.find(n=>n.type==="input"&&n.props.type==="date").props.onInput({currentTarget:{value:"2026-10-24"}});await personal.submit();assert.equal(personal.calls[0][0],"personal-date");assert.equal(personal.calls[0][1].next_charge_on,"2026-10-24");assert.equal(personal.calls[0][1].version,3);
});
