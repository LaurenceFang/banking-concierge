import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
const source = await readFile(
  new URL("../src/api.ts", import.meta.url),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const api = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);
function capture(t, result = { status: "declined", message: "已取消" }) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: init?.body ? JSON.parse(init.body) : undefined });
    return Response.json(result);
  };
  t.after(() => {
    globalThis.fetch = original;
  });
  return calls;
}
test("transfer and subscription decisions are separate, explicit and scoped to their conversation", async (t) => {
  const calls = capture(t);
  await api.decideAction(
    { id: "transfer/a", kind: "transfer" },
    "approve",
    "conversation/a",
  );
  await api.decideAction(
    { id: "subscription/b", kind: "subscription" },
    "decline",
    "conversation/b",
  );
  assert.deepEqual(calls, [
    {
      url: "/api/plans/transfer%2Fa/decision",
      body: { decision: "approve", conversation_id: "conversation/a" },
    },
    {
      url: "/api/subscription-plans/subscription%2Fb/decision",
      body: { decision: "decline", conversation_id: "conversation/b" },
    },
  ]);
});
test("subscription preview never sends a decision and preserves operation semantics", async (t) => {
  const calls = capture(t);
  await api.previewSubscription("cloud", "stop_autopay", "conversation-1");
  assert.deepEqual(calls, [
    {
      url: "/api/subscriptions/cloud/preview",
      body: { action: "stop_autopay", conversation_id: "conversation-1" },
    },
  ]);
});
test("CSV is sent as text with original filename and exact unicode/newlines", async (t) => {
  const content = "日期,商户,金额\r\n2026-10-12,测试咖啡,-23.90\r\n";
  const calls = capture(t, {
    imported: 1,
    duplicates: 0,
    errors: [],
    message: "完成",
  });
  await api.importBilling("测试账单.csv", content);
  assert.deepEqual(calls, [
    { url: "/api/billing/import", body: { filename: "测试账单.csv", content } },
  ]);
});
test("date filters omit empty bounds and preserve both supplied dates", async (t) => {
  const calls = capture(t);
  await api.getBillingReport("", "2026-10-12");
  await api.getBillingReport("2026-04-01", "2026-10-12");
  assert.equal(calls[0].url, "/api/billing/report?end=2026-10-12");
  assert.equal(
    calls[1].url,
    "/api/billing/report?start=2026-04-01&end=2026-10-12",
  );
});
