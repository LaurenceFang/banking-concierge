import { useEffect, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Pencil, Pin, PinOff, ChevronDown, ChevronRight } from "lucide-react";

import type { Conversation } from "../types";

const WIDTH_KEY = "yx2.rail.width.v1";
const minimum = 200;
const maximum = () => Math.min(600, Math.max(minimum, window.innerWidth - 480));
const clamp = (value: number) => Math.min(maximum(), Math.max(minimum, value));

export function useRailResize() {
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(WIDTH_KEY));
      return clamp(Number.isFinite(saved) && saved >= minimum ? saved : 260);
    } catch { return clamp(260); }
  });
  const [dragging, setDragging] = useState(false);
  const widthRef = useRef(width);
  const drag = useRef<{ pointer: number; origin: number; width: number } | null>(null);
  const update = (value: number) => {
    widthRef.current = clamp(value);
    setWidth(widthRef.current);
  };
  const persist = () => {
    try { localStorage.setItem(WIDTH_KEY, String(widthRef.current)); } catch { /* Optional preference. */ }
  };
  useEffect(() => {
    const resize = () => update(widthRef.current);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const finish = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    persist();
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return {
    style: { "--rail-width": `${width}px` } as CSSProperties,
    dragging,
    separator: <div
      className="rail-resizer" role="separator" aria-label="调整侧栏宽度"
      aria-orientation="vertical" aria-valuemin={minimum} aria-valuemax={maximum()}
      aria-valuenow={Math.round(width)} tabIndex={0}
      title="拖动调整侧栏宽度；也可用左右方向键"
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        drag.current = { pointer: event.pointerId, origin: event.clientX, width: widthRef.current };
        event.currentTarget.setPointerCapture(event.pointerId);
        setDragging(true);
      }}
      onPointerMove={event => {
        if (drag.current?.pointer === event.pointerId) update(drag.current.width + event.clientX - drag.current.origin);
      }}
      onPointerUp={finish} onPointerCancel={finish}
      onLostPointerCapture={() => { drag.current = null; setDragging(false); }}
      onKeyDown={event => {
        const value = event.key === "ArrowLeft" ? widthRef.current - 16
          : event.key === "ArrowRight" ? widthRef.current + 16
          : event.key === "Home" ? minimum : event.key === "End" ? maximum() : null;
        if (value === null) return;
        event.preventDefault();
        update(value);
        persist();
      }}
    />,
  };
}

export function ConversationTitle({ title }: { title: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth + 1);
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    // Font substitution can change text width without changing its container.
    let mounted = true;
    void document.fonts.ready.then(() => { if (mounted) measure(); });
    return () => { mounted = false; observer.disconnect(); };
  }, [title]);
  return <span ref={ref} className={`conversation-title${overflow ? " is-overflowing" : ""}`}>{title}</span>;
}

export interface ConversationMenuTarget {
  id: string;
  title: string;
  pinned: boolean;
  x: number;
  y: number;
  trigger: HTMLElement;
}

export function ConversationMenu({ target, onClose, onRename, onPin, busy }: {
  target: ConversationMenuTarget;
  onClose: () => void;
  onRename: () => void;
  onPin: () => void;
  busy: boolean;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const firstItem = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    firstItem.current?.focus();
    const outside = (event: globalThis.PointerEvent) => {
      if (!menu.current?.contains(event.target as Node)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      if (event.key === "Tab") onClose();
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (current + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
        items[index]?.focus();
      }
    };
    window.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", onClose, true);
    return () => {
      window.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", escape, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onClose, true);
      if (menu.current?.contains(document.activeElement) || document.activeElement === document.body) {
        if (target.trigger.isConnected) target.trigger.focus();
      }
    };
  }, [target, onClose]);
  return createPortal(<div
    ref={menu} className="conversation-context-menu" role="menu" aria-label="对话操作"
    style={{ left: Math.max(8, Math.min(target.x, window.innerWidth - 184)), top: Math.max(8, Math.min(target.y, window.innerHeight - 102)) }}
    onContextMenu={event => event.preventDefault()}
  >
    <button ref={firstItem} role="menuitem" disabled={busy} onClick={onPin}>{target.pinned ? <PinOff size={15}/> : <Pin size={15}/>} {busy ? "正在更新…" : target.pinned ? "取消置顶" : "置顶"}</button>
    <button role="menuitem" disabled={busy} onClick={onRename}><Pencil size={15} />重命名</button>
  </div>, document.body);
}


export function splitConversationGroups(conversations: Conversation[]) {
  const ordinary = conversations.filter(item => item.kind !== "main");
  return {
    pinned: ordinary.filter(item => item.pinned).sort((a, b) => (b.pinned_at ?? "").localeCompare(a.pinned_at ?? "") || a.id.localeCompare(b.id)),
    recent: ordinary.filter(item => !item.pinned),
  };
}

type GroupName = "pinned" | "recent";
type GroupPreferences = Record<string, Partial<Record<GroupName, boolean>>>;
const GROUP_KEY = "yx2.sidebar.groups.v1";
export function useConversationGroups(scope?: string) {
  const [preferences, setPreferences] = useState<GroupPreferences>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(GROUP_KEY) ?? "{}");
      return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
    } catch { return {}; }
  });
  const key = scope ?? "global";
  const open = (group: GroupName) => preferences[key]?.[group] !== false;
  const toggle = (group: GroupName) => setPreferences(previous => {
    const next = {...previous, [key]: {...previous[key], [group]: previous[key]?.[group] === false}};
    try { localStorage.setItem(GROUP_KEY, JSON.stringify(next)); } catch { /* Optional preference. */ }
    return next;
  });
  return { open, toggle };
}

export function ConversationGroup({ name, label, open, loading, empty, children, onToggle }: {
  name: GroupName; label: string; open: boolean; loading: boolean; empty: boolean;
  children: ReactNode; onToggle: () => void;
}) {
  return <section className="conversation-group" aria-label={label}>
    <button className="conversation-group-heading" aria-expanded={open} aria-controls={`conversation-group-${name}`} onClick={onToggle}>
      <span>{label}</span>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}
    </button>
    {open && <div id={`conversation-group-${name}`}>
      {loading ? <p className="rail-empty" role="status">正在读取…</p> : empty ? <p className="rail-empty">{name === "pinned" ? "常用对话可右键置顶。" : "还没有最近对话。"}</p> : children}
    </div>}
  </section>;
}
