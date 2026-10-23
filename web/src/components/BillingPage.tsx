import { useEffect, useRef, useState } from "react";
import { getBillingReport, importBilling, money } from "../api";
import type { OpenRecord } from "./RecordPanel";
import type { BillingReport, ImportResult } from "../types";
const sourceNames: Record<string, string> = {
  manual: "手工记账",
  bank: "模拟银行",
  demo: "演示账单",
  import: "CSV 导入",
};
const kindNames: Record<string, string> = {
  expense: "消费",
  refund: "退款",
  income: "收入",
  transfer: "转账",
  opening: "期初",
};
export function BillingPage({ revision, mode = "analysis", onManage, managementBlocked = false }: { onManage?:OpenRecord; managementBlocked?:boolean; revision: number; mode?: "entries"|"analysis"|"import" }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [query,setQuery]=useState("");const [category,setCategory]=useState("");const [source,setSource]=useState("");
  const importRetry=useRef<{filename:string;content:string;id:string}|null>(null); const importLock=useRef(false);
  const [report, setReport] = useState<BillingReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const version = useRef(0);
  const alive = useRef(true);
  const latestRefresh = useRef(refresh);
  latestRefresh.current = refresh;
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  async function refresh(clear=false) {
    const v = ++version.current;
    if (!clear && start && end && start > end) {
      setBusy(false);
      setError("开始日期不能晚于结束日期。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const data = await getBillingReport(clear?"":start, clear?"":end,clear?{}:{query,category,source});
      if (v === version.current) setReport(data);
    } catch (failure) {
      if (v === version.current)
        setError(failure instanceof Error ? failure.message : "账单读取失败。");
    } finally {
      if (v === version.current) setBusy(false);
    }
  }
  useEffect(() => {
    void refresh();
    return () => {
      version.current++;
    };
  }, [revision]);
  async function importFile(file?: File) {
    if (!file || importLock.current) return;
    setError("");
    setResult(null);
    if (file.size > 2 * 1024 * 1024) {
      setError("CSV 文件不能超过 2 MiB。");
      return;
    }
    importLock.current=true;
    setImporting(true);
    try {
      const content = await file.text();
      if (!alive.current) return;
      if (!importRetry.current || importRetry.current.filename!==file.name || importRetry.current.content!==content) importRetry.current={filename:file.name,content,id:crypto.randomUUID()};
      const data = await importBilling(file.name, content, importRetry.current.id);
      importRetry.current=null;
      if (!alive.current) return;
      setResult(data);
      await latestRefresh.current();
    } catch (failure) {
      if (alive.current) setError(failure instanceof Error ? failure.message : "CSV 导入失败。");
    } finally {
      importLock.current=false;
      if (alive.current) setImporting(false);
    }
  }
  return (
    <section className="data-page" aria-label="账单分析">
      <div className="page-intro">
        <h2>{mode === "entries" ? "收支明细" : mode === "import" ? "导入整理" : "分析与预算"}</h2>
        <p>{mode === "entries" ? "每一笔收支，清楚可查。" : mode === "import" ? "导入 CSV 账单，整理分类与重复记录。" : "分类与周期来自账单规则，异常仅供复核；暂不支持设置预算。"}</p>
      </div>
      <form
        className="filter-bar record-filters"
        onSubmit={(event) => {
          event.preventDefault();
          void refresh();
        }}
      >
        <label>
          开始日期
          <input
            type="date"
            value={start}
            onInput={(event) => setStart(event.currentTarget.value)}
            onChange={(event) => setStart(event.target.value)}
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={end}
            onInput={(event) => setEnd(event.currentTarget.value)}
            onChange={(event) => setEnd(event.target.value)}
          />
        </label>
        <button className="button secondary" disabled={busy}>
          查询
        </button>
        <label>商户或备注<input aria-label="搜索商户或备注" maxLength={160} value={query} onChange={e=>setQuery(e.target.value)}/></label>
        <label>类别<input aria-label="筛选类别" maxLength={80} value={category} onChange={e=>setCategory(e.target.value)}/></label>
        <label>来源<select aria-label="筛选来源" value={source} onChange={e=>setSource(e.target.value)}><option value="">全部来源</option>{Object.entries(sourceNames).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
        <button type="button" className="text-button" onClick={()=>{setStart("");setEnd("");setQuery("");setCategory("");setSource("");void refresh(true);}}>清除筛选</button>
        {onManage&&<button type="button" className="button secondary" disabled={managementBlocked} onClick={()=>onManage({kind:"entry"})}>手工记账</button>}
        {mode === "import" && <><label className="button secondary file-button">
          {importing ? "正在导入…" : "导入 CSV"}
          <input
            type="file"
            accept=".csv,text/csv"
            aria-label="选择 CSV 账单"
            disabled={importing}
            onChange={(event) => {
              void importFile(event.target.files?.[0]);
              event.target.value = "";
            }}
          />
        </label>
        <a className="text-button" href="/api/billing/sample" download>
          下载示例
        </a>{onManage&&<button type="button" className="button secondary" disabled={managementBlocked} onClick={()=>onManage({kind:"batches"})}>导入记录</button>}</>}
      </form>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      {result && (
        <div className="import-result" role="status">
          <p>
            {result.message} · 新增 {result.imported} 笔，重复{" "}
            {result.duplicates} 笔，错误 {result.errors.length} 行
          </p>
          {result.errors.map((item, index) => (
            <p className="inline-error" key={index}>
              第 {item.row} 行：{item.message}
            </p>
          ))}
        </div>
      )}
      {busy && (
        <p className="muted" role="status">
          正在读取账单…
        </p>
      )}
      {report && (
        <>
          <details className="source-notice"><summary>共 {report.entry_count} 笔 · {report.coverage_start ?? "—"} 至 {report.coverage_end ?? "—"} · 数据说明</summary><p>{report.source_notice}</p></details>
          {mode === "analysis" && <><div className="metric-row">
            {[
              ["净消费", report.net_expense_minor],
              ["消费扣款", report.expense_minor],
              ["消费退款", report.refund_minor],
              ["收入", report.income_minor],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{money(Number(value))}</strong>
              </div>
            ))}
          </div>
          <p className="muted small">
            净消费 = 消费扣款 −
            消费退款。期初与转账不计入消费和收入；覆盖不完整时不推算全年。
          </p>
          <div className="report-columns">
            <section>
              <h3>分类汇总</h3>
              {report.categories.length ? (
                report.categories.map((item) => (
                  <div className="stat-row" key={item.category}>
                    <span>
                      {item.category}{" "}
                      <small className="muted">{item.count} 笔</small>
                    </span>
                    <span>{money(item.amount_minor)}</span>
                  </div>
                ))
              ) : (
                <p className="muted">此区间暂无消费分类。</p>
              )}
            </section>
            <section>
              <h3>待复核记录</h3>
              {report.anomalies.length ? (
                report.anomalies.map((item, index) => (
                  <div
                    className="anomaly-row"
                    key={`${item.entry_id}-${index}`}
                  >
                    <div>
                      {item.merchant} · {money(item.amount_minor)}
                    </div>
                    <p>{item.reason}</p>
                  </div>
                ))
              ) : (
                <p className="muted">此区间未发现规则提示。</p>
              )}
            </section>
          </div>
          </>}
          <h3>{mode === "import" ? "已整理记录" : "账单明细"}</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>日期 / 来源</th>
                  <th>商户</th>
                  <th>分类 / 类型</th>
                  <th>金额</th>
                  {mode !== "entries" && <th>规则依据</th>}<th>备注 / 标签</th>{onManage&&<th>操作</th>}
                </tr>
              </thead>
              <tbody>
                {report.entries.slice().sort((a,b)=>b.date.localeCompare(a.date)).map((entry) => (
                  <tr key={entry.id}>
                    <td>
                      {entry.date}
                      <small>{sourceNames[entry.source] ?? entry.source}</small>
                    </td>
                    <td>
                      {entry.merchant}
                      {mode === "entries" && <details className="small muted"><summary>分类依据</summary><p>{entry.reason}</p></details>}
                      {entry.raw_merchant !== entry.merchant && (
                        <small>原始：{entry.raw_merchant}</small>
                      )}
                    </td>
                    <td>
                      {entry.category}
                      <small>
                        {kindNames[entry.transaction_kind] ??
                          entry.transaction_kind}
                      </small>
                    </td>
                    <td className={entry.amount_minor > 0 ? "positive" : ""}>
                      {money(entry.amount_minor, entry.currency)}
                    </td>
                    {mode !== "entries" && <td>{entry.reason}</td>}<td className="record-note">{entry.note||"—"}<small>{entry.tags?.join(" · ")}</small></td>{onManage&&<td><button className="text-button" disabled={managementBlocked} onClick={()=>onManage({kind:"entry",entry})}>整理</button></td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!report.entries.length && <p className="muted">此区间暂无账单。</p>}
        </>
      )}
    </section>
  );
}
