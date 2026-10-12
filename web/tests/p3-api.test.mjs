import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
const source=await readFile(new URL("../src/api.ts",import.meta.url),"utf8");
const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const api=await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
function capture(t,result={status:"declined",message:"取消"}) {
  const calls=[];const original=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{calls.push({url,method:init?.method??"GET",body:init?.body?JSON.parse(init.body):undefined});return Response.json(result);};
  t.after(()=>{globalThis.fetch=original;});return calls;
}
test("amount parser preserves exact cents and refuses rounding or unsafe values",()=>{
  assert.equal(api.parseMinor("1000"),100000);assert.equal(api.parseMinor("0.29"),29);assert.equal(api.parseMinor(" 12.3 "),1230);
  for(const invalid of ["1.001","1e3","-1","0","1,000","NaN","90071992547410.00"]) assert.throws(()=>api.parseMinor(invalid));
});
test("card and wealth decisions retain explicit choice and conversation scope",async t=>{
  const calls=capture(t);
  await api.decideAction({kind:"card",id:"card/a"},"decline","one");
  await api.decideAction({kind:"wealth",id:"wealth/b"},"approve","two");
  assert.deepEqual(calls,[{url:"/api/card-plans/card%2Fa/decision",method:"POST",body:{decision:"decline",conversation_id:"one"}},{url:"/api/wealth-plans/wealth%2Fb/decision",method:"POST",body:{decision:"approve",conversation_id:"two"}}]);
});
test("previews send integer amounts and never imply approval",async t=>{
  const calls=capture(t);
  const card={action:"set_daily_limit",card_id:"card",amount_minor:123456,conversation_id:"one"};
  const wealth={action:"purchase",product_id:"flex",account_id:"checking",amount_minor:100000,conversation_id:"two"};
  await api.previewCard(card);await api.previewWealth(wealth);
  assert.deepEqual(calls,[{url:"/api/cards/preview",method:"POST",body:card},{url:"/api/wealth/preview",method:"POST",body:wealth}]);
});
test("risk answers are sent unchanged; queries never advance the demo clock",async t=>{
  const calls=capture(t);
  const answers={horizon:"medium",loss_tolerance:"moderate",experience:"basic",liquidity_need:"medium",objective:"balanced"};
  await api.getCards();await api.getWealth();await api.saveRiskProfile(answers);
  assert.deepEqual(calls,[{url:"/api/cards",method:"GET",body:undefined},{url:"/api/wealth",method:"GET",body:undefined},{url:"/api/wealth/risk-profile",method:"POST",body:{answers}}]);
  await api.advanceWealthDay();
  assert.deepEqual(calls[3],{url:"/api/wealth/demo/advance",method:"POST",body:{business_days:1}});
});
