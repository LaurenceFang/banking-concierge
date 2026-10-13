import { useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import "../side-panel-resize.css";

const storageKey = "yx2.side-panel.width.v1";
const defaultWidth = 360;

export function useSidePanelResize() {
  const workspaceRef = useRef<HTMLDivElement>(null);
  const [workspaceWidth, setWorkspaceWidth] = useState(window.innerWidth);
  const [preferred, setPreferred] = useState(() => {
    try {
      const value = Number(localStorage.getItem(storageKey));
      return Number.isFinite(value) && value >= 320 ? Math.min(720, value) : defaultWidth;
    } catch { return defaultWidth; }
  });
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ pointer: number; x: number; width: number } | null>(null);
  const savedWidth = useRef(preferred);
  // Both rails can change independently: measure remaining space, not viewport alone.
  const overlay = workspaceWidth < 720;
  const maximum = Math.max(1, Math.min(720, overlay ? workspaceWidth : workspaceWidth - 400));
  const minimum = Math.min(320, maximum);
  const width = Math.max(minimum, Math.min(preferred, maximum));

  useLayoutEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return;
    const measure = () => setWorkspaceWidth(workspace.getBoundingClientRect().width);
    const observer = new ResizeObserver(measure);
    observer.observe(workspace);
    measure();
    return () => observer.disconnect();
  }, []);

  function update(value: number) {
    const next = Math.max(minimum, Math.min(maximum, value));
    savedWidth.current = next;
    setPreferred(next);
  }
  function persist() {
    try { localStorage.setItem(storageKey, String(savedWidth.current)); } catch { /* Optional preference. */ }
  }
  function finish(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    persist();
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  return {
    workspaceRef, overlay, dragging,
    style: { "--side-panel-width": `${width}px` } as CSSProperties,
    separator: <div
      className="side-panel-resizer" role="separator" aria-label="调整右侧面板宽度"
      aria-orientation="vertical" aria-valuemin={Math.round(minimum)} aria-valuemax={Math.round(maximum)}
      aria-valuenow={Math.round(width)} tabIndex={0}
      title="拖动调整面板宽度；左右方向键也可调整"
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        drag.current = { pointer: event.pointerId, x: event.clientX, width };
        event.currentTarget.setPointerCapture(event.pointerId);
        setDragging(true);
      }}
      onPointerMove={event => {
        if (drag.current?.pointer === event.pointerId) update(drag.current.width + drag.current.x - event.clientX);
      }}
      onPointerUp={finish} onPointerCancel={finish}
      onLostPointerCapture={() => { drag.current = null; setDragging(false); }}
      onKeyDown={event => {
        const next = event.key === "ArrowLeft" ? width + 16 : event.key === "ArrowRight" ? width - 16
          : event.key === "Home" ? minimum : event.key === "End" ? maximum : null;
        if (next === null) return;
        event.preventDefault(); update(next); persist();
      }}
    />,
  };
}
