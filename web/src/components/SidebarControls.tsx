import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { Pencil } from "lucide-react";

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
  x: number;
  y: number;
  trigger: HTMLElement;
}

export function ConversationMenu({ target, onClose, onRename }: {
  target: ConversationMenuTarget;
  onClose: () => void;
  onRename: () => void;
}) {
  const menu = useRef<HTMLDivElement>(null);
  const rename = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    rename.current?.focus();
    const outside = (event: globalThis.PointerEvent) => {
      if (!menu.current?.contains(event.target as Node)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      if (event.key === "Tab") onClose();
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
      if (document.activeElement === rename.current || document.activeElement === document.body) {
        if (target.trigger.isConnected) target.trigger.focus();
      }
    };
  }, [target, onClose]);
  return createPortal(<div
    ref={menu} className="conversation-context-menu" role="menu" aria-label="对话操作"
    style={{ left: Math.max(8, Math.min(target.x, window.innerWidth - 184)), top: Math.max(8, Math.min(target.y, window.innerHeight - 60)) }}
    onContextMenu={event => event.preventDefault()}
  >
    <button ref={rename} role="menuitem" onClick={onRename}><Pencil size={15} />重命名</button>
  </div>, document.body);
}
