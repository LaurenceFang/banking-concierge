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
const { streamChat } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);
const finalResult = {
  conversation_id: "conversation-1",
  reply: "最终答复",
  sources: [],
  plan: null,
};
const event = (name, data) =>
  `event: ${name}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
const handlers = () => ({ onStatus() {}, onToken() {} });
function mockFetch(t, implementation) {
  const original = globalThis.fetch;
  globalThis.fetch = implementation;
  t.after(() => {
    globalThis.fetch = original;
  });
}
function sseResponse(text, step = 7) {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream({
      start(controller) {
        for (let offset = 0; offset < bytes.length; offset += step)
          controller.enqueue(bytes.slice(offset, offset + step));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream; charset=utf-8" } },
  );
}

test("split UTF-8 and CRLF events preserve status/tokens and return authoritative result", async (t) => {
  const status = [];
  const tokens = [];
  const content =
    event("status", { message: "正在检索" }) +
    event("token", { text: "部分中文" }) +
    event("token", { text: "答复" }) +
    event("result", finalResult) +
    event("done", {});
  mockFetch(t, async () => sseResponse(content, 1));
  const result = await streamChat("问题", undefined, {
    onStatus: (text) => status.push(text),
    onToken: (text) => tokens.push(text),
  });
  assert.deepEqual(result, finalResult);
  assert.deepEqual(status, ["正在检索"]);
  assert.equal(tokens.join(""), "部分中文答复");
});

test("only an initial unsupported route falls back to the ordinary chat endpoint", async (t) => {
  const calls = [];
  mockFetch(t, async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return calls.length === 1
      ? new Response(null, { status: 404 })
      : Response.json(finalResult);
  });
  assert.deepEqual(
    await streamChat("转账预览", "conversation-1", handlers()),
    finalResult,
  );
  assert.deepEqual(
    calls.map((call) => call.url),
    ["/api/chat/stream", "/api/chat"],
  );
  assert.deepEqual(calls[0].body, calls[1].body);
});

test("consumed stream errors are surfaced without replaying the request", async (t) => {
  let count = 0;
  mockFetch(t, async () => {
    count++;
    return sseResponse(
      event("status", { message: "处理中" }) +
        event("error", { code: "provider_timeout", message: "模型响应超时" }) +
        event("done", {}),
    );
  });
  await assert.rejects(streamChat("转账预览", undefined, handlers()), {
    message: "模型响应超时",
    code: "provider_timeout",
  });
  assert.equal(count, 1);
});

test("truncated stream never treats partial tokens as a successful reply or retries", async (t) => {
  let count = 0;
  mockFetch(t, async () => {
    count++;
    return sseResponse(event("token", { text: "尚未完成" }));
  });
  await assert.rejects(streamChat("问题", undefined, handlers()), {
    code: "incomplete_stream",
  });
  assert.equal(count, 1);
});

test("network failure and server failure never trigger automatic resend", async (t) => {
  let count = 0;
  mockFetch(t, async () => {
    count++;
    throw new TypeError("network disconnected");
  });
  await assert.rejects(streamChat("问题", undefined, handlers()), {
    code: "connection_failed",
  });
  assert.equal(count, 1);
  globalThis.fetch = async () => {
    count++;
    return Response.json(
      { detail: { code: "failed", message: "服务中断" } },
      { status: 500 },
    );
  };
  await assert.rejects(streamChat("问题", undefined, handlers()), {
    code: "failed",
  });
  assert.equal(count, 2);
});
