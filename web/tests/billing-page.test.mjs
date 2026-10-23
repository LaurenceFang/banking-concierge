import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import {webcrypto} from "node:crypto";
import ts from "typescript";

const source = await readFile(new URL("../src/components/BillingPage.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const settle = () => new Promise(resolve => setImmediate(resolve));
const report = { entry_count: 0, entries: [], categories: [], anomalies: [] };

// Drive the component's real handlers and effects with controlled async responses.
// No DOM is needed for these request-order and lifecycle regressions.
function mount(api) {
  const slots = [];
  let cursor = 0;
  let effects = [];
  const react = {
    useState(initial) {
      const i = cursor++;
      slots[i] ??= { value: initial };
      return [slots[i].value, value => { slots[i].value = value; }];
    },
    useRef(initial) {
      const i = cursor++;
      slots[i] ??= { current: initial };
      return slots[i];
    },
    useEffect(effect, dependencies) {
      const i = cursor++;
      const previous = slots[i];
      if (!previous || dependencies.some((value, j) => value !== previous.dependencies[j])) {
        effects.push(() => {
          previous?.cleanup?.();
          slots[i] = { dependencies, cleanup: effect() };
        });
      }
    },
  };
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  vm.runInNewContext(compiled, { exports, crypto:webcrypto, require: name => {
    if (name === "react") return react;
    if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (name === "../api") return { money: String, ...api };
    throw new Error(name);
  } });
  return {
    render(revision = 0) {
      cursor = 0;
      effects = [];
      const tree = exports.BillingPage({ revision, mode: "import" });
      effects.forEach(effect => effect());
      const nodes = [];
      function walk(node) {
        if (Array.isArray(node)) return node.forEach(walk);
        if (!node || typeof node !== "object") return;
        nodes.push(node);
        walk(node.props?.children);
      }
      walk(tree);
      return {
        dates: nodes.filter(node => node.type === "input" && node.props.type === "date"),
        query: nodes.find(node => node.type === "button"),
        form: nodes.find(node => node.type === "form"),
        file: nodes.find(node => node.props?.type === "file"),
      };
    },
    unmount() { slots.forEach(slot => slot.cleanup?.()); },
  };
}

test("invalid dates during an outstanding refresh cannot leave query permanently disabled", async () => {
  const pending = deferred();
  const page = mount({ getBillingReport: () => pending.promise });
  let ui = page.render();
  ui.dates[0].props.onChange({ target: { value: "2026-10-22" } });
  ui.dates[1].props.onChange({ target: { value: "2026-10-01" } });
  page.render(1);
  assert.equal(page.render(1).query.props.disabled, false);
  pending.resolve(report);
  await settle();
  assert.equal(page.render(1).query.props.disabled, false);
});

test("finishing an import refreshes the latest date filters instead of its starting filters", async () => {
  const pending = deferred();
  const calls = [];
  const page = mount({
    getBillingReport: async (...dates) => { calls.push(dates); return report; },
    importBilling: () => pending.promise,
  });
  let ui = page.render();
  ui.file.props.onChange({ target: { files: [{ name: "test.csv", size: 10, text: async () => "csv" }] } });
  await settle();
  ui.dates[0].props.onChange({ target: { value: "2026-10-01" } });
  ui = page.render();
  ui.form.props.onSubmit({ preventDefault() {} });
  await settle();
  pending.resolve({ imported: 1, duplicates: 0, errors: [] });
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [["", "", {query:"",category:"",source:""}], ["2026-10-01", "", {query:"",category:"",source:""}], ["2026-10-01", "", {query:"",category:"",source:""}]]);
});

test("an import finishing after leaving the page does not request an obsolete report", async () => {
  const pending = deferred();
  let reads = 0;
  const page = mount({
    getBillingReport: async () => { reads++; return report; },
    importBilling: () => pending.promise,
  });
  const ui = page.render();
  ui.file.props.onChange({ target: { files: [{ name: "test.csv", size: 10, text: async () => "csv" }] } });
  await settle();
  page.unmount();
  pending.resolve({ imported: 1, duplicates: 0, errors: [] });
  await settle();
  assert.equal(reads, 1);
});
