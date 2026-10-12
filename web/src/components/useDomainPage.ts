import { useEffect, useRef, useState } from "react";

// Every read and mutation belongs to this mounted conversation/page instance.
export function useDomainPage<T>(load: () => Promise<T>, revision: number, onBusy: (busy: boolean) => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const lock = useRef(false);
  const version = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; version.current++; }; }, []);
  useEffect(() => {
    const current = ++version.current;
    void load().then(result => { if (alive.current && current === version.current) { setData(result); setError(""); } })
      .catch(failure => { if (alive.current && current === version.current) setError(failure.message); });
    return () => { version.current++; };
  }, [load, revision]);
  async function run(task: () => Promise<void>, reload = false) {
    if (lock.current) return;
    lock.current = true;
    version.current++;
    setBusy(true); onBusy(true); setError("");
    try {
      await task();
      if (reload && alive.current) {
        const current = ++version.current;
        const result = await load();
        if (alive.current && current === version.current) setData(result);
      }
    } catch (failure) { if (alive.current) setError(failure instanceof Error ? failure.message : "操作未完成，请重试。"); }
    finally { lock.current = false; if (alive.current) { setBusy(false); onBusy(false); } }
  }
  return { data, error, busy, run, alive };
}
