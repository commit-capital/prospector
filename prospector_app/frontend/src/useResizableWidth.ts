import { useCallback, useEffect, useRef, useState } from "react";

const MIN_WIDTH = 380;

/** A right-anchored panel width that the user can drag (handle on the left
 *  edge) and that persists across reloads in localStorage. Width is measured
 *  from the right edge of the viewport, so the handle tracks the cursor. */
export function useResizableWidth(storageKey: string, fallback = 640) {
  const [width, setWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem(storageKey));
    return saved && saved >= MIN_WIDTH ? saved : fallback;
  });
  const dragging = useRef(false);

  const startResize = useCallback((e: { preventDefault: () => void }) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const max = window.innerWidth * 0.96;
      setWidth(Math.max(MIN_WIDTH, Math.min(max, window.innerWidth - e.clientX)));
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setWidth((w) => { localStorage.setItem(storageKey, String(Math.round(w))); return w; });
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [storageKey]);

  return { width, startResize };
}
